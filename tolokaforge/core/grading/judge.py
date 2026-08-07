"""Read-only agentic rubric judge (see ``docs/RUBRIC_GRADING_DESIGN.md``).

The judge is *"a solo read-only grader — the shared :class:`ToolCallingLoop`
with no user simulator, a harness-owned read-only toolset, and a rubric-shaped
``submit_report`` tool — run over the live final state, failing loud on its own
malfunction."*

Design:

* **Separate fixed judge model.** The judge constructs its own
  :class:`~tolokaforge.core.llm.client.LLMClient` from the run-level
  ``ModelConfig`` (``RunConfig.models["judge"]``) via the agent's
  ``build_capabilities`` path (so tool schemas/calls are provider-correct for
  Gemini / GPT-5 / …). API keys are read through
  :class:`SecretManager` inside ``LLMClient`` — never ``os.environ`` here.
* **Harness-owned read-only allowlist.** NOT the agent's tools, NO category
  filter, NO DB clone. The judge gets read-only DB tools, ``search_kb`` when the
  task is RAG-backed, file readers only when a real workspace exists, and the
  rubric-derived ``submit_report``.
* **Narrow input surface.** :meth:`Judge.run` receives only
  ``{agent_system_prompt, transcript, rubric (incl. reference/expected),
  read-tools, state_diff}`` — never ``golden_actions`` / ``expect_initial_state`` /
  ``jsonpath_checks``. The oracle fields cannot leak in by construction: they are
  not on the Protocol's ``run()`` surface.
* **Fail loud.** On :class:`SubmitReportValidationError` the judge re-prompts a
  bounded number of times; on exhaustion, on budget/turn exhaustion, or on any
  judge failure it returns :data:`JudgeStatus.ERRORED` with **no numeric score**.
  Never ``0.0`` / ``0.5``.

Async/sync bridge: this module is fully synchronous (``LLMClient.generate`` and
``ToolCallingLoop.run`` are sync). The DB read tools take a synchronous
``DBReader`` callable; the runner supplies one that bridges to its async
``DBServiceClient`` (see ``runner/service.py``). This module never touches an
event loop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from tolokaforge.core.grading.judge_tools import (
    GetDbStateTool,
    QueryDbTool,
    ReadFileTool,
    SearchKbTool,
    SubmitReportTool,
)
from tolokaforge.core.grading.kb_search import KnowledgeSearch
from tolokaforge.core.grading.rubric import (
    GRADED_MET_THRESHOLD,
    SUBMIT_REPORT_TOOL_NAME,
    SubmitReportValidationError,
    VerdictConsistencyError,
    aggregate_rubric,
    build_submit_report_tool,
    parse_submit_report,
)
from tolokaforge.core.llm.client import GenerationResult, LLMClient
from tolokaforge.core.logging import StructuredLogger, get_logger
from tolokaforge.core.loop import (
    LoopConfig,
    MetricsSink,
    TerminationDecision,
    ToolCallingLoop,
)
from tolokaforge.core.models import (
    Message,
    MessageRole,
    ModelConfig,
    TerminationReason,
    TrialStatus,
)
from tolokaforge.runner.models import Criterion, CriterionResult, Rubric
from tolokaforge.tools.registry import Tool, ToolExecutor, ToolRegistry

# ---------------------------------------------------------------------------
# Budget defaults (plan: max_turns ~12-15 + wall-time)
# ---------------------------------------------------------------------------

#: Default per-judge turn cap. Generous enough for read-then-grade, bounded so a
#: looping judge errors out rather than burning budget.
DEFAULT_JUDGE_MAX_TURNS = 14

#: Default wall-time budget for one judge episode, in seconds. The ``LoopConfig``
#: ``episode_timeout_s`` enforces this on the shared loop.
DEFAULT_JUDGE_EPISODE_TIMEOUT_S = 240

#: How many times we re-prompt the judge after a malformed ``submit_report``
#: before giving up with an errored grade.
DEFAULT_SUBMIT_REPORT_RETRIES = 2


# ---------------------------------------------------------------------------
# Read-side bridge contract (sync; runner adapts its async DB client to this)
# ---------------------------------------------------------------------------


class DBReader(Protocol):
    """Synchronous read seam over the trial's final DB state.

    The runner's :class:`~tolokaforge.runner.db_client.DBServiceClient` is async
    and lives on a dedicated event loop; the judge loop is sync and runs in a
    worker thread. The runner supplies an implementation that bridges each call
    back to its loop via ``asyncio.run_coroutine_threadsafe`` (safe from the
    worker thread). Strictly read-only — there is no mutate method here.
    """

    def get_state(self, tables: list[str] | None = None) -> dict[str, Any]: ...

    def query(self, jsonpath: str) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Judge status + outcome
# ---------------------------------------------------------------------------


class JudgeStatus(str, Enum):
    """Mirror of the proto ``JudgeStatus`` (kept as a host-side value object).

    ``ERRORED`` is the fail-loud marker: the judge malfunctioned and there is no
    trustworthy numeric score. ``COMPLETED`` means per-criterion results exist.
    ``UNSPECIFIED`` is never produced by this module (the caller uses it for the
    "no judge configured" case).
    """

    UNSPECIFIED = "unspecified"
    COMPLETED = "completed"
    ERRORED = "errored"


@dataclass(frozen=True)
class JudgeUsage:
    """The judge's own accounting — recorded to the output bundle (plan: judge cost).

    ``consistency_rejections`` counts ``submit_report`` attempts rejected for a
    verdict/justification marker mismatch (a :class:`VerdictConsistencyError`) on
    this trial — distinct from generic schema rejections, which are not counted.
    """

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    consistency_rejections: int = 0


@dataclass(frozen=True)
class JudgeResult:
    """Outcome of a rubric-judge run.

    ``status == ERRORED`` carries NO score (``score is None``) and NO criterion
    results — the fail-loud contract. ``status == COMPLETED`` carries the
    weighted ``score`` in ``[0, 1]``, the per-criterion ``criterion_results``,
    and ``gate_failed`` (a failed required criterion). ``reasons`` is always a
    human-readable diagnostic.
    """

    status: JudgeStatus
    usage: JudgeUsage
    reasons: str
    score: float | None = None
    binary_pass: bool | None = None
    gate_failed: bool = False
    criterion_results: tuple[CriterionResult, ...] = ()
    failed_required_ids: tuple[str, ...] = ()
    # Which KB backend(s) the judge was offered this trial — the visible signal
    # that the judge graded WITH (or WITHOUT) the knowledge base the agent used
    # (issue #95). ``("search_kb",)`` for rag-service, ``("search_policy",)`` for
    # the TypeSense passthrough, ``()`` for none offered. Surfaced verbatim into
    # ``reasons`` as a "Judge KB: …" note. Empty is NOT an error — we cannot
    # statically know a rubric needs KB — just an observability fact.
    kb_tools_offered: tuple[str, ...] = ()
    # KB-tagged tools the agent had this trial that the judge was constructed to
    # withhold (``disable_knowledge_search``). Empty when nothing was gated — either
    # the flag was off or the agent had no KB. ``knowledge_search_disabled`` records
    # the construction flag itself, so a disabled judge over a KB-less trial reads
    # ``knowledge_search_disabled=True`` with an empty ``kb_tools_withheld``.
    kb_tools_withheld: tuple[str, ...] = ()
    knowledge_search_disabled: bool = False
    # Whether the judge ran with a custom system-prompt body (the default marker
    # contract is always appended regardless). The full custom text is recorded in
    # the bundle's ``task.yaml`` grading config, not here — this is the honest bool.
    custom_system_prompt: bool = False
    # Whether the harness was configured to embed the agent's policy / system
    # prompt in the judge's opening-message evidence. Records the construction
    # setting, not whether a block physically appeared — a trial with an empty
    # agent prompt still reads ``True`` when the setting is default/on. Default
    # ``True``: the harness includes the agent policy unless gated off.
    include_agent_system_prompt: bool = True
    # Non-KB read-only tools the judge was offered this trial: ``get_db_state`` /
    # ``query_db`` (a DB reader was supplied), ``read_file`` (a workspace existed).
    # The KB surface is ``kb_tools_offered`` / ``kb_tools_withheld``. Recorded so an
    # offline replay knows which live backends to shim.
    read_tools_offered: tuple[str, ...] = ()
    # The ``initial → final`` state-delta string handed to the judge as its primary
    # outcome view (``None`` when no diff was built). Echoed from the ``run()`` input
    # so an offline replay can rebuild the judge's opening message from this exact
    # string rather than re-reading a live DB.
    state_diff: str | None = None
    # The judge's own message transcript (role / content / tool_calls dicts),
    # captured for audit/reproducibility (plan open question #2). Populated for
    # both COMPLETED and ERRORED runs — an errored judge's partial transcript is
    # the most useful artifact for debugging WHY it failed.
    transcript: tuple[dict[str, Any], ...] = ()


# ---------------------------------------------------------------------------
# Metrics sink — accumulates the judge's own usage
# ---------------------------------------------------------------------------


@dataclass
class _JudgeMetricsSink(MetricsSink):
    """Loop :class:`MetricsSink` that tallies the judge's token usage / cost."""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    consistency_rejections: int = 0

    def record_generation(self, result: GenerationResult) -> None:
        self.calls += 1
        self.prompt_tokens += result.usage.prompt_tokens
        self.completion_tokens += result.usage.completion_tokens
        self.reasoning_tokens += result.usage.reasoning_tokens
        if result.cost_usd is not None:
            self.cost_usd += result.cost_usd

    def record_tool_call(self) -> None:
        self.tool_calls += 1

    def snapshot(self) -> JudgeUsage:
        return JudgeUsage(
            calls=self.calls,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            reasoning_tokens=self.reasoning_tokens,
            cost_usd=self.cost_usd,
            tool_calls=self.tool_calls,
            consistency_rejections=self.consistency_rejections,
        )


