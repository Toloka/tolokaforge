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
  filter, NO DB clone. The rubric-derived ``submit_report``, plus what the parts of
  ``customization.evidence`` offer (:mod:`~tolokaforge.core.grading.judge_evidence`):
  read-only DB tools, ``search_kb`` when the task is RAG-backed, a file reader only
  when a real workspace exists, ``show_call`` over the numbered transcript.
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
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from tolokaforge.core.grading.judge_evidence import (
    ComposedEvidence,
    compose_evidence,
    format_transcript,
)
from tolokaforge.core.grading.judge_model_provider import JudgeModel
from tolokaforge.core.grading.judge_result import JudgeResult, JudgeStatus, JudgeUsage
from tolokaforge.core.grading.judge_tools import EvidenceOwnedTool, SubmitReportTool
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
from tolokaforge.core.grading.substrate import SubstrateUnreachableError
from tolokaforge.core.judge_prompt import (
    _compose_judge_system_prompt,
    effective_judge_system_prompt,
)
from tolokaforge.core.llm.client import GenerationResult, LLMClient
from tolokaforge.core.llm.usage import sum_known
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
from tolokaforge.core.tool_effects import ToolEffects
from tolokaforge.runner.models import (
    Criterion,
    CriterionResult,
    EvidenceInput,
    JudgeCustomization,
    JudgeEvidence,
    Rubric,
)
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


class _RefusalRecordingDBReader:
    """A :class:`DBReader` that keeps the substrate's refusal of a judge read.

    The judge's tools run under :class:`~tolokaforge.tools.registry.ToolExecutor`,
    which answers any raise with a tool error the judge reads and grades past. A
    substrate that cannot reach the trial's final state is no tool error: a
    verdict must not stand on it. The first :class:`SubstrateUnreachableError` is
    kept here, and :meth:`LLMJudge.run` raises it once the judge loop stops.
    """

    def __init__(self, reader: DBReader) -> None:
        self._reader = reader
        self.refusal: SubstrateUnreachableError | None = None

    def get_state(self, tables: list[str] | None = None) -> dict[str, Any]:
        try:
            return self._reader.get_state(tables)
        except SubstrateUnreachableError as exc:
            self.refusal = self.refusal or exc
            raise

    def query(self, jsonpath: str) -> dict[str, Any]:
        try:
            return self._reader.query(jsonpath)
        except SubstrateUnreachableError as exc:
            self.refusal = self.refusal or exc
            raise

    def raise_refusal(self, usage: JudgeUsage) -> None:
        """Re-raise the kept refusal, carrying what the judge spent before it."""
        if self.refusal is not None:
            self.refusal.judge_usage = usage
            raise self.refusal


# ---------------------------------------------------------------------------
# Metrics sink — accumulates the judge's own usage
# ---------------------------------------------------------------------------


@dataclass
class _JudgeMetricsSink(MetricsSink):
    """Loop :class:`MetricsSink` that tallies the judge's token usage / cost.

    ``billed_cost_usd`` sums the charge each call's
    :class:`~tolokaforge.core.llm.usage.ProviderRawCall` states and turns
    ``None`` for good at the first call that states none (no usage block, a
    route that states no charge).
    """

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    consistency_rejections: int = 0
    billed_cost_usd: float | None = 0.0

    def record_generation(self, result: GenerationResult) -> None:
        self.calls += 1
        self.prompt_tokens += result.usage.prompt_tokens
        self.completion_tokens += result.usage.completion_tokens
        self.reasoning_tokens += result.usage.reasoning_tokens
        if result.cost_usd is not None:
            self.cost_usd += result.cost_usd
        self.billed_cost_usd = sum_known(
            (self.billed_cost_usd, sum_known(call.billed_cost_usd for call in result.usage.calls))
        )

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
            # a judge that made no call has no call stating a charge
            billed_cost_usd=self.billed_cost_usd if self.calls else None,
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


#: Termination fires the instant the terminating tool appears, before any tool
#: runs (``loop.py``: ``should_terminate`` precedes the tool-call funnel), so
#: a sibling call on that same turn genuinely never executed — this returns an
#: honest "not run" note, not a fabricated tool output, naming whichever tool
#: actually ended the turn so the judge isn't told a falsehood about which call
#: pre-empted its siblings.
def _sibling_not_executed(terminating_tool: str) -> str:
    return (
        f"not executed: {terminating_tool} ended the turn; gather evidence with "
        "your read tools *before* calling submit_report."
    )


