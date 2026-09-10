"""Agentic impl of :class:`JudgeKind` — draft, critique, then submit.

Registered under the name ``agentic_rubric`` in the ``tolokaforge.judge_kinds``
entry-point group. Unlike ``single_shot_rubric`` (one ``submit_report`` call
ends the episode) this kind runs a two-phase episode over the SAME
:class:`~tolokaforge.core.loop.ToolCallingLoop` machinery: the judge must call
``draft_report`` first with its initial verdict, receives an injected
critique prompt that echoes its own draft back at it, then re-examines the
evidence with its read tools before calling ``submit_report`` with a final
verdict. Both tools are registered and visible from turn 0 — the ordering is
enforced by :class:`_DraftReportTermination`'s injected corrective messages,
not by hiding ``submit_report`` until a draft exists (dynamic tool hiding is
task-specific harness logic, out of bounds per AGENTS.md Core Rule 7).

``ToolCallingLoop.run`` resets its own ``max_turns`` on every call, but one
agentic episode calls ``.run`` once per draft/critique/submit pause. So
:data:`AGENTIC_JUDGE_MAX_TURNS` is enforced here, across resumptions, by
counting assistant turns already recorded in ``messages`` and shrinking each
``LoopConfig.max_turns`` by that count — the true hard backstop the plan
requires, not a per-``.run()``-call limit that resets on every pause.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar

from tolokaforge.core.grading.judge import (
    DEFAULT_SUBMIT_REPORT_RETRIES,
    JudgeMetricsSink,
    SubmitReportTermination,
    answer_terminating_submit_report,
    build_errored_judge_result,
    build_judge_reasons,
    build_judge_registry,
    build_opening_message,
    build_rubric_brief,
    serialize_judge_transcript,
)
from tolokaforge.core.grading.judge_result import JudgeResult, JudgeStatus
from tolokaforge.core.grading.judge_tools import SubmitReportTool
from tolokaforge.core.grading.rubric import (
    DRAFT_REPORT_TOOL_NAME,
    SubmitReportValidationError,
    VerdictConsistencyError,
    aggregate_rubric,
    build_draft_report_tool,
    parse_submit_report,
)
from tolokaforge.core.judge_prompt import _compose_judge_system_prompt
from tolokaforge.core.loop import LoopConfig, TerminationDecision, ToolCallingLoop
from tolokaforge.core.models import Message, MessageRole, TerminationReason
from tolokaforge.core.summarize_policy import LLMSummarizer
from tolokaforge.tools.registry import ToolExecutor

if TYPE_CHECKING:
    from pathlib import Path

    from tolokaforge.core.grading.judge import DBReader
    from tolokaforge.core.grading.judge_model_provider import JudgeModel, JudgeModelProvider
    from tolokaforge.core.grading.kb_search import KnowledgeSearch
    from tolokaforge.core.grading.rubric import RubricAggregate
    from tolokaforge.core.llm.capabilities import ModelCapabilities
    from tolokaforge.core.llm.client import GenerationResult
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.core.models import ModelConfig
    from tolokaforge.runner.models import CriterionResult, Rubric
    from tolokaforge.tools.registry import Tool

__all__ = [
    "AGENTIC_JUDGE_EPISODE_TIMEOUT_S",
    "AGENTIC_JUDGE_MAX_TURNS",
    "DEFAULT_CRITIQUE_TURN_BUDGET",
    "AgenticRubricJudgeKind",
]

#: Hard turn backstop for one whole draft/critique/submit episode, enforced
#: across every ``ToolCallingLoop.run`` resumption (see module docstring).
AGENTIC_JUDGE_MAX_TURNS = 50

#: Wall-time budget for the whole episode, in seconds. Captured once before
#: the first ``.run()`` so it accumulates across resumptions, unlike
#: ``LLMJudge.run``'s per-call ``time.time()`` (there is only ever one call there).
AGENTIC_JUDGE_EPISODE_TIMEOUT_S = 480

#: Default number of turns advised to the judge for its critique phase when
#: ``kind_config`` omits ``critique_turn_budget``. Advisory only — soft, stated
#: in the injected critique message; :data:`AGENTIC_JUDGE_MAX_TURNS` is the
#: only hard backstop.
DEFAULT_CRITIQUE_TURN_BUDGET = 3

#: Accepted ``kind_config`` keys; every other key raises ``ValueError``.
_ACCEPTED_KIND_CONFIG_KEYS = frozenset({"critique_turn_budget"})


def _resolve_critique_turn_budget(kind_config: Mapping[str, Any] | None) -> int:
    """Validate ``kind_config`` and return the effective critique turn budget.

    Raises :class:`ValueError` on any unknown key or a non-positive
    ``critique_turn_budget`` before any judge dispatch runs.
    """
    if kind_config is None:
        return DEFAULT_CRITIQUE_TURN_BUDGET
    unknown = set(kind_config) - _ACCEPTED_KIND_CONFIG_KEYS
    if unknown:
        raise ValueError(
            f"agentic_rubric kind_config contains unknown key(s): {sorted(unknown)}. "
            f"Accepted keys: {sorted(_ACCEPTED_KIND_CONFIG_KEYS)}."
        )
    raw = kind_config.get("critique_turn_budget")
    if raw is None:
        return DEFAULT_CRITIQUE_TURN_BUDGET
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise ValueError(
            f"agentic_rubric critique_turn_budget must be an int; got {type(raw).__name__} {raw!r}."
        )
    if raw < 1:
        raise ValueError(f"agentic_rubric critique_turn_budget must be >= 1; got {raw}.")
    return raw


class _JudgeState(str, Enum):
    """The two phases of one agentic judge episode."""

    AWAITING_DRAFT = "awaiting_draft"
    CRITIQUING = "critiquing"


@dataclass
class _DraftReportTermination:
    """Stateful termination policy driving the draft -> critique -> submit cycle.

    Fires a terminal :class:`TerminationDecision` the instant EITHER
    ``draft_report`` or ``submit_report`` appears in a turn's tool calls —
    mirroring :class:`SubmitReportTermination`'s unconditional-terminal-on-match
    design, generalized to two tool names — so the caller
    (:meth:`AgenticRubricJudgeKind.evaluate`) can inspect which tool fired and
    in which state, apply the state machine over ``messages``, and resume the
    loop. A ``draft_report`` match never sets ``status`` (the episode has not
    ended); ``submit_report`` delegates to the wrapped
    :class:`SubmitReportTermination`, which always sets
    ``status=TrialStatus.COMPLETED`` regardless of ``state`` — the caller
    decides whether that submit was premature (still ``awaiting_draft``) by
    checking ``state`` before trusting the terminal decision as final.
    """

    state: _JudgeState = _JudgeState.AWAITING_DRAFT
    draft_call_id: str | None = field(default=None, init=False)
    draft_args: dict[str, Any] | None = field(default=None, init=False)
    submit: SubmitReportTermination = field(default_factory=SubmitReportTermination, init=False)

    def __call__(
        self, result: GenerationResult, turn: int, messages: list[Message]
    ) -> TerminationDecision | None:
        for tc in result.tool_calls:
            if tc.name == DRAFT_REPORT_TOOL_NAME:
                self.draft_call_id = tc.id
                self.draft_args = dict(tc.arguments or {})
                return TerminationDecision(
                    reason=TerminationReason.AGENT_DONE,
                    system_message="draft_report received; agentic judge pausing to critique.",
                )
        return self.submit(result, turn, messages)


@dataclass
class _RetryCounts:
    """Bounded-retry counters for malformed ``draft_report`` / ``submit_report`` args."""

    draft: int = 0
    submit: int = 0


@dataclass(frozen=True)
class _JudgeResultContext:
    """Construction-time fields every :class:`JudgeResult` this episode returns shares."""

    kb_tools_offered: tuple[str, ...]
    kb_tools_withheld: tuple[str, ...]
    knowledge_search_disabled: bool
    custom_system_prompt: bool
    include_agent_system_prompt: bool
    read_tools_offered: tuple[str, ...]
    state_diff: str | None


@dataclass(frozen=True)
class _EpisodeSetup:
    """Everything :meth:`AgenticRubricJudgeKind.evaluate` builds once per episode."""

    judge_model: JudgeModel
    capabilities: ModelCapabilities
    metrics: JudgeMetricsSink
    summarize_policy: LLMSummarizer | None
    tool_executor: ToolExecutor
    tool_schemas: list[dict[str, Any]]
    validation_schemas_by_tool: dict[str, dict[str, Any]]
    tool_output_max_chars_by_tool: dict[str, int]
    ctx: _JudgeResultContext
    messages: list[Message]
    system_prompt: str
    logger: StructuredLogger


def _build_critique_message(
    draft_results: list[CriterionResult], overall_reasons: object, critique_turn_budget: int
) -> str:
    """Compose the injected critique prompt, echoing the draft's own verdicts."""
    lines = [
        "Your draft_report has been recorded. Before finalizing, critically "
        "re-examine each criterion with your read tools, then call submit_report "
        "with your final verdict. You have approximately "
        f"{critique_turn_budget} turn(s) budgeted for this critique before you "
        "should submit.",
        "",
        "Your draft verdict was:",
    ]
    lines.extend(
        f"  - [{cr.id}] {'MET' if cr.met else 'NOT MET'} (score={cr.score}): {cr.justification}"
        for cr in draft_results
    )
    if isinstance(overall_reasons, str) and overall_reasons.strip():
        lines.append(f"Overall: {overall_reasons.strip()}")
    return "\n".join(lines)