# ---------------------------------------------------------------------------
# Termination — stop when submit_report is called; capture its args
# ---------------------------------------------------------------------------


@dataclass
class _SubmitReportTermination:
    """Terminate the judge loop the moment ``submit_report`` is in the tool calls.

    Captures the *first* ``submit_report`` call's arguments and its call id so the
    caller can parse the arguments and — on a validation rejection — answer that
    call with a ``role=tool`` result on the retry. The loop appends the assistant
    message before consulting this policy, so the call is already recorded in
    ``messages`` for the audit transcript.
    """

    captured_args: dict[str, Any] | None = field(default=None, init=False)
    captured_call_id: str | None = field(default=None, init=False)

    def __call__(
        self, result: GenerationResult, turn: int, messages: list[Message]
    ) -> TerminationDecision | None:
        for tc in result.tool_calls:
            if tc.name == SUBMIT_REPORT_TOOL_NAME:
                self.captured_args = dict(tc.arguments or {})
                self.captured_call_id = tc.id
                return TerminationDecision(
                    reason=TerminationReason.AGENT_DONE,
                    system_message="submit_report received; judge terminating.",
                    status=TrialStatus.COMPLETED,
                )
        return None


#: Tool result for a non-``submit_report`` call that shared the terminating turn.
#: Termination fires the instant ``submit_report`` appears, before any tool runs
#: (``loop.py``: ``should_terminate`` precedes ``_execute_tool_calls``), so the
#: sibling genuinely never executed — this is an honest "not run" note, not a
#: fabricated tool output, and it nudges the judge to read before submitting.
_SIBLING_NOT_EXECUTED = (
    "not executed: submit_report ended the turn; gather evidence with your read "
    "tools *before* calling submit_report."
)