def _answer_terminating_submit_report(
    messages: list[Message],
    captured_call_id: str,
    rejection: str,
    *,
    terminating_tool: str = "submit_report",
) -> None:
    """Rewrite the retry tail into a provider-valid tool-call/tool-result cycle.

    Locates the assistant message bearing ``captured_call_id`` (the terminating
    ``terminating_tool`` turn), drops the loop's trailing system message
    announcing that termination (false on a continued run and what breaks
    tool-result adjacency), then answers **every** ``tool_call_id`` on that turn
    with an adjacent ``role=tool`` result: the id matching ``captured_call_id``
    carries ``rejection``; each sibling id carries the honest "not executed"
    note naming ``terminating_tool``. No non-tool message separates the
    assistant call from its (contiguous) results, which is what
    OpenAI/Azure-family providers require.
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
        content = (
            rejection if tc.id == captured_call_id else _sibling_not_executed(terminating_tool)
        )
        messages.append(Message(role=MessageRole.TOOL, tool_call_id=tc.id, content=content))


# ---------------------------------------------------------------------------
# Prompt construction (narrow input surface, by construction)
# ---------------------------------------------------------------------------


def _build_rubric_brief(rubric: Rubric) -> str:
    """Render the rubric (reference + per-criterion expected) for the judge.

    The per-criterion pass-conditions are *also* inlined in the ``submit_report``
    schema (see ``build_submit_report_tool``); this brief gives the judge the
    holistic picture (overall reference + the list it must score) up front.

    Graded criteria without an author-written ``expected:`` anchor also get a
    schema-level ``<id>_interpretation`` field the judge must fill BEFORE the
    justification — see ``build_submit_report_tool``. That field commits the
    judge to a specific reading of what "met" looks like for the criterion
    before the reasoning runs, absorbing the between-sample interpretation
    drift that per-sample voting alone does not fix.
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


