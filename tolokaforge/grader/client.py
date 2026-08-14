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
        }
        if response.success and response.grade:
            grade = response.grade
            result["grade"] = {
                "binary_pass": grade.binary_pass,
                "score": grade.score,
                "reasons": grade.reasons,
                "state_diff_json": (grade.state_diff_json if grade.state_diff_json else None),
                "components": {
                    "state_checks": grade.components.state_checks,
                    "transcript_rules": grade.components.transcript_rules,
                    "llm_judge": grade.components.llm_judge,
                    "custom_checks": grade.components.custom_checks,
                    "trace_checks": grade.components.trace_checks,
                },
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
            }
        return result