def _answer_terminating_submit_report(
    messages: list[Message], captured_call_id: str, rejection: str
) -> None:
    """Rewrite the retry tail into a provider-valid tool-call/tool-result cycle.

    Locates the assistant message bearing ``captured_call_id`` (the terminating
    ``submit_report`` turn), drops the loop's trailing ``"submit_report received;
    judge terminating."`` system message (false on a continued run and what
    breaks tool-result adjacency), then answers **every** ``tool_call_id`` on that
    turn with an adjacent ``role=tool`` result: the ``submit_report`` id carries
    ``rejection``; each sibling id carries :data:`_SIBLING_NOT_EXECUTED`. No
    non-tool message separates the assistant call from its (contiguous) results,
    which is what OpenAI/Azure-family providers require.
    """
    asst_idx = next(
        (
            i
            for i in range(len(messages) - 1, -1, -1)
            if messages[i].tool_calls
            and any(tc.id == captured_call_id for tc in messages[i].tool_calls)
        ),
        None,
    )
    if asst_idx is None:
        raise RuntimeError(
            "Judge retry invariant violated: no assistant message bears the "
            f"terminating submit_report call id {captured_call_id!r}."
        )
    terminating = messages[asst_idx]
    del messages[asst_idx + 1 :]
    for tc in terminating.tool_calls or []:
        content = rejection if tc.id == captured_call_id else _SIBLING_NOT_EXECUTED
        messages.append(Message(role=MessageRole.TOOL, tool_call_id=tc.id, content=content))


# ---------------------------------------------------------------------------
# Prompt construction (narrow input surface, by construction)
# ---------------------------------------------------------------------------

#: The judge's grading stance. A custom ``system_prompt`` replaces this body; the
#: marker contract below is always appended, so a custom voice can never break
#: ``submit_report`` validation.
_JUDGE_SYSTEM_PROMPT_BODY = (
    "You are a strict, evidence-based grading judge. You evaluate an AI agent's "
    "work against a rubric of independent criteria. Each criterion is "
    "self-contained: its text states everything that must be checked. Grade each "
    "criterion exactly as written — nothing more, nothing less. Do not import "
    "outside expectations, and do not excuse a failure for reasons the criterion "
    "does not name. Read each rubric carefully, check the real conversation and "
    "tool calls and don't overthink your decision. Apply criteria - make "
    "decision. Your main evidence is the agent's transcript and, when provided, a "
    "database state diff shown below, plus any read-only tools you are given. If "
    "the transcript and diff settle every criterion, call submit_report without "
    "using tools. Use the read-only tools only for evidence the provided context "
    "does not settle — for example absence or invariant checks, or full final "
    "values not shown in the transcript. Never guess and it's better to call the "
    "tool if you are not sure. A criterion passes only on positive evidence that "
    "the described behavior occurred as stated. If the behavior a criterion "
    "describes never occurred in the trajectory, the criterion FAILS — unless the "
    "criterion's own text explicitly states that it passes when the situation "
    "never arises."
)

#: The enforced output contract — the marker form ``parse_submit_report`` validates.
#: Appended to every judge system prompt (default or custom); the sole source of
#: the marker sentence.
_JUDGE_MARKER_CONTRACT = (
    "For every criterion, write the evidence-based justification "
    "first and commit the verdict after it; end each justification with a final "
    "line 'VERDICT: MET' or 'VERDICT: NOT MET' (binary) / 'SCORE: <value in "
    "[0,1]>' (graded), and make that criterion's verdict field match it. When you "
    "have judged every criterion, call submit_report exactly once."
)

_JUDGE_SYSTEM_PROMPT = f"{_JUDGE_SYSTEM_PROMPT_BODY} {_JUDGE_MARKER_CONTRACT}"


def _compose_judge_system_prompt(custom_system_prompt: str | None) -> str:
    """Compose the judge system prompt, always ending with the marker contract.

    ``None`` yields the byte-for-byte default prompt; a custom body replaces the
    default grading stance while the marker contract stays appended, so
    ``submit_report`` validation can never be silently broken.
    """
    if custom_system_prompt is None:
        return _JUDGE_SYSTEM_PROMPT
    return f"{custom_system_prompt.strip()}\n\n{_JUDGE_MARKER_CONTRACT}"