def _errored(
    ctx: _JudgeResultContext, metrics: JudgeMetricsSink, messages: list[Message], reasons: str
) -> JudgeResult:
    return build_errored_judge_result(
        metrics,
        reasons,
        messages,
        ctx.kb_tools_offered,
        ctx.kb_tools_withheld,
        knowledge_search_disabled=ctx.knowledge_search_disabled,
        custom_system_prompt=ctx.custom_system_prompt,
        include_agent_system_prompt=ctx.include_agent_system_prompt,
        read_tools_offered=ctx.read_tools_offered,
        state_diff=ctx.state_diff,
    )


def _completed(
    ctx: _JudgeResultContext,
    metrics: JudgeMetricsSink,
    messages: list[Message],
    tool_args: dict[str, Any],
    results: list[CriterionResult],
    aggregate: RubricAggregate,
) -> JudgeResult:
    reasons = build_judge_reasons(
        tool_args,
        aggregate.failed_required_ids,
        ctx.kb_tools_offered,
        ctx.kb_tools_withheld,
        ctx.knowledge_search_disabled,
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
        kb_tools_offered=ctx.kb_tools_offered,
        kb_tools_withheld=ctx.kb_tools_withheld,
        knowledge_search_disabled=ctx.knowledge_search_disabled,
        custom_system_prompt=ctx.custom_system_prompt,
        include_agent_system_prompt=ctx.include_agent_system_prompt,
        read_tools_offered=ctx.read_tools_offered,
        state_diff=ctx.state_diff,
        transcript=serialize_judge_transcript(messages),
    )


