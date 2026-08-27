"""Grader service implementation — the standalone Grade RPC.

Answers :class:`grader_pb2.GradeRequest` by delegating to an injected judge
callable and translating the returned :class:`~tolokaforge.core.models.Grade`
back to the wire type. Stateless per call: the caller supplies every field
the composite dispatch needs to grade the trial (trajectory as LLM messages,
termination reason, task-config JSON, judge-model-config JSON,
task-description JSON, runner substrate address) so the service can be pointed
at from any orchestrator without a prior registration handshake.

Standalone wiring — the CLI entry point at :mod:`tolokaforge.grader.__main__`
mounts :class:`~tolokaforge.grader.composite_dispatch.GraderCompositeDispatch`
as ``judge_fn``. Tests inject a stub callable that returns a canned
:class:`Grade`; the seam carries a :data:`JudgeGradeFn` callable so both the
standalone service and the in-process ``judge_only`` grader share the same
dispatch surface.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tolokaforge.core.models import (
    Grade,
    JudgeStatus,
    TraceChecksSummary,
    TraceConstraintResult,
)
from tolokaforge.grader import grader_pb2, grader_pb2_grpc

if TYPE_CHECKING:
    from tolokaforge.core.logging import StructuredLogger


@dataclass(frozen=True)
class GradeDispatch:
    """Wire-level dispatch payload the service hands to its judge callable.

    The standalone service is *stateless per call*: every field the injected
    dispatch needs to grade the trial travels on the wire and lands on this
    dataclass. The v2 fields (``judge_model_config_json`` through
    ``runner_substrate_address``) carry the run-scoped context the composite
    dispatcher would otherwise get in-process from ``RunnerServiceImpl`` —
    the grader constructs its :class:`LiveRunnerCallbackGradingSubstrate`
    against ``runner_substrate_address`` and deserialises the task-scoped
    ``TaskDescription`` / ``ModelConfig`` from the JSON fields. The agent
    system prompt already rides ``llm_messages_json`` as its leading system
    message; the composite dispatcher recovers it via
    :func:`split_leading_system_message`.
    """

    trial_id: str
    llm_messages_json: str
    termination_reason: str
    task_config_json: str
    judge_model_config_json: str
    task_description_json: str
    runner_substrate_address: str


JudgeGradeFn = Callable[[GradeDispatch], "Grade | None"]
"""Dispatch surface the service delegates to.