def _format_transcript(transcript: list[dict[str, Any]]) -> str:
    """Render the agent transcript to a compact, judge-readable string.

    Receives the same ``llm_messages`` list the runner already decoded for
    grading (role/content/tool_calls dicts). Tool calls are summarised inline so
    the judge sees what the agent *did*, not just what it said.
    """
    lines: list[str] = []
    for msg in transcript:
        role = str(msg.get("role", "?")).upper()
        content = msg.get("content")
        if isinstance(content, list):
            content = " ".join(
                block.get("text", "") for block in content if isinstance(block, dict)
            )
        content = (content or "").strip()
        if content:
            lines.append(f"{role}: {content}")
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", tc) if isinstance(tc, dict) else {}
            name = fn.get("name", "?")
            args = fn.get("arguments", "")
            lines.append(f"  -> tool_call {name}({args})")
        if msg.get("tool_call_id") and not content:
            lines.append(f"{role}: (tool result)")
    return "\n".join(lines) if lines else "(empty transcript)"


def _build_rubric_brief(rubric: Rubric) -> str:
    """Render the rubric (reference + per-criterion expected) for the judge.

    The per-criterion pass-conditions are *also* inlined in the ``submit_report``
    schema (see ``build_submit_report_tool``); this brief gives the judge the
    holistic picture (overall reference + the list it must score) up front.
    """
    parts: list[str] = []
    if rubric.reference:
        parts.append(f"Reference (author-written ground truth):\n{rubric.reference}\n")
    parts.append("Criteria to evaluate:")
    for c in rubric.criteria:
        flags = [c.kind]
        if c.required:
            flags.append("REQUIRED — failing this fails the whole rubric")
        expected = f"\n    expected: {c.expected}" if c.expected else ""
        parts.append(f"  - [{c.id}] ({', '.join(flags)}) {c.description}{expected}")
    return "\n".join(parts)


def _serialize_judge_transcript(messages: list[Message]) -> tuple[dict[str, Any], ...]:
    """Serialize the judge's own loop messages to plain dicts for the audit bundle.

    Captures role / content / tool_calls / tool_call_id so a reviewer can replay
    the judge's reasoning — what it inspected and how it scored — out of the
    sidecar ``judge_trajectory.yaml``. JSON/YAML-safe primitives only.
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        entry: dict[str, Any] = {
            "role": msg.role.value if hasattr(msg.role, "value") else str(msg.role),
            "content": msg.content,
        }
        if msg.tool_calls:
            entry["tool_calls"] = [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in msg.tool_calls
            ]
        if msg.tool_call_id:
            entry["tool_call_id"] = msg.tool_call_id
        out.append(entry)
    return tuple(out)


def _build_opening_message(
    agent_system_prompt: str,
    transcript: list[dict[str, Any]],
    state_diff: str | None = None,
    *,
    include_agent_system_prompt: bool = True,
) -> str:
    """Inject the agent's policy (system prompt) + transcript into the judge context.

    When a ``state_diff`` is supplied (the ``initial → final`` delta of the DB
    state — exactly what the agent changed), it is injected as the judge's
    default view of the outcome. It is deliberately NOT the trial-vs-golden diff:
    it reveals nothing about the expected answer, only the agent's own edits. The
    judge is steered to read the diff first and reach for ``get_db_state`` /
    ``query_db`` only to confirm post-conditions the diff does not settle.

    When ``include_agent_system_prompt`` is ``False`` the agent-policy section is
    omitted from the evidence entirely (not blanked to a placeholder), regardless
    of the ``agent_system_prompt`` content — self-contained rubrics that do not
    want the agent's framing in the judge's context.
    """
    policy_block = (
        f"The agent under evaluation operated under this policy / system prompt:\n"
        f"---\n{agent_system_prompt.strip()}\n---\n\n"
        if include_agent_system_prompt and agent_system_prompt and agent_system_prompt.strip()
        else ""
    )
    if state_diff and state_diff.strip():
        state_block = (
            "Below is a diff of the database state the agent changed "
            "(initial → final): the rows it added, removed, or modified, plus "
            "which tables it left untouched. Use it as your starting point for "
            "what the agent did.\n"
            f"{state_diff.strip()}\n\n"
        )
        closing = (
            "The diff shows changes, not the complete final state. For criteria "
            "about what must NOT change, invariants, absence, or the quality of a "
            "full final value, inspect the relevant state directly with query_db "
            "(or get_db_state as a last resort). Then grade each criterion and "
            "call submit_report."
        )
    else:
        state_block = ""
        closing = (
            "Use your read-only tools to inspect the final state, then grade each "
            "criterion and call submit_report."
        )
    return (
        f"{policy_block}"
        "Here is the full transcript of the agent's interaction:\n"
        "===== TRANSCRIPT =====\n"
        f"{_format_transcript(transcript)}\n"
        "===== END TRANSCRIPT =====\n\n"
        f"{state_block}"
        f"{closing}"
    )


# ---------------------------------------------------------------------------
# Tool assembly — harness-owned read-only allowlist
# ---------------------------------------------------------------------------


def _build_judge_registry(
    rubric: Rubric,
    *,
    db_reader: DBReader | None,
    kb_search: KnowledgeSearch | None,
    extra_read_tools: list[Tool] | None,
    workspace_dir: Path | None,
    disable_knowledge_search: bool,
    logger: StructuredLogger,
) -> tuple[ToolRegistry, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Build the read-only tool registry offered to the judge.

    Returns ``(registry, kb_offered, kb_withheld, read_tools_offered)`` —
    ``read_tools_offered`` being the non-KB read surface (``get_db_state`` /
    ``query_db`` / ``read_file`` / any non-KB ``extra_read_tools`` entry) an
    offline replay must shim. A candidate tool is
    knowledge-search iff it carries the declared ``is_knowledge_search`` tag
    (``SearchKbTool`` intrinsically; a ``search_policy`` ``DelegatingReadTool``
    when the runner tags it) — classification is by that tag, NEVER by tool name,
    so a future KB backend is covered by declaring the tag and a non-KB read tool
    in ``extra_read_tools`` is never gated by accident.

    * ``kb_offered`` — KB-tagged tools actually registered (the issue-#95
      observability signal: which knowledge base, if any, the judge could read —
      the SAME one the agent had).
    * ``kb_withheld`` — KB-tagged tools omitted because ``disable_knowledge_search``
      is set. They never enter ``get_schemas()``; the tool is absent, not stubbed.

    Which tools are offered, and when:

    * ``submit_report`` — ALWAYS (the terminal rubric tool, schema from the rubric).
    * ``get_db_state`` / ``query_db`` — when a ``db_reader`` is supplied (the task
      routes state through the DB service). Strictly read-only, never KB.
    * ``search_kb`` — iff a ``kb_search`` backend was resolved for this trial and
      knowledge search is not disabled. Faithful gating: the agent had a KB tool
      over the per-trial index ⇒ the judge gets the SAME KB; no backend ⇒ no tool.
    * ``extra_read_tools`` — ready-made read-only tools the runner supplies for
      this trial (e.g. a passthrough wrapping the agent's reconstructed
      ``search_policy`` TypeSense tool). Registered verbatim under their own names;
      the KB-tagged ones are withheld when disabled, non-KB ones always kept and
      recorded in ``read_tools_offered`` so a replay of the resulting bundle
      re-offers them.
    * ``read_file`` — only when ``workspace_dir`` exists (the agent produced files).
    """
    registry = ToolRegistry()
    registry.register(SubmitReportTool(build_submit_report_tool(rubric)))

    offered = [SUBMIT_REPORT_TOOL_NAME]
    read_tools_offered: list[str] = []
    if db_reader is not None:
        registry.register(GetDbStateTool(db_reader))
        registry.register(QueryDbTool(db_reader))
        offered += ["get_db_state", "query_db"]
        read_tools_offered += ["get_db_state", "query_db"]

    kb_candidates: list[Tool] = []
    if kb_search is not None:
        kb_candidates.append(SearchKbTool(kb_search))
    kb_candidates.extend(extra_read_tools or [])

    kb_offered: list[str] = []
    kb_withheld: list[str] = []
    for tool in kb_candidates:
        if getattr(tool, "is_knowledge_search", False) and disable_knowledge_search:
            kb_withheld.append(tool.name)
            continue
        registry.register(tool)
        offered.append(tool.name)
        if getattr(tool, "is_knowledge_search", False):
            kb_offered.append(tool.name)
        else:
            read_tools_offered.append(tool.name)

    if workspace_dir is not None and workspace_dir.exists():
        registry.register(ReadFileTool(workspace_dir))
        offered.append("read_file")
        read_tools_offered.append("read_file")

    logger.info(
        "Judge read-only tools assembled",
        tools=offered,
        kb_tools=kb_offered,
        kb_withheld=kb_withheld,
    )
    return registry, tuple(kb_offered), tuple(kb_withheld), tuple(read_tools_offered)


