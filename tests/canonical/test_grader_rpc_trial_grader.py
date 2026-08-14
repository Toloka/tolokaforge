"""``GraderRPCTrialGrader`` — the plug-in seam's third registered impl.

Structurally identical to ``RunnerRPCTrialGrader`` in dispatch shape, but
bound to the standalone grader service (:mod:`tolokaforge.grader`) instead
of the runner. Same call surface, same auto-fail branches, same wire
semantics — the difference is *what address the grader talks to*, which is
the whole point of ADR-0035 (grader service ships on its own image, own
release cadence, own host class).

The service-side wire round-trip is exercised by the in-process gRPC
integration test in ``tests/canonical/test_grader_service_contract.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tests.canonical._factories import make_trajectory, make_trial_spec
from tolokaforge.core.models import Grade, TerminationReason, TrialStatus
from tolokaforge.core.plugin_registry import TrialGraderContext, load_trial_grader
from tolokaforge.core.trial_grader import (
    GraderRPCTrialGrader,
    GradingFailedError,
    TrialGrader,
    grader_rpc_trial_grader_factory,
)

pytestmark = pytest.mark.canonical


class _StubGraderClient:
    """Grader-client stand-in whose ``grade`` returns a fixed dict."""

    def __init__(self, result: dict) -> None:
        self.result = result
        self.calls: list[tuple[str, str, str | None]] = []

    def grade(
        self,
        trial_id: str,
        llm_messages_json: str,
        termination_reason: str | None = None,
        task_config_json: str = "",  # noqa: ARG002
    ) -> dict:
        self.calls.append((trial_id, llm_messages_json, termination_reason))
        return self.result


def _success_result() -> dict:
    return {
        "success": True,
        "grade": {
            "binary_pass": True,
            "score": 0.9,
            "components": {"llm_judge": 0.9},
            "reasons": "grader-service says yes",
        },
    }


def _make_grader(result: dict | None = None) -> tuple[GraderRPCTrialGrader, _StubGraderClient]:
    stub = _StubGraderClient(result or _success_result())
    grader = GraderRPCTrialGrader(
        grader_address="stub:0",
        logger=MagicMock(),
        grader_client=stub,
    )
    return grader, stub


class TestProtocolContract:
    def test_satisfies_trial_grader_protocol(self) -> None:
        grader, _ = _make_grader()
        assert isinstance(grader, TrialGrader)


class TestSuccessPath:
    def test_grade_dispatches_to_client_and_returns_parsed_grade(self) -> None:
        grader, stub = _make_grader()
        result = grader.grade(
            make_trial_spec(), make_trajectory(status=TrialStatus.COMPLETED), "sys"
        )
        assert isinstance(result, Grade)
        assert result.binary_pass is True
        assert result.score == 0.9
        assert len(stub.calls) == 1

    def test_grade_forwards_termination_reason_verbatim(self) -> None:
        grader, stub = _make_grader()
        traj = make_trajectory(
            status=TrialStatus.COMPLETED, termination_reason=TerminationReason.MAX_TURNS
        )
        grader.grade(make_trial_spec(), traj, "sys")
        assert stub.calls[0][2] == TerminationReason.MAX_TURNS.value


class TestAutoFailBranches:
    def test_error_status_short_circuits_without_calling_client(self) -> None:
        grader, stub = _make_grader()
        result = grader.grade(make_trial_spec(), make_trajectory(status=TrialStatus.ERROR), "sys")
        assert isinstance(result, Grade)
        assert result.binary_pass is False
        assert stub.calls == []

    def test_stuck_termination_short_circuits(self) -> None:
        grader, stub = _make_grader()
        traj = make_trajectory(
            status=TrialStatus.COMPLETED, termination_reason=TerminationReason.STUCK_DETECTED
        )
        result = grader.grade(make_trial_spec(), traj, "sys")
        assert isinstance(result, Grade)
        assert result.binary_pass is False
        assert stub.calls == []


class TestFailureIsLoud:
    def test_a_failed_rpc_raises_grading_failed_error(self) -> None:
        grader, _ = _make_grader({"success": False, "error": "no verdict"})
        with pytest.raises(GradingFailedError, match="no verdict"):
            grader.grade(make_trial_spec(), make_trajectory(status=TrialStatus.COMPLETED), "sys")


class TestFactoryAndRegistration:
    def test_factory_builds_grader_from_context(self) -> None:
        ctx = TrialGraderContext(runner_address="stub:0", logger=MagicMock())
        grader = grader_rpc_trial_grader_factory(ctx)
        assert isinstance(grader, GraderRPCTrialGrader)

    def test_registered_under_grader_rpc_entry_point(self) -> None:
        factory = load_trial_grader("grader_rpc")
        assert factory is grader_rpc_trial_grader_factory
