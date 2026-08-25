"""Composite dispatch for the standalone grader service.

The grader dispatches every grading component (state_checks jsonpath +
db_probes, transcript_rules, trace_checks, llm_judge, custom_checks)
through the six plug-in seams over a
:class:`~tolokaforge.core.grading.substrate_live.LiveRunnerCallbackGradingSubstrate`
dialled at the runner's substrate address.

:class:`GraderCompositeDispatch` is constructed once at grader-service
boot. Five holistic seams (custom-check executor, judge-model provider,
transcript-rule matcher, state-check backends, substrate class) are
loaded from :mod:`tolokaforge.core.plugin_registry` and cached on the
instance; the sixth seam — the rubric evaluator — is loaded per-``grade``
call because :class:`~tolokaforge.runner.models.LLMJudgeConfig.customization`
shapes its construction and rides on the wire per trial.

Each :meth:`grade` call validates the required v2 wire fields, deserialises
the run-scoped :class:`~tolokaforge.runner.models.RunnerGradingConfig` /
:class:`~tolokaforge.runner.models.TaskDescription` /
:class:`~tolokaforge.core.models.ModelConfig` from JSON, builds a fresh
substrate against ``dispatch.runner_substrate_address``, drives the composite
functions in the same order the runner's ``_grade_trial_async`` does (minus
hash / accounted-keys ledger / verdict compose), and folds the results into
a :class:`~tolokaforge.core.models.Grade`. Hash grading is refused (the
substrate is read-only). ``SubstrateUnreachableError`` is translated to
:class:`~tolokaforge.core.trial_grader.GradingFailedError` so the trial books
as ungradeable rather than as an agent failure.

Sync/async: :meth:`GraderServiceImpl.Grade` runs on a gRPC server thread
from ``futures.ThreadPoolExecutor`` — already off any event loop — so
this dispatch is plain sync and the substrate's blocking gRPC reads
resolve without a ``run_in_executor`` bridge. The runner needs its bridge
because it shares an event-loop thread across dispatch + DB reads; the
grader does not.
"""

from __future__ import annotations

import json
import shutil
import sys
from typing import TYPE_CHECKING, Any

from tolokaforge.core.grading import composite
from tolokaforge.core.grading.judge_result import JudgeStatus as JudgeRunStatus
from tolokaforge.core.grading.substrate import SubstrateUnreachableError
from tolokaforge.core.grading.tool_artifacts import extract_tool_artifacts
from tolokaforge.core.grading.trace_checks import TraceChecksResult
from tolokaforge.core.grading.trace_timeline import build_trial_timeline
from tolokaforge.core.grading.transcript_wire import (
    decode_transcript_wire,
    split_leading_system_message,
)
from tolokaforge.core.models import (
    CustomCheckDetail,
    Grade,
    GradeComponents,
    JudgeInputs,
    JudgeKbGating,
    JudgeStatus,
    JudgeUsage,
    ModelConfig,
    TraceChecksSummary,
    TraceConstraintResult,
    TracePathResult,
)
from tolokaforge.core.plugin_registry import (
    load_custom_check_executor,
    load_grading_substrate,
    load_judge_model_provider,
    load_rubric_evaluator,
    load_state_check_backend,
    load_transcript_rule_matcher,
)
from tolokaforge.core.trial_grader import GradingFailedError
from tolokaforge.runner.grading import (
    build_grade_reasons,
    compose_runner_trial_verdict,
    resolve_state_checks_component,
)
from tolokaforge.runner.models import (
    RunnerGradeComponents,
    RunnerGradingConfig,
    TaskDescription,
)
from tolokaforge.runner.protocol import parse_termination_reason

if TYPE_CHECKING:
    from tolokaforge.core.grading.judge_result import JudgeResult
    from tolokaforge.core.grading.rubric_evaluator import RubricEvaluator
    from tolokaforge.core.grading.substrate import GradingSubstrate
    from tolokaforge.core.grading.transcript import TranscriptEvaluationResult
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.grader.service import GradeDispatch


__all__ = ["GraderCompositeDispatch"]