def model_config_from_ref(model_ref: str) -> ModelConfig:
    """Split a ``model_ref`` into ``provider`` / ``name`` (first ``/`` only).

    Boundary helper for the only places that still carry the judge model as a
    string — the CLI ``--judge-model`` flag and the ``rubric-calibrator``
    ``--model-ref`` option. The judge's own runtime path takes a full
    ``ModelConfig`` (run-level), never a string.

    Matches ``BaseAdapter.grade`` and ``LLMClient._format_model_name``: e.g.
    ``openrouter/anthropic/claude-sonnet-4.5`` → provider ``openrouter``, name
    ``anthropic/claude-sonnet-4.5``. ``temperature=0.0`` for grading stability.
    """
    if "/" not in model_ref:
        raise ValueError(
            f"Judge model_ref {model_ref!r} must be '<provider>/<model>'; no '/' found."
        )
    provider, name = model_ref.split("/", 1)
    return ModelConfig(provider=provider, name=name, temperature=0.0)


# ---------------------------------------------------------------------------
# The judge seam — Protocol + production impl + in-memory fixture
# ---------------------------------------------------------------------------


@runtime_checkable
class Judge(Protocol):
    """Grade one trial's evidence into a :class:`JudgeResult`.

    The per-trial *evidence* surface only. How a judge is built — model, budgets,
    injected client, logger — is a construction-time concern of the concrete impl,
    NOT part of this contract, so a variant that ignores live evidence (offline
    replay) need not accept a ``model_config``. Deliberately narrow: never the
    deterministic-oracle fields (``golden_actions`` / ``expect_initial_state`` /
    ``jsonpath_checks`` / ``grading_config``) — they cannot leak in because they
    are not on ``run()``.
    """

    def run(
        self,
        *,
        rubric: Rubric,
        agent_system_prompt: str,
        transcript: list[dict[str, Any]],
        db_reader: DBReader | None = None,
        kb_search: KnowledgeSearch | None = None,
        extra_read_tools: list[Tool] | None = None,
        workspace_dir: Path | None = None,
        state_diff: str | None = None,
    ) -> JudgeResult: ...


