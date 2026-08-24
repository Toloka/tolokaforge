"""``GrpcGraderClient`` — the gRPC client the plug-in seam dials.

Analogous to :class:`~tolokaforge.core.shared_stack_runtime.GrpcRunnerClient`
but bound to the grader service's address rather than the runner's. Kept as a
thin wrapper so the seam's transport can be swapped without touching
``GraderRPCTrialGrader``.
"""

from __future__ import annotations

import grpc

from tolokaforge.grader import grader_pb2, grader_pb2_grpc


class GrpcGraderClient:
    """gRPC client for the standalone :class:`GraderServiceImpl`.

    Owns its own channel and stub. Callers construct one per grader (mirroring
    the runner client's lifecycle) so the plug-in seam does not share transport
    state across graders that may be pointed at different addresses.

    Usable as a context manager — ``with GrpcGraderClient(addr) as c:``
    closes the channel on exit. Long-lived callers that need to keep the
    client for a whole orchestrator run still hold the instance directly
    and call :meth:`close` in their teardown.
    """

    def __init__(self, grader_address: str = "grader:50052") -> None:
        self.grader_address = grader_address
        self.channel: grpc.Channel | None = None
        self.stub: grader_pb2_grpc.GraderServiceStub | None = None

    def connect(self) -> None:
        if self.channel is None:
            self.channel = grpc.insecure_channel(self.grader_address)
            self.stub = grader_pb2_grpc.GraderServiceStub(self.channel)

    def close(self) -> None:
        if self.channel is not None:
            self.channel.close()
            self.channel = None
            self.stub = None

    def __enter__(self) -> GrpcGraderClient:
        self.connect()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def health_check(self) -> bool:
        if not self.stub:
            self.connect()
        assert self.stub is not None
        response = self.stub.HealthCheck(grader_pb2.HealthCheckRequest())
        return response.status == grader_pb2.HealthCheckResponse.SERVING

    def grade(
        self,
        trial_id: str,
        llm_messages_json: str,
        termination_reason: str | None = None,
        task_config_json: str = "",
    ) -> dict:
        """Send a ``Grade`` RPC and return the same dict shape as
        :meth:`GrpcRunnerClient.grade_trial` — so ``GraderRPCTrialGrader`` can
        reuse the runner's ``_parse_grade_result`` mapping without diverging.
        """
        if not self.stub:
            self.connect()
        assert self.stub is not None
        request = grader_pb2.GradeRequest(
            trial_id=trial_id,
            llm_messages_json=llm_messages_json or "",
            termination_reason=termination_reason or "",
            task_config_json=task_config_json or "",
        )
        response = self.stub.Grade(request)
        result: dict = {
            "success": response.success,
            "error": response.error if response.error else None,
            "grade": None,
            # ``no_verdict`` carries the "nothing to grade" outcome distinct
            # from a grading failure. The seam consumer translates it back to
            # ``TrialGrader.grade`` returning ``None``.
            "no_verdict": response.no_verdict,
        }
        if response.success and not response.no_verdict and response.HasField("grade"):
            result["grade"] = _grade_from_wire(response.grade)
        return result


def _grade_from_wire(grade: grader_pb2.Grade) -> dict:
    """Translate the wire :class:`Grade` back into the dict shape
    :func:`~tolokaforge.core.trial_grader._parse_grade_result` consumes.

    Symmetric with :func:`~tolokaforge.grader.service._grade_to_wire`: every
    field the wire may carry is decoded here so the client hands back the
    same shape a runner-RPC grader's parse path would.
    """
    payload: dict = {
        "binary_pass": grade.binary_pass,
        "score": grade.score,
        "reasons": grade.reasons,
        "state_diff_json": grade.state_diff_json if grade.state_diff_json else None,
        "components": (
            _components_from_wire(grade.components) if grade.HasField("components") else {}
        ),
        "custom_checks": [
            {
                "check_name": cc.check_name,
                "status": cc.status,
                "score": cc.score,
                "message": cc.message,
                "details_json": cc.details_json,
            }
            for cc in grade.custom_checks
        ],
        "criterion_results": [
            {
                "id": cr.id,
                "met": cr.met,
                "score": cr.score,
                "justification": cr.justification,
            }
            for cr in grade.criterion_results
        ],
        "judge_status": grade.judge_status,
        "trace_checks": [
            {
                "id": r.id,
                "kind": r.kind,
                "passed": r.passed,
                "weight": r.weight,
                "message": r.message,
                "matched_positions": list(r.matched_positions),
                "severity": r.severity,
                "undecided": r.undecided,
            }
            for r in grade.trace_checks
        ],
    }
    if grade.HasField("judge_report"):
        payload["judge_report"] = _judge_report_from_wire(grade.judge_report)
    if grade.HasField("trace_checks_summary"):
        summary = grade.trace_checks_summary
        payload["trace_checks_summary"] = {
            "winning_path": summary.winning_path,
            "gate_failed": summary.gate_failed,
            "failed_gate_ids": list(summary.failed_gate_ids),
            "paths": [
                {"id": p.id, "score": p.score, "gate_failed": p.gate_failed} for p in summary.paths
            ],
        }
    return payload


def _components_from_wire(components: grader_pb2.GradeComponents) -> dict:
    """Decode :class:`GradeComponents`. ``trace_checks`` uses proto
    ``optional`` presence — omit the key when the wire did not carry it so
    the parse path can distinguish "not evaluated" from "-1.0 sentinel"."""
    out: dict = {
        "state_checks": components.state_checks,
        "transcript_rules": components.transcript_rules,
        "llm_judge": components.llm_judge,
        "custom_checks": components.custom_checks,
    }
    if components.HasField("trace_checks"):
        out["trace_checks"] = components.trace_checks
    return out


def _judge_report_from_wire(report: grader_pb2.JudgeReport) -> dict:
    """Decode :class:`JudgeReport` symmetrically with
    :func:`~tolokaforge.grader.service._populate_judge_report`."""
    out: dict = {
        "calls": report.calls,
        "prompt_tokens": report.prompt_tokens,
        "completion_tokens": report.completion_tokens,
        "reasoning_tokens": report.reasoning_tokens,
        "cost_usd": report.cost_usd,
        "tool_calls": report.tool_calls,
        "consistency_rejections": report.consistency_rejections,
        "knowledge_search_disabled": report.knowledge_search_disabled,
        "kb_tools_offered": list(report.kb_tools_offered),
        "kb_tools_withheld": list(report.kb_tools_withheld),
        "state_diff_text": report.state_diff_text,
        "read_tools_offered": list(report.read_tools_offered),
        "custom_system_prompt": report.custom_system_prompt,
    }
    if report.transcript_json:
        out["transcript_json"] = report.transcript_json
    # ``include_agent_system_prompt`` is proto ``optional`` — presence-gated
    # so ``_parse_grade_result`` can distinguish the tri-state (True / False /
    # not-set-by-a-legacy-sender).
    if report.HasField("include_agent_system_prompt"):
        out["include_agent_system_prompt"] = report.include_agent_system_prompt
    return out