def model_config_from_ref(model_ref: str) -> ModelConfig:
    """Split a ``model_ref`` into ``provider`` / ``name`` (first ``/`` only).

    Boundary helper for the only places that still carry the judge model as a
    string — the CLI ``--judge-model`` flag and the ``rubric-calibrator``
    ``--model-ref`` option. The judge's own runtime path takes a full
    ``ModelConfig`` (run-level), never a string.

    Matches ``BaseAdapter.grade`` and ``providers.litellm_model_id``: e.g.
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
        extra_read_tools: list[EvidenceOwnedTool] | None = None,
        workspace_dir: Path | None = None,
        state_diff: str | None = None,
    ) -> JudgeResult: ...


class LLMJudge:
    """Production :class:`Judge`: the read-only agentic rubric judge over an LLM.

    Construction-time config is *how to run the LLM judge* — the run-level
    ``model_config``, the trial's ``customization`` (the system-prompt body and the
    parts of its ``evidence``, composed by
    :func:`~tolokaforge.core.grading.judge_evidence.compose_evidence`), the agent
    tools' ``tool_effects`` (read by the ``state_changing_calls`` part), the turn /
    wall-time / retry budgets, an optionally injected ``llm_client`` (any
    :class:`JudgeModel` — tests pass a scripted client; production passes ``None``
    and the judge builds one ``LLMClient(model_config)`` per :meth:`run`), and the
    logger. :meth:`run` carries only the per-trial evidence.

    Two prompt seams, mutually exclusive:

    * ``customization.system_prompt`` — a body fragment that replaces the default
      grading stance; :func:`_compose_judge_system_prompt` appends the marker
      contract at :meth:`run` time.
    * ``explicit_system_prompt`` — the already-composed prompt used verbatim. Bundle
      replay uses this seam so a recorded ``prompts.yaml.judge_prompt`` (composed
      body + marker) reaches the judge without a second marker being appended.

    Passing both raises :class:`ValueError` at construction time.
    """

    def __init__(
        self,
        model_config: ModelConfig,
        *,
        customization: JudgeCustomization | None = None,
        tool_effects: ToolEffects | None = None,
        explicit_system_prompt: str | None = None,
        max_turns: int = DEFAULT_JUDGE_MAX_TURNS,
        episode_timeout_s: int = DEFAULT_JUDGE_EPISODE_TIMEOUT_S,
        submit_report_retries: int = DEFAULT_SUBMIT_REPORT_RETRIES,
        llm_client: JudgeModel | None = None,
        logger: StructuredLogger | None = None,
    ) -> None:
        customization = JudgeCustomization() if customization is None else customization
        if explicit_system_prompt is not None and customization.system_prompt is not None:
            raise ValueError(
                "LLMJudge accepts explicit_system_prompt (a composed prompt used "
                "verbatim, no marker re-appended) OR customization.system_prompt (a body "
                "fragment composed at run time with the marker contract appended); "
                "these seams carry distinct semantics and passing both is a caller "
                "bug — bundle replay uses the former, task customization and "
                "--grading overrides the latter."
            )
        self._model_config = model_config
        self._customization = customization
        self._tool_effects = ToolEffects.empty() if tool_effects is None else tool_effects
        self._explicit_system_prompt = explicit_system_prompt
        self._max_turns = max_turns
        self._episode_timeout_s = episode_timeout_s
        self._submit_report_retries = submit_report_retries
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
        extra_read_tools: list[EvidenceOwnedTool] | None = None,
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

        Raises:
            ValueError: a view of the trial was handed that no enabled part needs
                (:meth:`~tolokaforge.runner.models.JudgeEvidence.needs`) — the caller
                builds only what the evidence needs — or an extra read tool names no
                part that owns it.
            SubstrateUnreachableError: a DB read through the judge's tools could
                not reach the trial's final state. The verdict would rest on a
                state the judge never saw, so none is returned.
        """
        logger = self._logger or get_logger("rubric_judge")
        evidence = self._customization.evidence
        _refuse_views_no_part_needs(
            evidence, state_diff=state_diff, db_reader=db_reader, workspace_dir=workspace_dir
        )
        guarded_reader = _RefusalRecordingDBReader(db_reader) if db_reader is not None else None
        composed = compose_evidence(
            evidence,
            agent_system_prompt=agent_system_prompt,
            transcript=transcript,
            state_diff=state_diff,
            db_reader=guarded_reader,
            kb_search=kb_search,
            extra_read_tools=extra_read_tools or [],
            workspace_dir=workspace_dir,
            tool_effects=self._tool_effects,
        )
        registry = ToolRegistry()
        registry.register(SubmitReportTool(build_submit_report_tool(rubric)))
        for tool in composed.tools:
            registry.register(tool)
        logger.info(
            "Judge read-only tools assembled",
            tools=[SUBMIT_REPORT_TOOL_NAME, *(tool.name for tool in composed.tools)],
            kb_tools=list(composed.kb_tools_offered),
            kb_withheld=list(composed.kb_tools_withheld),
        )
        return self._grade(
            rubric,
            registry,
            composed,
            guarded_reader=guarded_reader,
            state_diff=state_diff,
            logger=logger,
        )

    def _grade(
        self,
        rubric: Rubric,
        registry: ToolRegistry,
        composed: ComposedEvidence,
        *,
        guarded_reader: _RefusalRecordingDBReader | None,
        state_diff: str | None,
        logger: StructuredLogger,
    ) -> JudgeResult:
        """Drive the judge loop over ``registry`` until a valid ``submit_report``."""
        metrics = _JudgeMetricsSink()
        termination = _SubmitReportTermination()
        loop = self._loop(registry, metrics, termination, logger)
        messages: list[Message] = [Message(role=MessageRole.USER, content=composed.opening_message)]
        base_prompt = (
            self._explicit_system_prompt
            if self._explicit_system_prompt is not None
            else _compose_judge_system_prompt(self._customization.system_prompt)
        )
        system_prompt = f"{base_prompt}\n\n{_build_rubric_brief(rubric)}"

        def errored(reasons: str) -> JudgeResult:
            return _build_errored_judge_result(metrics, reasons, messages, composed, state_diff)

        attempts = 0
        while True:
            try:
                outcome = loop.run(system_prompt, messages, start_time=time.time())
            except Exception as exc:  # noqa: BLE001 — fail loud, never score on judge crash
                if guarded_reader is not None:
                    guarded_reader.raise_refusal(metrics.snapshot())
                logger.error("Judge loop raised", error=str(exc), error_type=type(exc).__name__)
                return errored(f"Judge loop crashed: {type(exc).__name__}: {exc}")

            if guarded_reader is not None:
                guarded_reader.raise_refusal(metrics.snapshot())

            if termination.captured_args is None:
                # Loop ended without submit_report — turn / wall-time / API error.
                return errored(
                    f"Judge did not call submit_report "
                    f"(termination={outcome.termination_reason}, status={outcome.status})."
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
                    return errored(
                        f"submit_report invalid after {self._submit_report_retries} retries: {exc}"
                    )
                logger.warning(
                    "Judge submit_report invalid; re-prompting", attempt=attempts, error=str(exc)
                )
                _reprompt_after_rejection(messages, termination, exc)
                continue

            logger.info(
                "Judge completed",
                score=aggregate.score,
                gate_failed=aggregate.gate_failed,
                failed_required=list(aggregate.failed_required_ids),
            )
            return JudgeResult(
                status=JudgeStatus.COMPLETED,
                usage=metrics.snapshot(),
                reasons=_build_judge_reasons(
                    termination.captured_args, aggregate.failed_required_ids, composed
                ),
                score=aggregate.score,
                binary_pass=aggregate.binary_pass,
                gate_failed=aggregate.gate_failed,
                criterion_results=tuple(results),
                failed_required_ids=aggregate.failed_required_ids,
                kb_tools_offered=composed.kb_tools_offered,
                kb_tools_withheld=composed.kb_tools_withheld,
                read_tools_offered=composed.read_tools_offered,
                state_diff=state_diff,
                transcript=_serialize_judge_transcript(messages),
            )

    def _loop(
        self,
        registry: ToolRegistry,
        metrics: _JudgeMetricsSink,
        termination: _SubmitReportTermination,
        logger: StructuredLogger,
    ) -> ToolCallingLoop:
        client: JudgeModel = (
            self._llm_client if self._llm_client is not None else LLMClient(self._model_config)
        )
        tool_schemas = registry.get_schemas(sanitize=False)
        return ToolCallingLoop(
            llm_client=client,
            tool_executor=ToolExecutor(registry),
            tool_schemas=tool_schemas,
            validation_schemas_by_tool=client.sanitize_tools_for_execution(tool_schemas),
            tool_output_max_chars_by_tool=registry.output_max_chars_by_tool(),
            # Bounded by max_turns + wall-time (episode_timeout_s). There is no
            # per-turn loop timeout; the per-call LLM timeout lives in LLMClient.
            config=LoopConfig(max_turns=self._max_turns, episode_timeout_s=self._episode_timeout_s),
            metrics=metrics,
            should_terminate=termination,
            classify_error=client.classify_loop_error,
            logger=logger,
            user_turn=None,
        )


def _reprompt_after_rejection(
    messages: list[Message],
    termination: _SubmitReportTermination,
    exc: SubmitReportValidationError,
) -> None:
    """Answer the rejected ``submit_report`` with its rejection so the judge tries again."""
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


def _refuse_views_no_part_needs(
    evidence: JudgeEvidence,
    *,
    state_diff: str | None,
    db_reader: DBReader | None,
    workspace_dir: Path | None,
) -> None:
    handed = [
        evidence_input.value
        for evidence_input, view in (
            (EvidenceInput.STATE_DIFF, state_diff),
            (EvidenceInput.DB, db_reader),
            (EvidenceInput.WORKSPACE, workspace_dir),
        )
        if view is not None and not evidence.needs(evidence_input)
    ]
    if handed:
        raise ValueError(
            f"the judge was handed {', '.join(handed)} with the evidence part that reads "
            "each off (customization.evidence); the caller builds only what "
            "JudgeEvidence.needs"
        )


def _build_errored_judge_result(
    metrics: _JudgeMetricsSink,
    reasons: str,
    messages: list[Message],
    composed: ComposedEvidence,
    state_diff: str | None,
) -> JudgeResult:
    """Build a fail-loud ERRORED result — no score, no criterion results.

    Carries the partial judge transcript: when the judge breaks, its messages
    so far are the most useful debugging artifact. The ``Judge KB: …`` note is
    appended even on error so a reviewer can see whether a KB-blind judge was a
    factor in the failure. Echoes the read-tool surface + state_diff
    it was handed so an offline replay of an errored trial is still reconstructable.
    """
    return JudgeResult(
        status=JudgeStatus.ERRORED,
        usage=metrics.snapshot(),
        reasons=f"{reasons} | {_kb_note(composed)}",
        score=None,
        binary_pass=None,
        kb_tools_offered=composed.kb_tools_offered,
        kb_tools_withheld=composed.kb_tools_withheld,
        read_tools_offered=composed.read_tools_offered,
        state_diff=state_diff,
        transcript=_serialize_judge_transcript(messages),
    )


def _kb_note(composed: ComposedEvidence) -> str:
    """The human-readable "graded with / without KB" signal (issue #95).

    e.g. ``Judge KB: search_policy`` / ``Judge KB: search_kb`` / ``Judge KB:
    none offered``. When the ``knowledge_search`` part is off and the agent
    actually had a KB tool to withhold, reads ``Judge KB: none offered (disabled
    by config)`` — distinguishing a deliberate gate from a rubric that simply
    needed no KB. Observability, not an error.
    """
    if composed.kb_tools_withheld:
        return "Judge KB: none offered (disabled by config)"
    if composed.kb_tools_offered:
        return f"Judge KB: {', '.join(composed.kb_tools_offered)}"
    return "Judge KB: none offered"


def _build_judge_reasons(
    tool_args: dict[str, Any],
    failed_required_ids: tuple[str, ...],
    composed: ComposedEvidence,
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
    parts.append(_kb_note(composed))
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
        extra_read_tools: list[EvidenceOwnedTool] | None = None,
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
    "format_transcript",
    "LLMJudge",
    "InMemoryJudge",
    "effective_judge_system_prompt",
    "model_config_from_ref",
]