class LLMJudge:
    """Production :class:`Judge`: the read-only agentic rubric judge over an LLM.

    Construction-time config is *how to run the LLM judge* — the run-level
    ``model_config``, the turn / wall-time / retry budgets, ``disable_knowledge_search``
    (withhold every KB-tagged tool from the judge's surface, per ADR-0019), an
    optional ``custom_system_prompt`` (replace the default grading-stance body; the
    marker contract is always appended), ``include_agent_system_prompt`` (embed the
    agent's policy in the judge's opening-message evidence — default on; gate off for
    self-contained rubrics), an
    optionally injected ``llm_client`` (tests pass a scripted client; production
    passes ``None`` and the judge builds one ``LLMClient(model_config)`` per
    :meth:`run`), and the logger. :meth:`run` carries only the per-trial evidence.
    """

    def __init__(
        self,
        model_config: ModelConfig,
        *,
        max_turns: int = DEFAULT_JUDGE_MAX_TURNS,
        episode_timeout_s: int = DEFAULT_JUDGE_EPISODE_TIMEOUT_S,
        submit_report_retries: int = DEFAULT_SUBMIT_REPORT_RETRIES,
        disable_knowledge_search: bool = False,
        custom_system_prompt: str | None = None,
        include_agent_system_prompt: bool = True,
        llm_client: LLMClient | None = None,
        logger: StructuredLogger | None = None,
    ) -> None:
        self._model_config = model_config
        self._max_turns = max_turns
        self._episode_timeout_s = episode_timeout_s
        self._submit_report_retries = submit_report_retries
        self._disable_knowledge_search = disable_knowledge_search
        self._custom_system_prompt = custom_system_prompt
        self._include_agent_system_prompt = include_agent_system_prompt
        self._llm_client = llm_client
        self._logger = logger

    def run(
        self,
        *,
        rubric: Rubric,
        agent_system_prompt: str,
        transcript: list[dict[str, Any]],
        db_reader: DBReader | None = None,
        kb_search: KnowledgeSearch | None = None,
        extra_read_tools: list[Tool] | None = None,
        workspace_dir: Path | None = None,
        state_diff: str | None = None,
    ) -> JudgeResult:
        """Run the read-only agentic rubric judge and return its verdict.

        Narrow input surface: ``{agent_system_prompt, transcript, rubric,
        read-tools, state_diff}`` only. Never receives the deterministic-oracle
        fields of ``GradingConfig``. ``state_diff`` is the ``initial → final``
        delta of the agent's own edits (not the trial-vs-golden diff) — it reveals
        nothing about the expected answer, so it does not bias the judge.

        Fail-loud: any judge malfunction — repeated malformed ``submit_report``
        past the configured ``submit_report_retries``, turn / wall-time
        exhaustion, or an LLM/tool error classified terminal by the loop — yields
        :data:`JudgeStatus.ERRORED` with NO numeric score. There is no path that
        returns ``0.0`` / ``0.5`` on failure.
        """
        logger = self._logger or get_logger("rubric_judge")
        metrics = _JudgeMetricsSink()

        client: LLMClient
        if self._llm_client is not None:
            client = self._llm_client
        else:
            client = LLMClient(self._model_config)

        registry, kb_tools_offered, kb_tools_withheld, read_tools_offered = _build_judge_registry(
            rubric,
            db_reader=db_reader,
            kb_search=kb_search,
            extra_read_tools=extra_read_tools,
            workspace_dir=workspace_dir,
            disable_knowledge_search=self._disable_knowledge_search,
            logger=logger,
        )
        tool_executor = ToolExecutor(registry)
        tool_schemas = registry.get_schemas(sanitize=False)

        termination = _SubmitReportTermination()
        loop = ToolCallingLoop(
            llm_client=client,
            tool_executor=tool_executor,
            tool_schemas=tool_schemas,
            # Bounded by max_turns + wall-time (episode_timeout_s). There is no
            # per-turn loop timeout; the per-call LLM timeout lives in LLMClient.
            config=LoopConfig(max_turns=self._max_turns, episode_timeout_s=self._episode_timeout_s),
            metrics=metrics,
            should_terminate=termination,
            logger=logger,
            user_turn=None,
        )

        messages: list[Message] = [
            Message(
                role=MessageRole.USER,
                content=_build_opening_message(
                    agent_system_prompt,
                    transcript,
                    state_diff,
                    include_agent_system_prompt=self._include_agent_system_prompt,
                ),
            )
        ]
        rubric_brief = _build_rubric_brief(rubric)
        system_prompt = (
            f"{_compose_judge_system_prompt(self._custom_system_prompt)}\n\n{rubric_brief}"
        )

        attempts = 0
        while True:
            try:
                outcome = loop.run(system_prompt, messages, start_time=time.time())
            except Exception as exc:  # noqa: BLE001 — fail loud, never score on judge crash
                logger.error("Judge loop raised", error=str(exc), error_type=type(exc).__name__)
                return _errored(
                    metrics,
                    f"Judge loop crashed: {type(exc).__name__}: {exc}",
                    messages,
                    kb_tools_offered,
                    kb_tools_withheld,
                    knowledge_search_disabled=self._disable_knowledge_search,
                    custom_system_prompt=self._custom_system_prompt is not None,
                    include_agent_system_prompt=self._include_agent_system_prompt,
                    read_tools_offered=read_tools_offered,
                    state_diff=state_diff,
                )

            if termination.captured_args is None:
                # Loop ended without submit_report — turn / wall-time / API error.
                return _errored(
                    metrics,
                    f"Judge did not call submit_report "
                    f"(termination={outcome.termination_reason}, status={outcome.status}).",
                    messages,
                    kb_tools_offered,
                    kb_tools_withheld,
                    knowledge_search_disabled=self._disable_knowledge_search,
                    custom_system_prompt=self._custom_system_prompt is not None,
                    include_agent_system_prompt=self._include_agent_system_prompt,
                    read_tools_offered=read_tools_offered,
                    state_diff=state_diff,
                )

            try:
                results = parse_submit_report(termination.captured_args, rubric)
                aggregate = aggregate_rubric(rubric, results)
            except SubmitReportValidationError as exc:
                if isinstance(exc, VerdictConsistencyError):
                    metrics.consistency_rejections += 1
                attempts += 1
                if attempts > self._submit_report_retries:
                    logger.error(
                        "Judge submit_report invalid after retries; erroring",
                        attempts=attempts,
                        error=str(exc),
                    )
                    return _errored(
                        metrics,
                        f"submit_report invalid after {self._submit_report_retries} retries: {exc}",
                        messages,
                        kb_tools_offered,
                        kb_tools_withheld,
                        knowledge_search_disabled=self._disable_knowledge_search,
                        custom_system_prompt=self._custom_system_prompt is not None,
                        include_agent_system_prompt=self._include_agent_system_prompt,
                        read_tools_offered=read_tools_offered,
                        state_diff=state_diff,
                    )
                logger.warning(
                    "Judge submit_report invalid; re-prompting", attempt=attempts, error=str(exc)
                )
                if termination.captured_call_id is None:
                    raise RuntimeError(
                        "Judge retry invariant violated: submit_report was rejected but "
                        "no terminating call id was captured."
                    )
                rejection = (
                    f"Your submit_report was rejected: {exc}\n"
                    "Fix the issue and call submit_report again with a verdict "
                    "and justification for every criterion."
                )
                _answer_terminating_submit_report(messages, termination.captured_call_id, rejection)
                termination.captured_args = None
                termination.captured_call_id = None
                continue

            logger.info(
                "Judge completed",
                score=aggregate.score,
                gate_failed=aggregate.gate_failed,
                failed_required=list(aggregate.failed_required_ids),
            )
            reasons = _build_reasons(
                termination.captured_args,
                aggregate.failed_required_ids,
                kb_tools_offered,
                kb_tools_withheld,
                self._disable_knowledge_search,
            )
            return JudgeResult(
                status=JudgeStatus.COMPLETED,
                usage=metrics.snapshot(),
                reasons=reasons,
                score=aggregate.score,
                binary_pass=aggregate.binary_pass,
                gate_failed=aggregate.gate_failed,
                criterion_results=tuple(results),
                failed_required_ids=aggregate.failed_required_ids,
                kb_tools_offered=kb_tools_offered,
                kb_tools_withheld=kb_tools_withheld,
                knowledge_search_disabled=self._disable_knowledge_search,
                custom_system_prompt=self._custom_system_prompt is not None,
                include_agent_system_prompt=self._include_agent_system_prompt,
                read_tools_offered=read_tools_offered,
                state_diff=state_diff,
                transcript=_serialize_judge_transcript(messages),
            )


