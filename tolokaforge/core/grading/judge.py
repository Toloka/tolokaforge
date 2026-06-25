"""Read-only agentic rubric judge — Stage 4 of ``plans/rubric_grading.md``.

The judge is *"a solo read-only grader — the shared :class:`ToolCallingLoop`
with no user simulator, a harness-owned read-only toolset, and a rubric-shaped
``submit_report`` tool — run over the live final state, failing loud on its own
malfunction."*

Design (locked decisions, see the plan):

* **Separate fixed judge model.** The judge constructs its own
  :class:`~tolokaforge.core.llm.client.LLMClient` from the rubric's ``model_ref``
  via the agent's ``build_capabilities`` path (so tool schemas/calls are
  provider-correct for Gemini / GPT-5 / …). API keys are read through
  :class:`SecretManager` inside ``LLMClient`` — never ``os.environ`` here.
* **Harness-owned read-only allowlist.** NOT the agent's tools, NO category
  filter, NO DB clone. The judge gets read-only DB tools, ``search_kb`` when the
  task is RAG-backed, file readers only when a real workspace exists, and the
  rubric-derived ``submit_report``.
* **Narrow input surface.** :func:`run_rubric_judge` receives only
  ``{agent_system_prompt, transcript, rubric (incl. reference/expected),
  read-tools}`` — never ``golden_actions`` / ``expected_hash`` /
  ``jsonpath_checks``.
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
from typing import Any, Protocol

from tolokaforge.core.grading.judge_tools import (
    GetDbStateTool,
    QueryDbTool,
    ReadFileTool,
    SearchKbTool,
    SubmitReportTool,
)
from tolokaforge.core.grading.rubric import (
    SUBMIT_REPORT_TOOL_NAME,
    SubmitReportValidationError,
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
from tolokaforge.runner.models import CriterionResult, Rubric
from tolokaforge.tools.registry import ToolExecutor, ToolRegistry

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
    """The judge's own accounting — recorded to the output bundle (plan: judge cost)."""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0


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
        )


# ---------------------------------------------------------------------------
# Termination — stop when submit_report is called; capture its args
# ---------------------------------------------------------------------------


@dataclass
class _SubmitReportTermination:
    """Terminate the judge loop the moment ``submit_report`` is in the tool calls.

    Captures the *first* ``submit_report`` call's arguments so the caller can
    parse them after the loop returns. The loop appends the assistant message
    before consulting this policy, so the call is already recorded in
    ``messages`` for the audit transcript.
    """

    captured_args: dict[str, Any] | None = field(default=None, init=False)

    def __call__(
        self, result: GenerationResult, turn: int, messages: list[Message]
    ) -> TerminationDecision | None:
        for tc in result.tool_calls:
            if tc.name == SUBMIT_REPORT_TOOL_NAME:
                self.captured_args = dict(tc.arguments or {})
                return TerminationDecision(
                    reason=TerminationReason.AGENT_DONE,
                    system_message="submit_report received; judge terminating.",
                    status=TrialStatus.COMPLETED,
                )
        return None


# ---------------------------------------------------------------------------
# Prompt construction (narrow input surface, by construction)
# ---------------------------------------------------------------------------

_JUDGE_SYSTEM_PROMPT = (
    "You are a strict, evidence-based grading judge. You evaluate an AI agent's "
    "work against a rubric of independent criteria. You have READ-ONLY tools to "
    "inspect the final database state, the knowledge base, and any workspace "
    "files the agent produced. Use them to gather evidence before scoring — never "
    "guess. Evaluate each criterion on its own merits using the provided rubric "
    "reference where given. When you have enough evidence, call submit_report "
    "exactly once with a verdict and an evidence-based justification for every "
    "criterion. Do not call submit_report before you have inspected the relevant "
    "state."
)


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