class GraderCompositeDispatch:
    """Grade a trial by dispatching through the six sub-component plug-in
    seams over a :class:`LiveRunnerCallbackGradingSubstrate`.

    Constructed once per grader process. Five holistic seams cache at
    construction; the rubric-evaluator seam loads per-call because the
    trial's :class:`LLMJudgeConfig.customization` shapes its context.
    Every :meth:`grade` call constructs a fresh substrate against the
    trial's ``runner_substrate_address``, extracts the pack's
    ``tool_artifacts`` bundle if present, runs the composite mirroring
    the runner (minus hash + ledger + verdict compose), and returns a
    :class:`Grade` or ``None``.

    Fail-loud: missing required wire fields raise
    :class:`GradingFailedError` naming the field; a task declaring
    ``state_checks.hash_enabled`` raises with the operator's actionable
    branch; :class:`SubstrateUnreachableError` propagates as
    :class:`GradingFailedError`. The service translates every raise into
    ``GradeResponse(success=false)``.
    """

    def __init__(self, logger: StructuredLogger) -> None:
        self._logger = logger
        self._check_executor = load_custom_check_executor("check_runner")()
        self._judge_model_provider = load_judge_model_provider("litellm")()
        self._transcript_rule_matcher = load_transcript_rule_matcher("default")()
        self._state_check_backends = {
            "jsonpath": load_state_check_backend("jsonpath")(),
            "db_probes": load_state_check_backend("db_probes")(),
        }
        self._substrate_cls = load_grading_substrate("live_callback")

    def grade(self, dispatch: GradeDispatch) -> Grade | None:
        """Grade one trial from its wire dispatch payload.

        Returns a :class:`Grade` on a completed grade, ``None`` when the
        trial produced no transcript to grade against and the config
        declared no grading. Raises :class:`GradingFailedError` on any
        missing required wire field, on a hash-enabled task, or on a
        substrate read that a runner outage makes unreachable.
        """
        if not dispatch.task_config_json:
            raise GradingFailedError(
                "grader_rpc requires task_config_json on GradeRequest (missing on v2 wire)"
            )
        if not dispatch.task_description_json:
            raise GradingFailedError(
                "grader_rpc requires task_description_json on GradeRequest (missing on v2 wire)"
            )
        if not dispatch.runner_substrate_address:
            raise GradingFailedError(
                "grader_rpc requires runner_substrate_address on GradeRequest (missing on v2 wire)"
            )

        try:
            grading_config = RunnerGradingConfig.model_validate_json(dispatch.task_config_json)
        except Exception as exc:
            raise GradingFailedError(
                f"grader_rpc could not deserialise task_config_json: {exc}"
            ) from exc
        try:
            task_description = TaskDescription.model_validate_json(dispatch.task_description_json)
        except Exception as exc:
            raise GradingFailedError(
                f"grader_rpc could not deserialise task_description_json: {exc}"
            ) from exc

        llm_judge_config = grading_config.llm_judge
        judge_model_config: ModelConfig | None = None
        if llm_judge_config is not None:
            if not dispatch.judge_model_config_json:
                raise GradingFailedError(
                    "grader_rpc requires judge_model_config_json when the task declares "
                    "llm_judge (missing on v2 wire)"
                )
            try:
                judge_model_config = ModelConfig.model_validate_json(
                    dispatch.judge_model_config_json
                )
            except Exception as exc:
                raise GradingFailedError(
                    f"grader_rpc could not deserialise judge_model_config_json: {exc}"
                ) from exc

        state_checks_config = grading_config.state_checks
        if state_checks_config and state_checks_config.hash_enabled:
            raise GradingFailedError(
                "grader_rpc cannot execute hash-based grading — the substrate is "
                "read-only. Configure grader: runner_rpc for this task, or disable "
                "hash_enabled."
            )

        id_fields: dict[str, str | list[str]] = (
            state_checks_config.id_fields if state_checks_config else {}
        )
        initial_state = task_description.initial_state
        unstable_fields = {(u.table_name, u.field_name) for u in initial_state.unstable_fields}
        initial_state_schemas = list(initial_state.schemas)
        tool_artifacts = task_description.tool_artifacts or {}

        artifacts_dir = None
        added_sys_path: list[str] = []
        if tool_artifacts:
            artifacts_dir = extract_tool_artifacts(dispatch.trial_id, tool_artifacts)
            candidate_entries = [str(artifacts_dir)]
            if (artifacts_dir / "tools").exists():
                candidate_entries.append(str(artifacts_dir / "tools"))
            for entry in candidate_entries:
                if entry in sys.path:
                    added_sys_path.append(entry)

        substrate: GradingSubstrate = self._substrate_cls(
            dispatch.runner_substrate_address, dispatch.trial_id
        )
        try:
            try:
                grade = self._run_composite(
                    dispatch=dispatch,
                    grading_config=grading_config,
                    task_description=task_description,
                    judge_model_config=judge_model_config,
                    id_fields=id_fields,
                    unstable_fields=unstable_fields,
                    initial_state_schemas=initial_state_schemas,
                    substrate=substrate,
                    artifacts_dir=artifacts_dir,
                )
            except SubstrateUnreachableError as exc:
                raise GradingFailedError(
                    f"substrate unreachable at {dispatch.runner_substrate_address}: {exc}"
                ) from exc
        finally:
            substrate.close()
            for entry in added_sys_path:
                while entry in sys.path:
                    sys.path.remove(entry)
            if artifacts_dir is not None:
                shutil.rmtree(artifacts_dir, ignore_errors=True)

        return grade

    def _run_composite(
        self,
        *,
        dispatch: GradeDispatch,
        grading_config: RunnerGradingConfig,
        task_description: TaskDescription,
        judge_model_config: ModelConfig | None,
        id_fields: dict[str, str | list[str]],
        unstable_fields: set[tuple[str, str]],
        initial_state_schemas: list[Any],
        substrate: GradingSubstrate,
        artifacts_dir: Any,
    ) -> Grade:
        """Mirror ``_grade_trial_async`` (runner) minus hash / accounted-keys ledger."""
        trial_id = dispatch.trial_id
        llm_messages: list[dict[str, Any]] = json.loads(dispatch.llm_messages_json or "[]")
        timeline = _build_timeline(llm_messages, dispatch.termination_reason)
        components = RunnerGradeComponents()
        state_checks_config = grading_config.state_checks
        self._grade_state_checks_block(
            trial_id=trial_id,
            state_checks_config=state_checks_config,
            substrate=substrate,
            components=components,
        )
        transcript_result = self._grade_transcript_rules_block(
            trial_id=trial_id,
            config=grading_config.transcript_rules,
            timeline=timeline,
            components=components,
        )
        trace_result = self._grade_trace_checks_block(
            trial_id=trial_id,
            config=grading_config.trace_checks,
            timeline=timeline,
            components=components,
        )
        judge_result, judge_status, judge_gate_failed = self._grade_llm_judge_block(
            trial_id=trial_id,
            llm_judge_config=grading_config.llm_judge,
            judge_model_config=judge_model_config,
            llm_messages=llm_messages,
            substrate=substrate,
            initial_state_schemas=initial_state_schemas,
            id_fields=id_fields,
            unstable_fields=unstable_fields,
            components=components,
        )
        custom_wire_results, custom_reasons = self._grade_custom_checks_block(
            trial_id=trial_id,
            grading_config=grading_config,
            task_description=task_description,
            llm_messages=llm_messages,
            substrate=substrate,
            artifacts_dir=artifacts_dir,
            components=components,
        )
        state_checks_slot, verdict, reasons = _compose_verdict_and_reasons(
            components=components,
            grading_config=grading_config,
            state_checks_config=state_checks_config,
            judge_gate_failed=judge_gate_failed,
            judge_status=judge_status,
            judge_result=judge_result,
            transcript_result=transcript_result,
            trace_result=trace_result,
            custom_reasons=custom_reasons,
        )
        return _build_grade(
            verdict_score=verdict.score,
            verdict_pass=verdict.binary_pass,
            components=components,
            state_checks_slot_component=state_checks_slot.component,
            reasons=reasons,
            custom_wire_results=custom_wire_results,
            trace_result=trace_result,
            judge_result=judge_result,
            judge_status=judge_status,
        )

    def _grade_state_checks_block(
        self,
        *,
        trial_id: str,
        state_checks_config: Any,
        substrate: GradingSubstrate,
        components: RunnerGradeComponents,
    ) -> None:
        """Run the ``state_checks`` reads block and fold results onto ``components``."""
        if not state_checks_config:
            return
        if not (state_checks_config.jsonpath_checks or state_checks_config.db_probes):
            return
        state_reads = composite.grade_state_checks_reads(
            trial_id=trial_id,
            config=state_checks_config,
            substrate=substrate,
            state_check_backends=self._state_check_backends,
            logger=self._logger,
        )
        if state_reads.jsonpath_score is not None:
            components.jsonpath_score = state_reads.jsonpath_score
            components.jsonpath_reasons = state_reads.jsonpath_reasons or ""
        if state_reads.db_probe_score is not None:
            components.db_probe_score = state_reads.db_probe_score
            components.db_probe_reasons = state_reads.db_probe_reasons or ""

    def _grade_transcript_rules_block(
        self,
        *,
        trial_id: str,
        config: Any,
        timeline: Any,
        components: RunnerGradeComponents,
    ) -> TranscriptEvaluationResult | None:
        """Run the transcript-rules block and fold the pass / score onto ``components``."""
        if not config:
            return None
        transcript_result, _accounting = composite.grade_transcript_rules(
            trial_id=trial_id,
            config=config,
            timeline=timeline,
            matcher=self._transcript_rule_matcher,
            logger=self._logger,
        )
        if transcript_result is not None:
            components.transcript_pass = transcript_result.passed
            components.transcript_score = transcript_result.score
        return transcript_result

    def _grade_trace_checks_block(
        self,
        *,
        trial_id: str,
        config: Any,
        timeline: Any,
        components: RunnerGradeComponents,
    ) -> TraceChecksResult:
        """Run the trace-checks block; populate ``components.trace_checks_score`` when scored."""
        if not config:
            return TraceChecksResult()
        trace_result = composite.grade_trace_checks(
            trial_id=trial_id,
            config=config,
            timeline=timeline,
            logger=self._logger,
        )
        if trace_result.constraints:
            components.trace_checks_score = trace_result.score
        return trace_result

    def _grade_custom_checks_block(
        self,
        *,
        trial_id: str,
        grading_config: RunnerGradingConfig,
        task_description: TaskDescription,
        llm_messages: list[dict[str, Any]],
        substrate: GradingSubstrate,
        artifacts_dir: Any,
        components: RunnerGradeComponents,
    ) -> tuple[list[Any], str | None]:
        """Run custom checks and fold the score onto ``components``.

        Returns ``(custom_wire_results, custom_reasons)`` for the reason
        composition and Grade assembly downstream.
        """
        custom_score, custom_wire_results, custom_reasons = composite.grade_custom_checks(
            trial_id=trial_id,
            config=grading_config.custom_checks,
            substrate=substrate,
            llm_messages=llm_messages,
            task_description=task_description,
            artifacts_dir=artifacts_dir,
            check_executor=self._check_executor,
            logger=self._logger,
        )
        components.custom_checks_score = custom_score
        return custom_wire_results, custom_reasons

    def _grade_llm_judge_block(
        self,
        *,
        trial_id: str,
        llm_judge_config: Any,
        judge_model_config: ModelConfig | None,
        llm_messages: list[dict[str, Any]],
        substrate: GradingSubstrate,
        initial_state_schemas: list[Any],
        id_fields: dict[str, str | list[str]],
        unstable_fields: set[tuple[str, str]],
        components: RunnerGradeComponents,
    ) -> tuple[JudgeResult | None, JudgeStatus, bool]:
        """Load the rubric-evaluator seam, render the state diff, and grade.

        Returns ``(judge_result, wire_judge_status, judge_gate_failed)``.
        A skipped judge (missing config or empty transcript) reports
        :attr:`JudgeStatus.UNSPECIFIED` with a ``None`` result; a runner
        errored status maps to :attr:`JudgeStatus.ERRORED`. On a
        completed run, ``components.llm_judge_score`` is populated when
        the judge produced a numeric score.
        """
        if not (llm_judge_config and llm_messages):
            return None, JudgeStatus.UNSPECIFIED, False
        assert (
            judge_model_config is not None
        ), "llm_judge branch requires judge_model_config — validated above"
        rubric_evaluator = self._build_rubric_evaluator(llm_judge_config)
        state_diff_text = composite.build_judge_state_diff(
            trial_id=trial_id,
            substrate=substrate,
            initial_state_schemas=initial_state_schemas,
            id_fields=id_fields,
            unstable_fields=unstable_fields,
            logger=self._logger,  # type: ignore[arg-type]
        )
        judge_result = composite.grade_llm_judge(
            trial_id=trial_id,
            config=llm_judge_config,
            substrate=substrate,
            rubric_evaluator=rubric_evaluator,
            llm_messages=llm_messages,
            judge_model_config=judge_model_config,
            extra_read_tools=[],
            state_diff=state_diff_text,
            logger=self._logger,
        )
        if judge_result.status is JudgeRunStatus.ERRORED:
            return judge_result, JudgeStatus.ERRORED, False
        judge_gate_failed = judge_result.gate_failed
        if judge_result.score is not None:
            components.llm_judge_score = judge_result.score
        return judge_result, JudgeStatus.COMPLETED, judge_gate_failed

    def _build_rubric_evaluator(self, llm_judge_config: Any) -> RubricEvaluator:
        """Load the ``llm_judge`` rubric-evaluator seam with per-trial context.

        Mirrors the runner's ``_grade_llm_judge`` construction — the trial's
        :attr:`LLMJudgeConfig.customization` shapes the KB gate, custom
        system-prompt override, and the include-agent-system-prompt default.
        """
        from tolokaforge.core.grading.rubric_evaluator import RubricEvaluatorContext

        customization = llm_judge_config.customization
        disable_knowledge_search = bool(customization and customization.disable_knowledge_search)
        custom_system_prompt = customization.system_prompt if customization else None
        include_agent_system_prompt = (
            customization.include_agent_system_prompt
            if customization and customization.include_agent_system_prompt is not None
            else True
        )
        return load_rubric_evaluator("llm_judge")(
            RubricEvaluatorContext(
                judge_model_provider=self._judge_model_provider,
                disable_knowledge_search=disable_knowledge_search,
                custom_system_prompt=custom_system_prompt,
                include_agent_system_prompt=include_agent_system_prompt,
            )
        )