def _errored(
    metrics: _JudgeMetricsSink,
    reasons: str,
    messages: list[Message],
    kb_tools_offered: tuple[str, ...],
    kb_tools_withheld: tuple[str, ...],
    *,
    knowledge_search_disabled: bool,
    custom_system_prompt: bool,
    include_agent_system_prompt: bool,
    read_tools_offered: tuple[str, ...],
    state_diff: str | None,
) -> JudgeResult:
    """Build a fail-loud ERRORED result — no score, no criterion results.

    Carries the partial judge transcript: when the judge breaks, its messages
    so far are the most useful debugging artifact. The ``Judge KB: …`` note is
    appended even on error so a reviewer can see whether a KB-blind judge was a
    factor in the failure. Echoes the read-tool surface + state_diff
    it was handed so an offline replay of an errored trial is still reconstructable.
    """
    kb_note = _kb_note(kb_tools_offered, kb_tools_withheld, knowledge_search_disabled)
    return JudgeResult(
        status=JudgeStatus.ERRORED,
        usage=metrics.snapshot(),
        reasons=f"{reasons} | {kb_note}",
        score=None,
        binary_pass=None,
        kb_tools_offered=kb_tools_offered,
        kb_tools_withheld=kb_tools_withheld,
        knowledge_search_disabled=knowledge_search_disabled,
        custom_system_prompt=custom_system_prompt,
        include_agent_system_prompt=include_agent_system_prompt,
        read_tools_offered=read_tools_offered,
        state_diff=state_diff,
        transcript=_serialize_judge_transcript(messages),
    )