def _build_opening_message(agent_system_prompt: str, transcript: list[dict[str, Any]]) -> str:
    """Inject the agent's policy (system prompt) + transcript into the judge context."""
    policy_block = (
        f"The agent under evaluation operated under this policy / system prompt:\n"
        f"---\n{agent_system_prompt.strip()}\n---\n\n"
        if agent_system_prompt and agent_system_prompt.strip()
        else ""
    )
    return (
        f"{policy_block}"
        "Here is the full transcript of the agent's interaction:\n"
        "===== TRANSCRIPT =====\n"
        f"{_format_transcript(transcript)}\n"
        "===== END TRANSCRIPT =====\n\n"
        "Use your read-only tools to inspect the final state, then grade each "
        "criterion and call submit_report."
    )


# ---------------------------------------------------------------------------
# Tool assembly — harness-owned read-only allowlist
# ---------------------------------------------------------------------------


def _build_judge_registry(
    rubric: Rubric,
    *,
    db_reader: DBReader | None,
    rag_url: str | None,
    workspace_dir: Path | None,
    logger: StructuredLogger,
) -> ToolRegistry:
    """Build the read-only tool registry offered to the judge.

    Which tools are offered, and when:

    * ``submit_report`` — ALWAYS (the terminal rubric tool, schema from the rubric).
    * ``get_db_state`` / ``query_db`` — when a ``db_reader`` is supplied (the task
      routes state through the DB service). Strictly read-only.
    * ``search_kb`` — when ``rag_url`` is set (the task is RAG-backed).
    * ``read_file`` — only when ``workspace_dir`` exists (the agent produced files).
    """
    registry = ToolRegistry()
    registry.register(SubmitReportTool(build_submit_report_tool(rubric)))

    offered = [SUBMIT_REPORT_TOOL_NAME]
    if db_reader is not None:
        registry.register(GetDbStateTool(db_reader))
        registry.register(QueryDbTool(db_reader))
        offered += ["get_db_state", "query_db"]
    if rag_url:
        registry.register(SearchKbTool(rag_url))
        offered.append("search_kb")
    if workspace_dir is not None and workspace_dir.exists():
        registry.register(ReadFileTool(workspace_dir))
        offered.append("read_file")

    logger.info("Judge read-only tools assembled", tools=offered)
    return registry