def _handle_submit_call(
    termination: _DraftReportTermination,
    ctx: _JudgeResultContext,
    metrics: JudgeMetricsSink,
    messages: list[Message],
    rubric: Rubric,
    retry_counts: _RetryCounts,
) -> JudgeResult | None:
    """Apply transitions 4/5 for a captured ``submit_report`` call.

    Returns the episode's final :class:`JudgeResult` on real completion or
    retry exhaustion, ``None`` to resume the loop.
    """
    call_id = termination.submit.captured_call_id
    args = termination.submit.captured_args
    termination.submit.captured_args = None
    termination.submit.captured_call_id = None

    if termination.state is not _JudgeState.CRITIQUING:
        answer_terminating_submit_report(
            messages,
            call_id,
            "submit_report rejected: call draft_report first with your initial "
            "verdict, then critique it, before calling submit_report.",
        )
        return None

    try:
        results = parse_submit_report(args, rubric)
        aggregate = aggregate_rubric(rubric, results)
    except SubmitReportValidationError as exc:
        if isinstance(exc, VerdictConsistencyError):
            metrics.consistency_rejections += 1
        retry_counts.submit += 1
        if retry_counts.submit > DEFAULT_SUBMIT_REPORT_RETRIES:
            return _errored(
                ctx,
                metrics,
                messages,
                f"submit_report invalid after {DEFAULT_SUBMIT_REPORT_RETRIES} retries: {exc}",
            )
        answer_terminating_submit_report(
            messages,
            call_id,
            f"Your submit_report was rejected: {exc}\n"
            "Fix the issue and call submit_report again with a verdict and "
            "justification for every criterion.",
        )
        return None

    return _completed(ctx, metrics, messages, args, results, aggregate)


