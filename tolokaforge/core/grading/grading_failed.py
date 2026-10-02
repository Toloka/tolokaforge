"""Structured refusal shared by host grading and the runner RPC service."""

from __future__ import annotations

from typing import Any

from tolokaforge.core.models import JudgeUsage
from tolokaforge.core.models.grade import GradingStateSnapshots


class GradingFailedError(Exception):
    """Grading ran and could not produce a verdict.

    The trial was measured, so its verdict exists to be computed and only the
    grading substrate can compute it. A host-side stand-in would land in
    ``success_rate``, ``avg_score``, ``pass@k`` and ``binary_pass`` as an agent
    failure that no measurement supports, so the failure is raised instead.

    The conductor's grading phase catches it, records the reason on
    ``Trajectory.grading_error`` and leaves ``grade`` unset. The trial keeps its
    own ``termination_reason``, writes its bundle, and counts as an attempt that
    scored nothing — never as an attempt the agent failed.
    """

    def __init__(
        self,
        message: str,
        *,
        judge_usage: JudgeUsage | None = None,
        state_snapshots: GradingStateSnapshots | None = None,
        state_diff: dict[str, Any] | None = None,
        comparison_view: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.judge_usage = judge_usage
        self.state_snapshots = state_snapshots
        self.state_diff = state_diff
        self.comparison_view = comparison_view

    def evidence(self) -> dict[str, Any]:
        """Structured work already completed before the verdict failed."""
        return {
            "judge_usage": self.judge_usage.model_dump(mode="json") if self.judge_usage else None,
            "state_snapshots": (
                self.state_snapshots.model_dump(mode="json") if self.state_snapshots else None
            ),
            "state_diff": self.state_diff,
            "comparison_view": self.comparison_view,
        }
