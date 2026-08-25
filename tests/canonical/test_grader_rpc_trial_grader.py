"""``GraderRPCTrialGrader`` — the plug-in seam's third registered impl.

Structurally identical to ``RunnerRPCTrialGrader`` in dispatch shape, but
bound to the standalone grader service (:mod:`tolokaforge.grader`) instead
of the runner. Same call surface, same auto-fail branches, same wire
semantics — the difference is *what address the grader talks to*, which is
the whole point of ADR-0038 (grader service ships on its own image, own
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
    """Grader-client stand-in whose ``grade`` returns a fixed dict.

    Records every kwargs dict its :meth:`grade` was called with so tests can
    assert field-by-field on the wire payload the grader packed. The dict
    shape matches :meth:`GrpcGraderClient.grade` and the wire the composite
    dispatcher reads on the other end.
    """

    def __init__(self, result: dict) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def grade(self, **kwargs: object) -> dict:
        self.calls.append(kwargs)
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


def _make_grader(
    result: dict | None = None,
    *,
    runner_substrate_address: str = "runner:50051",
) -> tuple[GraderRPCTrialGrader, _StubGraderClient]:
    stub = _StubGraderClient(result or _success_result())
    grader = GraderRPCTrialGrader(
        grader_address="stub:0",
        logger=MagicMock(),
        runner_substrate_address=runner_substrate_address,
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
        assert stub.calls[0]["termination_reason"] == TerminationReason.MAX_TURNS.value


class TestWirePayload:
    """The grader packs every v2 wire field from the ``TrialSpec`` before it
    dials the client. A drift here would silently under-populate the request
    the composite dispatcher reads."""

    def test_grade_packs_every_v2_wire_field_from_spec(self) -> None:
        grader, stub = _make_grader()
        spec = make_trial_spec()
        grader.grade(
            spec,
            make_trajectory(status=TrialStatus.COMPLETED),
            "You are the agent.",
        )
        assert len(stub.calls) == 1
        call = stub.calls[0]
        assert call["trial_id"] == spec.trial_id
        # llm_messages_json is trajectory-derived — the client-side
        # ``encode_transcript_wire`` produces a JSON payload with the
        # policy leading system message; assert its shape is populated
        # rather than pin its exact bytes (encoder shape is locked
        # elsewhere).
        assert isinstance(call["llm_messages_json"], str)
        assert call["llm_messages_json"].startswith("[")
        assert call["task_config_json"] == spec.task.grading.model_dump_json()
        # No judge model configured on the default spec — the empty
        # judge_model_config_json is the fail-loud signal to the composite
        # dispatcher that this task has no ``llm_judge`` component.
        assert call["judge_model_config_json"] == ""
        assert call["task_description_json"] == spec.task.model_dump_json()
        assert call["runner_substrate_address"] == "runner:50051"
        assert call["agent_system_prompt"] == "You are the agent."


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

    def test_factory_threads_runner_address_to_runner_substrate_address(self) -> None:
        """``SubstrateService`` shares the runner's listen port, so
        ``ctx.runner_address`` is the same address the grader-side
        dispatcher dials for state reads."""
        ctx = TrialGraderContext(
            runner_address="runner.grid-01:50051",
            grader_address="grader.grid-02:50052",
            logger=MagicMock(),
        )
        grader = grader_rpc_trial_grader_factory(ctx)
        assert grader.runner_substrate_address == "runner.grid-01:50051"

    def test_registered_under_grader_rpc_entry_point(self) -> None:
        factory = load_trial_grader("grader_rpc")
        assert factory is grader_rpc_trial_grader_factory