def _build_timeline(
    llm_messages: list[dict[str, Any]],
    raw_termination_reason: str,
) -> Any:
    """Build the grader-side :class:`TrialTimeline` from the wire alone.

    Empty ``llm_messages`` produces a records-empty timeline that still
    reflects the termination reason. The composite skips llm_judge in
    that case but the timeline call must reconcile without raising so
    the trace-checks branch runs.
    """
    termination_reason = parse_termination_reason(raw_termination_reason)
    if llm_messages:
        _, transcript = split_leading_system_message(llm_messages)
        return build_trial_timeline(decode_transcript_wire(transcript), [], termination_reason)
    return build_trial_timeline([], [], termination_reason)


def _compose_verdict_and_reasons(
    *,
    components: RunnerGradeComponents,
    grading_config: RunnerGradingConfig,
    state_checks_config: Any,
    judge_gate_failed: bool,
    judge_status: JudgeStatus,
    judge_result: JudgeResult | None,
    transcript_result: TranscriptEvaluationResult | None,
    trace_result: TraceChecksResult,
    custom_reasons: str | None,
) -> tuple[Any, Any, str]:
    """Fold state-checks slot, verdict, and reason segments into their wire shapes.

    Mutates ``components.llm_judge_score`` to the composed judge component
    (matching the runner's ``_grade_trial_async`` combine order). Returns
    ``(state_checks_slot, verdict, reasons)``.
    """
    state_checks_slot = resolve_state_checks_component(
        hash_score=components.hash_score,
        jsonpath_score=components.jsonpath_score,
        db_probe_score=components.db_probe_score,
        hash_weight=state_checks_config.hash_weight if state_checks_config else None,
    )
    verdict = compose_runner_trial_verdict(
        components.model_dump(),
        grading_config.model_dump(),
        judge_gate_failed=judge_gate_failed,
        trace_gate_failed=trace_result.gate_failed,
    )
    components.llm_judge_score = verdict.judge_component

    judge_reasons = judge_result.reasons if judge_result is not None else None
    reason_segments = [
        build_grade_reasons(
            components.model_dump(),
            None,
            transcript_result.model_dump() if transcript_result is not None else None,
            judge_reasons=judge_reasons or None,
            trace_checks_result=trace_result.model_dump(mode="json"),
            golden_replay=None,
            custom_checks_reasons=custom_reasons,
        )
    ]
    if judge_status is JudgeStatus.ERRORED:
        reason_segments.append(f"JUDGE ERRORED: {judge_reasons}")
    if state_checks_slot.inert_weight_reason:
        reason_segments.append(state_checks_slot.inert_weight_reason)
    if verdict.reason:
        reason_segments.append(verdict.reason)
    reasons = " | ".join(segment for segment in reason_segments if segment)
    return state_checks_slot, verdict, reasons