Takes a serialisable :class:`GradeDispatch` and returns a :class:`Grade` or
``None`` (nothing to grade). Kept intentionally narrower than
:data:`tolokaforge.core.trial_grader.JudgeGradeFn` (which accepts full
``TrialSpec`` / ``Trajectory`` objects) so the standalone service does not
need to reconstruct engine-side models with orchestrator-only fields.
"""


def _grade_to_wire(grade: Grade) -> grader_pb2.Grade:
    """Translate a Python :class:`Grade` to its wire equivalent.

    ``grade.reasons`` is typed ``str | dict[str, list[str]]`` — the dict
    shape carries per-component reasons a structured judge dispatch
    emits. JSON-encoded on the wire so downstream consumers can
    lazy-decode; the caller distinguishes the two by attempting a
    ``json.loads`` (a bare string is never valid JSON for a mapping).
    """
    if isinstance(grade.reasons, str):
        reasons_wire = grade.reasons
    else:
        reasons_wire = json.dumps(grade.reasons)
    wire = grader_pb2.Grade(
        binary_pass=grade.binary_pass,
        score=grade.score,
        reasons=reasons_wire,
        judge_status=_judge_status_to_wire(grade.judge_status),
    )
    if grade.state_diff is not None:
        # ``Grade.state_diff`` is the parsed dict; the wire carries it JSON-
        # encoded so downstream consumers can lazy-decode. Symmetric with
        # runner.Grade.state_diff_json which the ``_parse_grade_result`` mapper
        # deserialises on receipt.
        wire.state_diff_json = json.dumps(grade.state_diff)
    if grade.components is not None:
        wire.components.state_checks = _sentinel(grade.components.state_checks)
        wire.components.transcript_rules = _sentinel(grade.components.transcript_rules)
        wire.components.llm_judge = _sentinel(grade.components.llm_judge)
        wire.components.custom_checks = _sentinel(grade.components.custom_checks)
        # ``trace_checks`` uses proto ``optional`` presence, not the -1.0
        # sentinel — set it only when the source Grade actually carries one.
        if grade.components.trace_checks is not None:
            wire.components.trace_checks = grade.components.trace_checks
    if grade.custom_checks_details:
        wire.custom_checks.extend(
            grader_pb2.CustomCheckResult(
                check_name=c.check_name,
                status=c.status,
                score=c.score,
                message=c.message,
                details_json=json.dumps(c.details) if c.details else "",
            )
            for c in grade.custom_checks_details
        )
    if grade.criterion_results:
        wire.criterion_results.extend(
            grader_pb2.CriterionResult(
                id=c.id, met=c.met, score=c.score, justification=c.justification
            )
            for c in grade.criterion_results
        )
    _populate_judge_report(wire, grade)
    if grade.trace_check_results:
        wire.trace_checks.extend(_trace_constraint_to_wire(r) for r in grade.trace_check_results)
    if grade.trace_checks_summary is not None:
        _populate_trace_checks_summary(wire, grade.trace_checks_summary)
    return wire


def _populate_judge_report(wire: grader_pb2.Grade, grade: Grade) -> None:
    """Populate the ``JudgeReport`` sub-message from every field a live
    :class:`Grade` may carry. The wire ships every judge-side audit field so
    an offline replay reads the same shape a runner-RPC grade would.
    """
    usage = grade.judge_usage
    kb = grade.judge_kb_gating
    inputs = grade.judge_inputs
    transcript = grade.judge_transcript
    custom_prompt = grade.judge_custom_prompt
    include_agent_prompt = grade.judge_agent_prompt_included
    if all(
        value is None
        for value in (usage, kb, inputs, transcript, custom_prompt, include_agent_prompt)
    ):
        return
    report = wire.judge_report
    if usage is not None:
        report.calls = usage.calls
        report.prompt_tokens = usage.prompt_tokens
        report.completion_tokens = usage.completion_tokens
        report.reasoning_tokens = usage.reasoning_tokens
        report.cost_usd = usage.cost_usd
        report.tool_calls = usage.tool_calls
        report.consistency_rejections = usage.consistency_rejections
    if transcript is not None:
        report.transcript_json = json.dumps(transcript)
    if kb is not None:
        report.knowledge_search_disabled = kb.knowledge_search_disabled
        report.kb_tools_offered.extend(kb.offered)
        report.kb_tools_withheld.extend(kb.withheld)
    if inputs is not None:
        if inputs.state_diff_text is not None:
            report.state_diff_text = inputs.state_diff_text
        report.read_tools_offered.extend(inputs.read_tools_offered)
    if custom_prompt is not None:
        report.custom_system_prompt = custom_prompt
    if include_agent_prompt is not None:
        report.include_agent_system_prompt = include_agent_prompt


def _trace_constraint_to_wire(r: TraceConstraintResult) -> grader_pb2.TraceConstraintResult:
    return grader_pb2.TraceConstraintResult(
        id=r.id,
        kind=r.kind,
        passed=r.passed,
        weight=r.weight,
        message=r.message,
        matched_positions=list(r.matched_positions),
        severity=r.severity,
        undecided=r.undecided,
        withheld=r.withheld,
    )


def _populate_trace_checks_summary(wire: grader_pb2.Grade, summary: TraceChecksSummary) -> None:
    wire.trace_checks_summary.winning_path = summary.winning_path
    wire.trace_checks_summary.gate_failed = summary.gate_failed
    wire.trace_checks_summary.failed_gate_ids.extend(summary.failed_gate_ids)
    wire.trace_checks_summary.paths.extend(
        grader_pb2.TracePathResult(id=p.id, score=p.score, gate_failed=p.gate_failed)
        for p in summary.paths
    )


def _sentinel(value: float | None) -> float:
    """Encode ``None`` as the ``-1.0`` sentinel that runner.proto uses."""
    return -1.0 if value is None else value


def _judge_status_to_wire(status: JudgeStatus | None) -> int:
    if status is None:
        return grader_pb2.JUDGE_STATUS_UNSPECIFIED
    mapping = {
        JudgeStatus.UNSPECIFIED: grader_pb2.JUDGE_STATUS_UNSPECIFIED,
        JudgeStatus.COMPLETED: grader_pb2.JUDGE_STATUS_COMPLETED,
        JudgeStatus.ERRORED: grader_pb2.JUDGE_STATUS_ERRORED,
    }
    return mapping.get(status, grader_pb2.JUDGE_STATUS_UNSPECIFIED)


class GraderServiceImpl(grader_pb2_grpc.GraderServiceServicer):
    """Answers :meth:`Grade` by dispatching to an injected :data:`JudgeGradeFn`.

    A production integration constructs the service with a real ``LLMJudge``-
    backed dispatch (deferred follow-up); tests inject a stub callable that
    returns a canned :class:`Grade`. The service is stateless — each request
    carries everything the dispatch needs.
    """

    def __init__(self, judge_fn: JudgeGradeFn, logger: StructuredLogger) -> None:
        self.judge_fn = judge_fn
        self.logger = logger

    def Grade(  # noqa: N802 — matches the generated stub method name
        self,
        request: grader_pb2.GradeRequest,
        context: object,  # noqa: ARG002 — gRPC context, unused by this impl
    ) -> grader_pb2.GradeResponse:
        dispatch = GradeDispatch(
            trial_id=request.trial_id,
            llm_messages_json=request.llm_messages_json or "",
            termination_reason=request.termination_reason or "",
            task_config_json=request.task_config_json or "",
            judge_model_config_json=request.judge_model_config_json or "",
            task_description_json=request.task_description_json or "",
            runner_substrate_address=request.runner_substrate_address or "",
        )
        # Local import: ``tolokaforge.core.trial_grader`` imports
        # ``tolokaforge.grader.wire_snapshot`` at module top, which triggers
        # the ``tolokaforge.grader`` package init that pulls this module —
        # a top-level import here would close the cycle.
        from tolokaforge.core.trial_grader import GradingFailedError

        try:
            grade = self.judge_fn(dispatch)
        except (NotImplementedError, GradingFailedError) as exc:
            self.logger.info(
                "Grader service refused to grade",
                trial_id=request.trial_id,
                error=str(exc),
            )
            return grader_pb2.GradeResponse(success=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001 — surface everything to the caller
            self.logger.error(
                "Grader service raised while dispatching",
                trial_id=request.trial_id,
                error=str(exc),
            )
            return grader_pb2.GradeResponse(success=False, error=str(exc))
        if grade is None:
            # "Nothing to grade" — a trial the agent never got to run. Distinct
            # from a grading failure: the wire carries ``no_verdict=True`` so
            # the caller returns ``None`` at the ``TrialGrader`` seam rather
            # than raising ``GradingFailedError``.
            self.logger.info(
                "Grader service produced no verdict",
                trial_id=request.trial_id,
            )
            return grader_pb2.GradeResponse(success=True, no_verdict=True)
        self.logger.info(
            "Grader service produced a Grade",
            trial_id=request.trial_id,
            score=grade.score,
            binary_pass=grade.binary_pass,
        )
        return grader_pb2.GradeResponse(success=True, grade=_grade_to_wire(grade))

    def HealthCheck(  # noqa: N802 — matches the generated stub method name
        self,
        request: grader_pb2.HealthCheckRequest,  # noqa: ARG002 — proto contract requires the arg; unused by this impl
        context: object,  # noqa: ARG002 — gRPC context, unused by this impl
    ) -> grader_pb2.HealthCheckResponse:
        return grader_pb2.HealthCheckResponse(status=grader_pb2.HealthCheckResponse.SERVING)