def _model_config_from_ref(model_ref: str) -> ModelConfig:
    """Split a ``model_ref`` into ``provider`` / ``name`` (first ``/`` only).

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
# The judge entry point
# ---------------------------------------------------------------------------


def run_rubric_judge(
    *,
    rubric: Rubric,
    model_ref: str,
    agent_system_prompt: str,
    transcript: list[dict[str, Any]],
    db_reader: DBReader | None = None,
    rag_url: str | None = None,
    workspace_dir: Path | None = None,
    max_turns: int = DEFAULT_JUDGE_MAX_TURNS,
    episode_timeout_s: int = DEFAULT_JUDGE_EPISODE_TIMEOUT_S,
    submit_report_retries: int = DEFAULT_SUBMIT_REPORT_RETRIES,
    llm_client: LLMClient | None = None,
    logger: StructuredLogger | None = None,
) -> JudgeResult:
    """Run the read-only agentic rubric judge and return its verdict.

    Narrow input surface: ``{agent_system_prompt, transcript, rubric, read-tools}``
    only. Never receives the deterministic-oracle fields of ``GradingConfig``.

    Fail-loud: any judge malfunction — repeated malformed ``submit_report`` past
    ``submit_report_retries``, turn / wall-time exhaustion, or an LLM/tool error
    classified terminal by the loop — yields :data:`JudgeStatus.ERRORED` with NO
    numeric score. There is no path that returns ``0.0`` / ``0.5`` on failure.

    ``llm_client`` may be injected for tests (a scripted ``LoopLLMClient`` /
    fake); production passes ``None`` and the judge builds one from ``model_ref``.
    """
    logger = logger or get_logger("rubric_judge")
    metrics = _JudgeMetricsSink()

    client: LLMClient
    if llm_client is not None:
        client = llm_client  # type: ignore[assignment]
    else:
        client = LLMClient(_model_config_from_ref(model_ref))

    registry = _build_judge_registry(
        rubric,
        db_reader=db_reader,
        rag_url=rag_url,
        workspace_dir=workspace_dir,
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
        config=LoopConfig(max_turns=max_turns, episode_timeout_s=episode_timeout_s),
        metrics=metrics,
        should_terminate=termination,
        logger=logger,
        user_turn=None,
    )

    messages: list[Message] = [
        Message(
            role=MessageRole.USER,
            content=_build_opening_message(agent_system_prompt, transcript),
        )
    ]
    rubric_brief = _build_rubric_brief(rubric)
    system_prompt = f"{_JUDGE_SYSTEM_PROMPT}\n\n{rubric_brief}"

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
            )

        if termination.captured_args is None:
            # Loop ended without submit_report — turn / wall-time / API error.
            return _errored(
                metrics,
                f"Judge did not call submit_report "
                f"(termination={outcome.termination_reason}, status={outcome.status}).",
                messages,
            )

        try:
            results = parse_submit_report(termination.captured_args, rubric)
            aggregate = aggregate_rubric(rubric, results)
        except SubmitReportValidationError as exc:
            attempts += 1
            if attempts > submit_report_retries:
                logger.error(
                    "Judge submit_report invalid after retries; erroring",
                    attempts=attempts,
                    error=str(exc),
                )
                return _errored(
                    metrics,
                    f"submit_report invalid after {submit_report_retries} retries: {exc}",
                    messages,
                )
            logger.warning(
                "Judge submit_report invalid; re-prompting", attempt=attempts, error=str(exc)
            )
            # Re-prompt: keep the audit trail (assistant + the termination system
            # message the loop appended) and add a corrective user turn.
            messages.append(
                Message(
                    role=MessageRole.USER,
                    content=(
                        f"Your submit_report was rejected: {exc}\n"
                        "Fix the issue and call submit_report again with a verdict "
                        "and justification for every criterion."
                    ),
                )
            )
            termination.captured_args = None
            continue

        logger.info(
            "Judge completed",
            score=aggregate.score,
            gate_failed=aggregate.gate_failed,
            failed_required=list(aggregate.failed_required_ids),
        )
        reasons = _build_reasons(termination.captured_args, aggregate.failed_required_ids)
        return JudgeResult(
            status=JudgeStatus.COMPLETED,
            usage=metrics.snapshot(),
            reasons=reasons,
            score=aggregate.score,
            binary_pass=aggregate.binary_pass,
            gate_failed=aggregate.gate_failed,
            criterion_results=tuple(results),
            failed_required_ids=aggregate.failed_required_ids,
            transcript=_serialize_judge_transcript(messages),
        )


def _errored(metrics: _JudgeMetricsSink, reasons: str, messages: list[Message]) -> JudgeResult:
    """Build a fail-loud ERRORED result — no score, no criterion results.

    Carries the partial judge transcript: when the judge breaks, its messages
    so far are the most useful debugging artifact.
    """
    return JudgeResult(
        status=JudgeStatus.ERRORED,
        usage=metrics.snapshot(),
        reasons=reasons,
        score=None,
        binary_pass=None,
        transcript=_serialize_judge_transcript(messages),
    )


def _build_reasons(tool_args: dict[str, Any], failed_required_ids: tuple[str, ...]) -> str:
    """Compose the judge's human-readable reasons from its overall summary + gate."""
    overall = tool_args.get("reasons")
    parts: list[str] = []
    if isinstance(overall, str) and overall.strip():
        parts.append(overall.strip())
    if failed_required_ids:
        parts.append(f"FAILED required criteria: {', '.join(failed_required_ids)}")
    return " | ".join(parts) if parts else "Judge completed."


__all__ = [
    "DBReader",
    "JudgeResult",
    "JudgeStatus",
    "JudgeUsage",
    "run_rubric_judge",
]