def _handle_draft_call(
    termination: _DraftReportTermination,
    ctx: _JudgeResultContext,
    metrics: JudgeMetricsSink,
    messages: list[Message],
    rubric: Rubric,
    retry_counts: _RetryCounts,
    critique_turn_budget: int,
) -> JudgeResult | None:
    """Apply transitions 1/2/3 for a captured ``draft_report`` call.

    Returns the episode's final (errored) :class:`JudgeResult` on retry
    exhaustion, ``None`` to resume the loop.
    """
    call_id = termination.draft_call_id
    args = termination.draft_args
    termination.draft_call_id = None
    termination.draft_args = None

    if termination.state is _JudgeState.CRITIQUING:
        answer_terminating_submit_report(
            messages,
            call_id,
            "draft_report already submitted; continue critiquing your draft with "
            "your read tools, then call submit_report with your final verdict.",
        )
        return None

    try:
        draft_results = parse_submit_report(args, rubric)
    except SubmitReportValidationError as exc:
        if isinstance(exc, VerdictConsistencyError):
            metrics.consistency_rejections += 1
        retry_counts.draft += 1
        if retry_counts.draft > DEFAULT_SUBMIT_REPORT_RETRIES:
            return _errored(
                ctx,
                metrics,
                messages,
                f"draft_report invalid after {DEFAULT_SUBMIT_REPORT_RETRIES} retries: {exc}",
            )
        answer_terminating_submit_report(
            messages,
            call_id,
            f"Your draft_report was rejected: {exc}\n"
            "Fix the issue and call draft_report again with a verdict and "
            "justification for every criterion.",
        )
        return None

    answer_terminating_submit_report(messages, call_id, "Draft received.")
    messages.append(
        Message(
            role=MessageRole.USER,
            content=_build_critique_message(
                draft_results, args.get("reasons"), critique_turn_budget
            ),
        )
    )
    termination.state = _JudgeState.CRITIQUING
    return None