def _kb_note(
    kb_tools_offered: tuple[str, ...],
    kb_tools_withheld: tuple[str, ...],
    knowledge_search_disabled: bool,
) -> str:
    """The human-readable "graded with / without KB" signal (issue #95).

    e.g. ``Judge KB: search_policy`` / ``Judge KB: search_kb`` / ``Judge KB:
    none offered``. When knowledge search was disabled by config and the agent
    actually had a KB tool to withhold, reads ``Judge KB: none offered (disabled
    by config)`` — distinguishing a deliberate gate from a rubric that simply
    needed no KB. Observability, not an error.
    """
    if knowledge_search_disabled and kb_tools_withheld:
        return "Judge KB: none offered (disabled by config)"
    if kb_tools_offered:
        return f"Judge KB: {', '.join(kb_tools_offered)}"
    return "Judge KB: none offered"


def _build_reasons(
    tool_args: dict[str, Any],
    failed_required_ids: tuple[str, ...],
    kb_tools_offered: tuple[str, ...],
    kb_tools_withheld: tuple[str, ...],
    knowledge_search_disabled: bool,
) -> str:
    """Compose the judge's human-readable reasons from its overall summary + gate.

    Always ends with the ``Judge KB: …`` note (issue #95) so the grade output
    makes visible which knowledge base — if any — the judge could read.
    """
    overall = tool_args.get("reasons")
    parts: list[str] = []
    if isinstance(overall, str) and overall.strip():
        parts.append(overall.strip())
    if failed_required_ids:
        parts.append(f"FAILED required criteria: {', '.join(failed_required_ids)}")
    parts.append(_kb_note(kb_tools_offered, kb_tools_withheld, knowledge_search_disabled))
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# InMemoryJudge — non-LLM test fixture (records calls, returns synthetic verdicts)
# ---------------------------------------------------------------------------


@dataclass
class JudgeCallLog:
    """Records what an :class:`InMemoryJudge` was asked to grade.

    Tests assert on this directly instead of mocking the judge. Each entry
    captures the rubric's criterion ids, the transcript length, and which evidence
    seams were present on the ``run()`` call.
    """

    runs: list[dict[str, Any]] = field(default_factory=list)


class InMemoryJudge:
    """Non-LLM :class:`Judge` fixture — deterministic verdicts, no inference.

    Records every ``run()`` on :attr:`call_log` and returns a configurable
    :class:`JudgeResult`. By default every criterion is met with score ``1.0``,
    aggregated through the real :func:`aggregate_rubric` so the score math is
    authentic. Per-criterion verdicts override the default (a ``bool`` is a binary
    met/not-met verdict; a ``float`` is a graded score); ``force_errored`` makes
    ``run()`` return :data:`JudgeStatus.ERRORED` with no score. Production never
    constructs this — it is a test seam only.
    """

    def __init__(
        self,
        *,
        verdicts: dict[str, bool | float] | None = None,
        force_errored: bool = False,
        errored_reasons: str = "InMemoryJudge forced error",
    ) -> None:
        self._verdicts = dict(verdicts or {})
        self._force_errored = force_errored
        self._errored_reasons = errored_reasons
        self.call_log = JudgeCallLog()

    def run(
        self,
        *,
        rubric: Rubric,
        agent_system_prompt: str,
        transcript: list[dict[str, Any]],
        db_reader: DBReader | None = None,
        kb_search: KnowledgeSearch | None = None,
        extra_read_tools: list[Tool] | None = None,
        workspace_dir: Path | None = None,
        state_diff: str | None = None,
    ) -> JudgeResult:
        self.call_log.runs.append(
            {
                "criterion_ids": tuple(c.id for c in rubric.criteria),
                "transcript_len": len(transcript),
                "db_reader": db_reader is not None,
                "kb_search": kb_search is not None,
                "extra_read_tools": bool(extra_read_tools),
                "workspace_dir": workspace_dir is not None,
                "state_diff": state_diff is not None,
            }
        )
        if self._force_errored:
            return JudgeResult(
                status=JudgeStatus.ERRORED,
                usage=JudgeUsage(),
                reasons=self._errored_reasons,
                score=None,
                binary_pass=None,
            )

        results = [self._criterion_result(c) for c in rubric.criteria]
        aggregate = aggregate_rubric(rubric, results)
        return JudgeResult(
            status=JudgeStatus.COMPLETED,
            usage=JudgeUsage(),
            reasons="InMemoryJudge synthetic verdict",
            score=aggregate.score,
            binary_pass=aggregate.binary_pass,
            gate_failed=aggregate.gate_failed,
            criterion_results=tuple(results),
            failed_required_ids=aggregate.failed_required_ids,
        )

    def _criterion_result(self, criterion: Criterion) -> CriterionResult:
        """Map the configured (or default-``True``) verdict to a per-criterion result."""
        verdict = self._verdicts.get(criterion.id, True)
        if isinstance(verdict, bool):
            met = verdict
            score = 1.0 if verdict else 0.0
        else:
            score = float(verdict)
            met = score >= GRADED_MET_THRESHOLD
        return CriterionResult(
            id=criterion.id,
            met=met,
            score=score,
            justification=f"InMemoryJudge verdict for {criterion.id}",
        )


__all__ = [
    "DBReader",
    "KnowledgeSearch",
    "Judge",
    "JudgeResult",
    "JudgeStatus",
    "JudgeUsage",
    "JudgeCallLog",
    "LLMJudge",
    "InMemoryJudge",
    "model_config_from_ref",
]
