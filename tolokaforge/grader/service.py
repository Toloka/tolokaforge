"""Grader service implementation — the standalone Grade RPC.

Answers :class:`grader_pb2.GradeRequest` by delegating to an injected judge
callable and translating the returned :class:`~tolokaforge.core.models.Grade`
back to the wire type. Stateless per call: the caller supplies everything
the grader needs (trajectory as LLM messages, termination reason,
task-config JSON) so the service can be pointed at from any orchestrator
without a prior registration handshake.

Real-judge wiring (constructing an :class:`~tolokaforge.core.grading.judge.LLMJudge`
from per-task rubric config) is deferred to the milestone follow-up on
:mod:`~tolokaforge.core.trial_grader.JudgeBackedTrialGrader`; the seam
carries a ``JudgeGradeFn`` callable so both the standalone service and the
in-process ``judge_only`` grader share the same dispatch surface.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tolokaforge.core.models import (
    Grade,
    JudgeStatus,
)
from tolokaforge.grader import grader_pb2, grader_pb2_grpc

if TYPE_CHECKING:
    from tolokaforge.core.logging import StructuredLogger


@dataclass(frozen=True)
class GradeDispatch:
    """Wire-level dispatch payload the service hands to its judge callable.

    The standalone service is *stateless per call*: everything the injected
    dispatch needs to run — the trial identifier, the encoded transcript, the
    termination-reason string, and optional task-config JSON — travels on the
    wire. This dataclass captures that shape without pulling in the engine's
    Pydantic ``TrialSpec`` / ``Trajectory`` types, which carry
    orchestrator-only fields (env-endpoints, seeds, run-scoped IDs) the
    service cannot know about.

    Production wiring adapts :class:`GradeDispatch` back to real ``TrialSpec``
    / ``Trajectory`` values before dispatching to :class:`LLMJudge`; that
    adapter lives on the milestone follow-up.
    """

    trial_id: str
    llm_messages_json: str
    termination_reason: str
    task_config_json: str


JudgeGradeFn = Callable[[GradeDispatch], "Grade | None"]
"""Dispatch surface the service delegates to.

Takes a serialisable :class:`GradeDispatch` and returns a :class:`Grade` or
``None`` (nothing to grade). Kept intentionally narrower than
:data:`tolokaforge.core.trial_grader.JudgeGradeFn` (which accepts full
``TrialSpec`` / ``Trajectory`` objects) so the standalone service does not
need to reconstruct engine-side models with orchestrator-only fields.
"""


def _grade_to_wire(grade: Grade) -> grader_pb2.Grade:
    """Translate a Python :class:`Grade` to its wire equivalent."""
    wire = grader_pb2.Grade(
        binary_pass=grade.binary_pass,
        score=grade.score,
        reasons=grade.reasons or "",
        judge_status=_judge_status_to_wire(grade.judge_status),
    )
    if grade.components is not None:
        wire.components.state_checks = _sentinel(grade.components.state_checks)
        wire.components.transcript_rules = _sentinel(grade.components.transcript_rules)
        wire.components.llm_judge = _sentinel(grade.components.llm_judge)
        wire.components.custom_checks = _sentinel(grade.components.custom_checks)
        wire.components.trace_checks = _sentinel(grade.components.trace_checks)
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
    return wire


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
        )
        try:
            grade = self.judge_fn(dispatch)
        except NotImplementedError as exc:
            self.logger.info(
                "Grader service invoked with unwired judge dispatch",
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
            self.logger.info(
                "Grader service produced no verdict",
                trial_id=request.trial_id,
            )
            return grader_pb2.GradeResponse(success=False, error="no verdict")
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