def _build_loop(
    judge_model: JudgeModel,
    tool_executor: ToolExecutor,
    tool_schemas: list[dict[str, Any]],
    validation_schemas_by_tool: dict[str, dict[str, Any]],
    tool_output_max_chars_by_tool: dict[str, int],
    max_turns: int,
    capabilities: ModelCapabilities,
    summarize_policy: LLMSummarizer | None,
    metrics: JudgeMetricsSink,
    termination: _DraftReportTermination,
    logger: StructuredLogger,
) -> ToolCallingLoop:
    return ToolCallingLoop(
        llm_client=judge_model,
        tool_executor=tool_executor,
        tool_schemas=tool_schemas,
        validation_schemas_by_tool=validation_schemas_by_tool,
        tool_output_max_chars_by_tool=tool_output_max_chars_by_tool,
        config=LoopConfig(
            max_turns=max_turns,
            episode_timeout_s=AGENTIC_JUDGE_EPISODE_TIMEOUT_S,
            empty_retry_count=capabilities.empty_retry_count,
            output_length_retry_count=capabilities.output_length_retry_count,
            parser_error_retry_count=capabilities.parser_error_retry_count,
            tool_output_max_chars=capabilities.tool_output_max_chars,
            max_context_tokens=capabilities.max_context_tokens,
            context_watermark=capabilities.context_watermark,
            summarize_policy=summarize_policy,
        ),
        metrics=metrics,
        should_terminate=termination,
        classify_error=judge_model.classify_loop_error,
        logger=logger,
        user_turn=None,
    )


def _build_episode_setup(
    *,
    rubric: Rubric,
    agent_system_prompt: str,
    transcript: list[dict[str, Any]],
    db_reader: DBReader | None,
    kb_search: KnowledgeSearch | None,
    workspace_dir: Path | None,
    extra_read_tools: list[Tool],
    state_diff: str | None,
    judge_model_config: ModelConfig,
    judge_model_provider: JudgeModelProvider,
    disable_knowledge_search: bool,
    custom_system_prompt: str | None,
    include_agent_system_prompt: bool,
    logger: StructuredLogger,
) -> _EpisodeSetup:
    """Build the judge model, tool registry, and opening transcript for one episode."""
    judge_model = judge_model_provider.build(judge_model_config)
    capabilities = judge_model.capabilities
    metrics = JudgeMetricsSink()
    summarize_policy: LLMSummarizer | None = None
    if capabilities.max_context_tokens is not None and capabilities.context_watermark is not None:
        summarize_policy = LLMSummarizer(judge_model, metrics)

    registry, kb_tools_offered, kb_tools_withheld, read_tools_offered = build_judge_registry(
        rubric,
        db_reader=db_reader,
        kb_search=kb_search,
        extra_read_tools=extra_read_tools,
        workspace_dir=workspace_dir,
        disable_knowledge_search=disable_knowledge_search,
        logger=logger,
    )
    registry.register(SubmitReportTool(build_draft_report_tool(rubric)))
    tool_executor = ToolExecutor(registry)
    tool_schemas = registry.get_schemas(sanitize=False)

    ctx = _JudgeResultContext(
        kb_tools_offered=kb_tools_offered,
        kb_tools_withheld=kb_tools_withheld,
        knowledge_search_disabled=disable_knowledge_search,
        custom_system_prompt=custom_system_prompt is not None,
        include_agent_system_prompt=include_agent_system_prompt,
        read_tools_offered=read_tools_offered,
        state_diff=state_diff,
    )
    messages: list[Message] = [
        Message(
            role=MessageRole.USER,
            content=build_opening_message(
                agent_system_prompt,
                transcript,
                state_diff,
                include_agent_system_prompt=include_agent_system_prompt,
            ),
        )
    ]
    system_prompt = (
        f"{_compose_judge_system_prompt(custom_system_prompt)}\n\n{build_rubric_brief(rubric)}"
    )
    return _EpisodeSetup(
        judge_model=judge_model,
        capabilities=capabilities,
        metrics=metrics,
        summarize_policy=summarize_policy,
        tool_executor=tool_executor,
        tool_schemas=tool_schemas,
        validation_schemas_by_tool=judge_model.sanitize_tools_for_execution(tool_schemas),
        tool_output_max_chars_by_tool=registry.output_max_chars_by_tool(),
        ctx=ctx,
        messages=messages,
        system_prompt=system_prompt,
        logger=logger,
    )