def _components_to_grade_components(
    components: RunnerGradeComponents,
    state_checks_slot_component: float | None,
) -> GradeComponents:
    """Project the runner-side ``RunnerGradeComponents`` onto the wire
    ``GradeComponents`` shape, translating the ``-1.0`` not-evaluated
    sentinel into ``None``.
    """

    def _slot(value: float) -> float | None:
        return None if value < 0 else value

    return GradeComponents(
        state_checks=state_checks_slot_component,
        transcript_rules=_slot(components.transcript_score),
        trace_checks=_slot(components.trace_checks_score),
        llm_judge=_slot(components.llm_judge_score),
        custom_checks=_slot(components.custom_checks_score),
    )


def _build_grade(
    *,
    verdict_score: float,
    verdict_pass: bool,
    components: RunnerGradeComponents,
    state_checks_slot_component: float | None,
    reasons: str,
    custom_wire_results: list[Any],
    trace_result: TraceChecksResult,
    judge_result: JudgeResult | None,
    judge_status: JudgeStatus,
) -> Grade:
    """Assemble the Python :class:`Grade` from every dispatch output.

    ``custom_wire_results`` are pb2 ``CustomCheckResult`` shapes the composite
    produces; they are decoded into :class:`CustomCheckDetail` for the
    Grade's inline field. Trace-check verdicts and the summary come off
    the :class:`TraceChecksResult`; judge audit fields come off the
    optional :class:`JudgeResult`.
    """
    grade_components = _components_to_grade_components(components, state_checks_slot_component)
    custom_details = [
        CustomCheckDetail(
            check_name=r.check_name,
            status=r.status,
            score=r.score,
            message=r.message,
            details=json.loads(r.details_json) if r.details_json else None,
        )
        for r in custom_wire_results
    ] or None

    trace_check_results = [
        TraceConstraintResult(
            id=c.id,
            kind=c.kind,
            passed=c.passed,
            weight=c.weight,
            message=c.message,
            matched_positions=list(c.matched_positions),
            severity=c.severity,
            undecided=c.undecided,
        )
        for c in trace_result.constraints
    ]
    trace_checks_summary = TraceChecksSummary(
        winning_path=trace_result.winning_path,
        gate_failed=trace_result.gate_failed,
        failed_gate_ids=list(trace_result.failed_gate_ids),
        paths=[
            TracePathResult(id=p.id, score=p.score, gate_failed=p.gate_failed)
            for p in trace_result.paths
        ],
    )

    judge_usage = None
    judge_transcript = None
    judge_kb_gating = None
    judge_inputs = None
    judge_custom_prompt = None
    judge_agent_prompt_included = None
    criterion_results = None
    if judge_result is not None:
        judge_usage = JudgeUsage(
            calls=judge_result.usage.calls,
            prompt_tokens=judge_result.usage.prompt_tokens,
            completion_tokens=judge_result.usage.completion_tokens,
            reasoning_tokens=judge_result.usage.reasoning_tokens,
            cost_usd=judge_result.usage.cost_usd,
            tool_calls=judge_result.usage.tool_calls,
            consistency_rejections=judge_result.usage.consistency_rejections,
        )
        judge_transcript = list(judge_result.transcript)
        judge_kb_gating = JudgeKbGating(
            knowledge_search_disabled=judge_result.knowledge_search_disabled,
            offered=list(judge_result.kb_tools_offered),
            withheld=list(judge_result.kb_tools_withheld),
        )
        judge_inputs = JudgeInputs(
            state_diff_text=judge_result.state_diff,
            read_tools_offered=list(judge_result.read_tools_offered),
        )
        judge_custom_prompt = judge_result.custom_system_prompt
        judge_agent_prompt_included = judge_result.include_agent_system_prompt
        criterion_results = list(judge_result.criterion_results)

    return Grade(
        binary_pass=verdict_pass,
        score=max(0.0, min(1.0, verdict_score)),
        components=grade_components,
        reasons=reasons,
        custom_checks_details=custom_details,
        trace_check_results=trace_check_results,
        trace_checks_summary=trace_checks_summary,
        criterion_results=criterion_results,
        judge_status=judge_status,
        judge_usage=judge_usage,
        judge_transcript=judge_transcript,
        judge_kb_gating=judge_kb_gating,
        judge_inputs=judge_inputs,
        judge_custom_prompt=judge_custom_prompt,
        judge_agent_prompt_included=judge_agent_prompt_included,
    )
