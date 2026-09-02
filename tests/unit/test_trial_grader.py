"""Unit tests for :class:`RunnerRPCTrialGrader` — the three grading branches.

Uses a stub :class:`RuntimeBackend` that captures ``grade_trial`` calls so
each branch's runner interaction is asserted directly. No gRPC involved.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.canonical._factories import make_trajectory, make_trial_spec
from tests.unit.test_failure_attribution import outcome_cells
from tolokaforge.core.failure_attribution import TrialOutcomeClass
from tolokaforge.core.models import Grade, JudgeStatus, TerminationReason, TrialStatus
from tolokaforge.core.trial_grader import GradingFailedError, RunnerRPCTrialGrader

pytestmark = pytest.mark.unit


class _StubBackend:
    """Records ``grade_trial`` calls; returns a canned result."""

    def __init__(self, grade_result: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._grade_result = grade_result or {
            "success": True,
            "grade": {
                "binary_pass": True,
                "score": 1.0,
                "components": {
                    "state_checks": 1.0,
                    "transcript_rules": -1.0,
                    "llm_judge": -1.0,
                    "custom_checks": -1.0,
                },
                "reasons": "ok",
            },
        }

    def grade_trial(
        self,
        trial_id: str,
        llm_messages_json: str | None = None,
        grading_components: list[str] | None = None,
        termination_reason: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "trial_id": trial_id,
                "llm_messages_json": llm_messages_json,
                "grading_components": grading_components,
                "termination_reason": termination_reason,
            }
        )
        return self._grade_result


def _make_grader(backend: _StubBackend | None = None) -> tuple[RunnerRPCTrialGrader, MagicMock]:
    logger = MagicMock()
    grader = RunnerRPCTrialGrader(
        runner_address="stub:0",
        logger=logger,
        runner_client=backend or _StubBackend(),
    )
    return grader, logger


class TestAutoFailBranches:
    """Trajectories that never reach the runner produce a synthesised
    fail-`Grade` without calling ``grade_trial`` — and log the auto-fail.
    """

    def test_error_status_auto_fails_and_logs(self) -> None:
        backend = _StubBackend()
        grader, logger = _make_grader(backend)
        traj = make_trajectory(status=TrialStatus.ERROR)

        grade = grader.grade(make_trial_spec(), traj, "sysprompt")

        assert grade.binary_pass is False
        assert grade.score == 0.0
        assert "Trial failed with status: error" in grade.reasons
        assert backend.calls == []
        logger.info.assert_called_once()
        call_args = logger.info.call_args
        assert call_args.args[0] == "Trial did not complete successfully - automatic fail"
        assert call_args.kwargs["status"] == "error"

    def test_timeout_status_auto_fails_and_logs(self) -> None:
        backend = _StubBackend()
        grader, logger = _make_grader(backend)
        traj = make_trajectory(status=TrialStatus.TIMEOUT)

        grade = grader.grade(make_trial_spec(), traj, "sysprompt")

        assert grade.binary_pass is False
        assert "Trial failed with status: timeout" in grade.reasons
        assert backend.calls == []
        assert logger.info.call_args.kwargs["status"] == "timeout"

    def test_stuck_detected_auto_fails_and_logs(self) -> None:
        backend = _StubBackend()
        grader, logger = _make_grader(backend)
        traj = make_trajectory(
            status=TrialStatus.COMPLETED,
            termination_reason=TerminationReason.STUCK_DETECTED,
        )

        grade = grader.grade(make_trial_spec(), traj, "sysprompt")

        assert grade.binary_pass is False
        assert "stuck" in grade.reasons.lower()
        assert backend.calls == []
        assert logger.info.call_args.args[0] == "Trial stuck - automatic fail"
        assert logger.info.call_args.kwargs["termination_reason"] == "stuck_detected"


def _make_auto_fail_grader(kind: str) -> Any:
    """One :class:`TrialGrader` per registered subclass, wired with a stub
    dispatch that must not be reached — every auto-fail branch here refuses to
    dispatch to its evaluator, so any recorded call is a defect.
    """
    from tolokaforge.core.trial_grader import (
        GraderRPCTrialGrader,
        JudgeBackedTrialGrader,
        QueueTrialGrader,
        RunnerRPCTrialGrader,
    )
    from tolokaforge.grader.queue import InMemoryGradeBroker

    logger = MagicMock()
    if kind == "runner_rpc":

        class _RecordingBackend:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            def grade_trial(self, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover
                self.calls.append(kwargs)
                return {"success": True, "grade": None}

        return RunnerRPCTrialGrader(
            runner_address="stub:0", logger=logger, runner_client=_RecordingBackend()
        )
    if kind == "judge_backed":

        def _refusing_judge(*_args: Any, **_kwargs: Any) -> Grade:  # pragma: no cover
            raise AssertionError("judge_fn must not be called on an auto-fail branch")

        return JudgeBackedTrialGrader(judge_fn=_refusing_judge, logger=logger)
    if kind == "grader_rpc":

        class _RefusingGraderClient:
            def grade(self, **_kwargs: Any) -> dict[str, Any]:  # pragma: no cover
                raise AssertionError("grader_client.grade must not be called on auto-fail")

            def close(self) -> None:
                return None

        return GraderRPCTrialGrader(
            grader_address="stub:0",
            logger=logger,
            runner_substrate_address="runner:50051",
            grader_client=_RefusingGraderClient(),  # type: ignore[arg-type]
        )
    if kind == "queue":
        return QueueTrialGrader(
            broker=InMemoryGradeBroker(),
            logger=logger,
            runner_substrate_address="runner:50051",
            timeout_s=1.0,
        )
    raise AssertionError(f"unknown TrialGrader kind: {kind}")


_SYNTH_ROWS: tuple[tuple[str, TrialStatus, TerminationReason | None, TerminationReason], ...] = (
    # (subclass_kind, trial_status, trajectory_termination_reason, expected_marker)
    ("runner_rpc", TrialStatus.ERROR, None, TerminationReason.ERROR),
    ("runner_rpc", TrialStatus.TIMEOUT, None, TerminationReason.ERROR),
    (
        "runner_rpc",
        TrialStatus.COMPLETED,
        TerminationReason.STUCK_DETECTED,
        TerminationReason.STUCK_DETECTED,
    ),
    (
        "runner_rpc",
        TrialStatus.FAILED,
        TerminationReason.EMPTY_COMPLETION,
        TerminationReason.EMPTY_COMPLETION,
    ),
    ("judge_backed", TrialStatus.ERROR, None, TerminationReason.ERROR),
    ("judge_backed", TrialStatus.TIMEOUT, None, TerminationReason.ERROR),
    (
        "judge_backed",
        TrialStatus.COMPLETED,
        TerminationReason.STUCK_DETECTED,
        TerminationReason.STUCK_DETECTED,
    ),
    (
        "judge_backed",
        TrialStatus.FAILED,
        TerminationReason.EMPTY_COMPLETION,
        TerminationReason.EMPTY_COMPLETION,
    ),
    ("grader_rpc", TrialStatus.ERROR, None, TerminationReason.ERROR),
    ("grader_rpc", TrialStatus.TIMEOUT, None, TerminationReason.ERROR),
    (
        "grader_rpc",
        TrialStatus.COMPLETED,
        TerminationReason.STUCK_DETECTED,
        TerminationReason.STUCK_DETECTED,
    ),
    (
        "grader_rpc",
        TrialStatus.FAILED,
        TerminationReason.EMPTY_COMPLETION,
        TerminationReason.EMPTY_COMPLETION,
    ),
    ("queue", TrialStatus.ERROR, None, TerminationReason.ERROR),
    ("queue", TrialStatus.TIMEOUT, None, TerminationReason.ERROR),
    (
        "queue",
        TrialStatus.COMPLETED,
        TerminationReason.STUCK_DETECTED,
        TerminationReason.STUCK_DETECTED,
    ),
    (
        "queue",
        TrialStatus.FAILED,
        TerminationReason.EMPTY_COMPLETION,
        TerminationReason.EMPTY_COMPLETION,
    ),
)


@pytest.mark.parametrize(
    ("kind", "status", "reason", "expected_marker"),
    _SYNTH_ROWS,
    ids=[f"{k}-{s.value}-{r.value if r else 'none'}" for k, s, r, _ in _SYNTH_ROWS],
)
def test_auto_fail_synthesis_carries_marker_and_empty_components(
    kind: str,
    status: TrialStatus,
    reason: TerminationReason | None,
    expected_marker: TerminationReason,
) -> None:
    """Every ``TrialGrader`` subclass on every auto-fail branch synthesises a
    :class:`Grade` with ``GradeComponents()`` (all fields ``None``) and the
    ``synthesized_by_termination_reason`` marker set to the trajectory's
    ``TerminationReason`` (or ``ERROR`` when the status is ERROR/TIMEOUT with
    no matching reason). ``binary_pass=False``, ``score=0.0``.

    The parametrisation covers four subclasses × four branches so a subclass
    that quietly resurrects the fabricated ``state_checks=0.0`` shape reds
    here rather than in a downstream analytics-consumer test.
    """
    grader = _make_auto_fail_grader(kind)
    trajectory = make_trajectory(status=status, termination_reason=reason)

    grade = grader.grade(make_trial_spec(), trajectory, "sysprompt")

    assert grade is not None, f"{kind} refused to grade instead of synthesising"
    assert grade.binary_pass is False
    assert grade.score == 0.0
    assert grade.synthesized_by_termination_reason is expected_marker
    # Every component field reads as ``None`` — nothing was measured. The
    # fabricated ``state_checks=0.0`` / ``llm_judge=0.0`` shape the auto-fail
    # branches used to emit is gone; downstream analytics can tell an
    # auto-failed trial from one that measured ``state_checks: 0.0``.
    components = grade.components
    assert components.state_checks is None
    assert components.transcript_rules is None
    assert components.trace_checks is None
    assert components.llm_judge is None
    assert components.custom_checks is None


class TestNoVerdictProducesNoGrade:
    """A trial no verdict can be computed for is not graded at all.

    ``None`` rather than ``Grade(score=0.0)``: ``Grade.score`` is a required
    ``[0, 1]`` float, so any grade here has to carry a number describing work
    nobody did. Absence cannot be misread as zero, and a consumer that forgets to
    branch fails loudly instead of quietly reporting a model failure.

    Two conditions answer that way, and they are not the same condition. A trial
    the infrastructure killed never ran, and leaves the denominator as well. A
    trial whose runner lost it *did* run and is counted — as our defect — but the
    party that would compute its verdict is the one that lost it.
    """

    @pytest.mark.parametrize("cell", outcome_cells())
    def test_none_exactly_for_the_abort_and_lost_cells(self, cell) -> None:
        status, reason, outcome_class, _ = cell
        backend = _StubBackend()
        grader, _ = _make_grader(backend)

        grade = grader.grade(
            make_trial_spec(),
            make_trajectory(status=status, termination_reason=reason),
            "sysprompt",
        )

        if (
            outcome_class is TrialOutcomeClass.INFRASTRUCTURE_ABORT
            or reason is TerminationReason.TRIAL_LOST
        ):
            assert grade is None
            assert backend.calls == [], "an ungraded trial must not reach the runner"
        else:
            assert isinstance(grade, Grade)

    def test_the_abort_is_logged_with_its_reason(self) -> None:
        backend = _StubBackend()
        grader, logger = _make_grader(backend)

        grader.grade(
            make_trial_spec(),
            make_trajectory(
                status=TrialStatus.ERROR, termination_reason=TerminationReason.RATE_LIMIT
            ),
            "sysprompt",
        )

        assert logger.info.call_args.args[0] == "Trial aborted by infrastructure - not graded"
        assert logger.info.call_args.kwargs["termination_reason"] == "rate_limit"


class TestRunnerRPCBranch:
    """Completed trajectories dispatch to ``grade_trial`` and materialise
    the returned dict into a :class:`Grade`.
    """

    def test_success_path_produces_grade_and_logs(self) -> None:
        backend = _StubBackend()
        grader, logger = _make_grader(backend)
        traj = make_trajectory(status=TrialStatus.COMPLETED)

        grade = grader.grade(make_trial_spec(), traj, "sysprompt")

        assert grade.binary_pass is True
        assert grade.score == 1.0
        assert grade.components.state_checks == 1.0
        assert len(backend.calls) == 1
        assert backend.calls[0]["trial_id"] == "task-1:0"
        logger.info.assert_called_once()
        assert logger.info.call_args.args[0] == "Grading via Runner RPC"

    def test_grpc_failure_raises_instead_of_scoring_the_trial_zero(self) -> None:
        """A failed grading run publishes no verdict at all.

        A normally-terminated trial classifies ``MEASURED``, so a host-side
        ``score=0.0`` would enter ``success_rate`` / ``avg_score`` / ``pass@k``
        as an agent failure that grading never established.
        """
        backend = _StubBackend(
            grade_result={"success": False, "grade": None, "error": "runner exploded"}
        )
        grader, logger = _make_grader(backend)
        traj = make_trajectory(status=TrialStatus.COMPLETED)

        with pytest.raises(GradingFailedError) as excinfo:
            grader.grade(make_trial_spec(), traj, "sysprompt")

        assert "runner exploded" in str(excinfo.value)
        assert "task-1:0" in str(excinfo.value)
        logger.error.assert_called_once()
        assert logger.error.call_args.args[0] == "Grading RPC failed"
        assert logger.error.call_args.kwargs["error"] == "runner exploded"

    def test_a_successful_rpc_carrying_no_grade_also_raises(self) -> None:
        backend = _StubBackend(grade_result={"success": True, "grade": None})
        grader, _ = _make_grader(backend)
        traj = make_trajectory(status=TrialStatus.COMPLETED)

        with pytest.raises(GradingFailedError):
            grader.grade(make_trial_spec(), traj, "sysprompt")

    def test_judge_report_populates_judge_usage(self) -> None:
        backend = _StubBackend(
            grade_result={
                "success": True,
                "grade": {
                    "binary_pass": True,
                    "score": 0.75,
                    "components": {"state_checks": 1.0, "llm_judge": 0.5},
                    "reasons": "partial",
                    "judge_report": {
                        "calls": 2,
                        "prompt_tokens": 100,
                        "completion_tokens": 50,
                        "reasoning_tokens": 10,
                        "cost_usd": 0.001,
                        "tool_calls": 1,
                        "consistency_rejections": 1,
                    },
                    "judge_status": 1,
                },
            }
        )
        grader, _ = _make_grader(backend)
        traj = make_trajectory(status=TrialStatus.COMPLETED)

        grade = grader.grade(make_trial_spec(), traj, "sysprompt")

        assert grade.judge_usage is not None
        assert grade.judge_usage.calls == 2
        assert grade.judge_usage.prompt_tokens == 100
        assert grade.judge_usage.cost_usd == 0.001
        assert grade.judge_usage.consistency_rejections == 1
        assert grade.judge_status == JudgeStatus.from_proto(1)

    def test_state_diff_json_parses(self) -> None:
        backend = _StubBackend(
            grade_result={
                "success": True,
                "grade": {
                    "binary_pass": False,
                    "score": 0.0,
                    "components": {"state_checks": 0.0},
                    "reasons": "diff",
                    "state_diff_json": '{"missing_rows": ["a", "b"]}',
                },
            }
        )
        grader, _ = _make_grader(backend)
        traj = make_trajectory(status=TrialStatus.COMPLETED)

        grade = grader.grade(make_trial_spec(), traj, "sysprompt")

        assert grade.state_diff == {"missing_rows": ["a", "b"]}

    def test_state_diff_json_malformed_is_ignored(self) -> None:
        backend = _StubBackend(
            grade_result={
                "success": True,
                "grade": {
                    "binary_pass": False,
                    "score": 0.0,
                    "components": {"state_checks": 0.0},
                    "reasons": "diff",
                    "state_diff_json": "not-json",
                },
            }
        )
        grader, _ = _make_grader(backend)
        traj = make_trajectory(status=TrialStatus.COMPLETED)

        grade = grader.grade(make_trial_spec(), traj, "sysprompt")

        assert grade.state_diff is None


class TestTerminationReasonForwarding:
    """The grader hands the runner the trial's own termination reason, as its
    wire value, so grading can tell a deliberate finish from a spent budget."""

    def test_reason_crosses_as_its_wire_value(self) -> None:
        backend = _StubBackend()
        grader, _ = _make_grader(backend)
        traj = make_trajectory(
            status=TrialStatus.COMPLETED, termination_reason=TerminationReason.AGENT_DONE
        )

        grader.grade(make_trial_spec(), traj, "sysprompt")

        assert backend.calls[0]["termination_reason"] == "agent_done"

    def test_trajectory_without_a_reason_forwards_none(self) -> None:
        backend = _StubBackend()
        grader, _ = _make_grader(backend)
        traj = make_trajectory(status=TrialStatus.COMPLETED, termination_reason=None)

        grader.grade(make_trial_spec(), traj, "sysprompt")

        assert backend.calls[0]["termination_reason"] is None


class TestJudgeMessagesJson:
    """The transcript sent to the runner encodes the agent's policy as
    a leading ``system`` message. Empty trajectory + empty prompt yields
    ``None`` (nothing to grade).
    """

    def test_empty_trajectory_and_prompt_sends_none(self) -> None:
        backend = _StubBackend()
        grader, _ = _make_grader(backend)
        traj = make_trajectory(status=TrialStatus.COMPLETED)

        grader.grade(make_trial_spec(), traj, "")

        assert backend.calls[0]["llm_messages_json"] is None

    def test_prompt_alone_still_sends_messages(self) -> None:
        import json

        backend = _StubBackend()
        grader, _ = _make_grader(backend)
        traj = make_trajectory(status=TrialStatus.COMPLETED)

        grader.grade(make_trial_spec(), traj, "you are a helper")

        assert backend.calls[0]["llm_messages_json"] is not None
        parsed = json.loads(backend.calls[0]["llm_messages_json"])
        assert parsed == [{"role": "system", "content": "you are a helper"}]


class TestRuntimeBackendDispatchPreference:
    """When ``runtime_backend`` is set on the grader (in-process per-trial
    routing), :meth:`grade` dispatches through it — not through the
    address-built client. When ``runtime_backend`` is ``None`` (the P2/P3
    on-the-wire shape), the address-built client is the target. This is the
    regression this PR fixes: on ``PerTrialRuntimeBackend``, the client was
    bound to ``""`` and every ``grade_trial`` call failed with
    :class:`_InactiveRpcError`.
    """

    def test_runtime_backend_wins_when_both_are_set(self) -> None:
        """Grade a COMPLETED trajectory with both a runtime_backend and a
        runner_client set. Only the runtime_backend must record the call —
        the fallback client stays silent."""
        backend_target = _StubBackend()
        client_target = _StubBackend()
        grader = RunnerRPCTrialGrader(
            runner_address="stub:0",
            logger=MagicMock(),
            runner_client=client_target,
            runtime_backend=backend_target,  # type: ignore[arg-type]
        )

        grader.grade(
            make_trial_spec(),
            make_trajectory(status=TrialStatus.COMPLETED),
            "sysprompt",
        )

        assert len(backend_target.calls) == 1
        assert backend_target.calls[0]["trial_id"].endswith(":0")
        assert client_target.calls == []

    def test_runner_client_used_when_runtime_backend_is_none(self) -> None:
        """The P2/P3 on-the-wire shape — no runtime_backend, only an
        address-built client. This locks that Stage 2's routing change did
        not regress the out-of-process path."""
        client_target = _StubBackend()
        grader = RunnerRPCTrialGrader(
            runner_address="stub:0",
            logger=MagicMock(),
            runner_client=client_target,
            runtime_backend=None,
        )

        grader.grade(
            make_trial_spec(),
            make_trajectory(status=TrialStatus.COMPLETED),
            "sysprompt",
        )

        assert len(client_target.calls) == 1
        assert client_target.calls[0]["trial_id"].endswith(":0")

    def test_factory_threads_runtime_backend_from_context(self) -> None:
        """``runner_rpc_trial_grader_factory(ctx)`` must pull
        ``ctx.runtime_backend`` into the grader when set, AND skip building
        a live gRPC client (nothing to dial — the backend is the target)."""
        from tolokaforge.core.plugin_registry import TrialGraderContext
        from tolokaforge.core.trial_grader import runner_rpc_trial_grader_factory

        backend_target = _StubBackend()
        ctx = TrialGraderContext(
            runner_address=None,
            logger=MagicMock(),
            runtime_backend=backend_target,  # type: ignore[arg-type]
        )
        grader = runner_rpc_trial_grader_factory(ctx)

        assert grader.runtime_backend is backend_target
        assert grader.runner_client is None