def _run_episode(setup: _EpisodeSetup, rubric: Rubric, critique_turn_budget: int) -> JudgeResult:
    """Drive the draft/critique/submit state machine to a final :class:`JudgeResult`.

    Resumes a fresh :class:`ToolCallingLoop` after every ``draft_report`` /
    ``submit_report`` pause, shrinking ``max_turns`` by the assistant turns
    already spent so :data:`AGENTIC_JUDGE_MAX_TURNS` holds across resumptions.
    """
    termination = _DraftReportTermination()
    retry_counts = _RetryCounts()
    start_time = time.time()
    while True:
        turns_used = sum(1 for m in setup.messages if m.role == MessageRole.ASSISTANT)
        remaining_turns = AGENTIC_JUDGE_MAX_TURNS - turns_used
        if remaining_turns <= 0:
            return _errored(
                setup.ctx,
                setup.metrics,
                setup.messages,
                f"Agentic judge exhausted its {AGENTIC_JUDGE_MAX_TURNS}-turn budget "
                "across the draft/critique/submit cycle.",
            )

        loop = _build_loop(
            setup.judge_model,
            setup.tool_executor,
            setup.tool_schemas,
            setup.validation_schemas_by_tool,
            setup.tool_output_max_chars_by_tool,
            remaining_turns,
            setup.capabilities,
            setup.summarize_policy,
            setup.metrics,
            termination,
            setup.logger,
        )
        try:
            outcome = loop.run(setup.system_prompt, setup.messages, start_time=start_time)
        except Exception as exc:  # noqa: BLE001 — fail loud, never score on judge crash
            setup.logger.error(
                "Agentic judge loop raised", error=str(exc), error_type=type(exc).__name__
            )
            return _errored(
                setup.ctx,
                setup.metrics,
                setup.messages,
                f"Agentic judge loop crashed: {type(exc).__name__}: {exc}",
            )

        if termination.submit.captured_args is not None:
            result = _handle_submit_call(
                termination, setup.ctx, setup.metrics, setup.messages, rubric, retry_counts
            )
            if result is not None:
                return result
            continue

        if termination.draft_call_id is not None:
            result = _handle_draft_call(
                termination,
                setup.ctx,
                setup.metrics,
                setup.messages,
                rubric,
                retry_counts,
                critique_turn_budget,
            )
            if result is not None:
                return result
            continue

        return _errored(
            setup.ctx,
            setup.metrics,
            setup.messages,
            "Agentic judge ended without calling submit_report "
            f"(termination={outcome.termination_reason}, status={outcome.status}).",
        )


class AgenticRubricJudgeKind:
    """Grade a rubric via a draft-then-critique-then-submit judge episode."""

    NAME: ClassVar[str] = "agentic_rubric"

    def evaluate(
        self,
        *,
        rubric: Rubric,
        agent_system_prompt: str,
        transcript: list[dict[str, Any]],
        db_reader: DBReader | None,
        kb_search: KnowledgeSearch | None,
        workspace_dir: Path | None,
        extra_read_tools: list[Tool],
        state_diff: str | None,
        judge_model_config: ModelConfig,
        judge_model_provider: JudgeModelProvider,
        disable_knowledge_search: bool,
        custom_system_prompt: str | None,
        include_agent_system_prompt: bool,
        kind_config: Mapping[str, Any] | None,
        logger: StructuredLogger,
    ) -> JudgeResult:
        critique_turn_budget = _resolve_critique_turn_budget(kind_config)
        setup = _build_episode_setup(
            rubric=rubric,
            agent_system_prompt=agent_system_prompt,
            transcript=transcript,
            db_reader=db_reader,
            kb_search=kb_search,
            workspace_dir=workspace_dir,
            extra_read_tools=extra_read_tools,
            state_diff=state_diff,
            judge_model_config=judge_model_config,
            judge_model_provider=judge_model_provider,
            disable_knowledge_search=disable_knowledge_search,
            custom_system_prompt=custom_system_prompt,
            include_agent_system_prompt=include_agent_system_prompt,
            logger=logger,
        )
        return _run_episode(setup, rubric, critique_turn_budget)
