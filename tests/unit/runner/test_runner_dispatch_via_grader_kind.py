"""``RunnerServiceImpl._dispatch_via_grader_kind`` — runtime dispatch coverage.

Drives ``GradeTrial`` end-to-end through a registered trial with
``grading_method='test_execution'`` and asserts the wire response for each
outcome cell. The runner routes non-composite kinds through the typed
:class:`GraderKind` seam; composite (or ``None``) stays on the runner-side
composite fold.

Five outcome cells:

- **Tool absent** — the substrate reports ``tool_absent=True``; the kind
  raises :class:`GraderKindRefusedError`; the dispatcher maps it to
  ``GradeTrialResponse(success=False, error=<reason>)``.
- **Script raised** — the substrate reports ``script_exec_error``; the
  kind returns ``Grade(0.0, "test.sh execution failed: ...")``; the
  dispatcher wraps it in ``success=True``.
- **Happy rc=0** — reward parseable, score matches the reward.
- **Happy rc≠0 (regression lock)** — a rc≠0 script that wrote a valid
  reward is scored by the reward. The dispatch does NOT gate on
  ``exit_code``. Uses the "test-execution reward" reasons format, NOT
  the "execution failed" one.
- **Composite unchanged** — a ``grading_method='composite'`` trial does
  NOT route through the kind seam; the runner's inline composite path
  handles it.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.utils.runner_requests import register_request, trial_spec_json
from tolokaforge.core.grading.substrate import RunTestSuiteResult
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.service import RunnerServiceImpl

pytestmark = pytest.mark.unit


def _task(grading_method: str, weights: dict[str, float] | None = None) -> dict[str, Any]:
    grading: dict[str, Any] = {"grading_method": grading_method}
    if weights is not None:
        grading["weights"] = weights
    return {
        "task_id": "dispatch_via_kind",
        "name": "dispatch_via_kind",
        "category": "test",
        "description": "Exercises _dispatch_via_grader_kind through GradeTrial.",
        "adapter_type": "native",
        "system_prompt": "You are a test assistant.",
        "initial_state": {"tables": {}, "schemas": []},
        "agent_tools": [],
        "user_tools": [],
        "grading": grading,
    }


@pytest.fixture
def service(db_client: Any) -> Any:
    impl = RunnerServiceImpl(db_client)
    try:
        yield impl
    finally:
        impl.shutdown()


def _register_and_return_trial_id(
    service: RunnerServiceImpl,
    mock_grpc_context: Any,
    trial_id: str,
    task: dict[str, Any],
) -> None:
    response = service.RegisterTrial(
        register_request(trial_spec_json(task, trial_id=trial_id), trial_id=trial_id),
        mock_grpc_context,
    )
    assert response.success is True, response.error


def _script_run_test_suite(service: RunnerServiceImpl, result: RunTestSuiteResult) -> None:
    """Monkeypatch the runner's ``_run_test_suite_via_agent_tools`` to return
    ``result`` regardless of arguments. ``_build_grading_substrate`` binds this
    method to the InProcess substrate via ``partial(m, trial_id=...)`` so the
    kind's ``substrate.run_test_suite(...)`` reaches this stub."""

    def stub(
        script_path: str,
        reward_path: str,
        timeout_s: float,
        reward_read_timeout_s: float,
        *,
        trial_id: str,
    ) -> RunTestSuiteResult:  # noqa: ARG001
        return result

    service._run_test_suite_via_agent_tools = stub  # type: ignore[method-assign]


def test_tool_absent_maps_to_success_false_with_reason(
    service: RunnerServiceImpl, mock_grpc_context: Any
) -> None:
    trial_id = "tool_absent:0"
    _register_and_return_trial_id(service, mock_grpc_context, trial_id, _task("test_execution"))
    reason = "no exec-capable env tool was found in this trial."
    _script_run_test_suite(
        service,
        RunTestSuiteResult(
            exit_code=0,
            reward_bytes=b"",
            stdout="",
            tool_absent=True,
            tool_absent_reason=reason,
            script_exec_error="",
        ),
    )

    response = service.GradeTrial(pb2.GradeTrialRequest(trial_id=trial_id), mock_grpc_context)

    assert response.success is False
    assert response.error == reason


def test_script_exec_error_maps_to_success_true_with_execution_failed_reasons(
    service: RunnerServiceImpl, mock_grpc_context: Any
) -> None:
    trial_id = "script_raised:0"
    _register_and_return_trial_id(service, mock_grpc_context, trial_id, _task("test_execution"))
    _script_run_test_suite(
        service,
        RunTestSuiteResult(
            exit_code=-1,
            reward_bytes=b"",
            stdout="",
            tool_absent=False,
            tool_absent_reason="",
            script_exec_error="TimeoutExpired: bash test.sh timed out after 300s",
        ),
    )

    response = service.GradeTrial(pb2.GradeTrialRequest(trial_id=trial_id), mock_grpc_context)

    assert response.success is True
    assert response.grade.binary_pass is False
    assert response.grade.score == pytest.approx(0.0)
    assert response.grade.reasons == (
        "test.sh execution failed: TimeoutExpired: bash test.sh timed out after 300s"
    )


def test_happy_rc_zero_scored_by_reward(service: RunnerServiceImpl, mock_grpc_context: Any) -> None:
    trial_id = "happy_rc_zero:0"
    _register_and_return_trial_id(service, mock_grpc_context, trial_id, _task("test_execution"))
    _script_run_test_suite(
        service,
        RunTestSuiteResult(
            exit_code=0,
            reward_bytes=b"0.85\n",
            stdout="PASS",
            tool_absent=False,
            tool_absent_reason="",
            script_exec_error="",
        ),
    )

    response = service.GradeTrial(pb2.GradeTrialRequest(trial_id=trial_id), mock_grpc_context)

    assert response.success is True
    assert response.grade.binary_pass is True
    assert response.grade.score == pytest.approx(0.85)
    assert response.grade.reasons == (
        "test-execution reward: 0.8500\n\ntest output (truncated):\nPASS"
    )


def test_happy_rc_nonzero_scored_by_reward_not_gated_on_exit_code(
    service: RunnerServiceImpl, mock_grpc_context: Any
) -> None:
    """Regression lock — dispatch must NOT gate on ``exit_code``. A rc≠0
    script that wrote a valid reward.txt is scored by the reward and uses
    the "test-execution reward" reasons format (NOT "execution failed")."""
    trial_id = "happy_rc_nonzero:0"
    _register_and_return_trial_id(service, mock_grpc_context, trial_id, _task("test_execution"))
    _script_run_test_suite(
        service,
        RunTestSuiteResult(
            exit_code=1,
            reward_bytes=b"0.7\n",
            stdout="FAIL: 1",
            tool_absent=False,
            tool_absent_reason="",
            script_exec_error="",
        ),
    )

    response = service.GradeTrial(pb2.GradeTrialRequest(trial_id=trial_id), mock_grpc_context)

    assert response.success is True
    assert response.grade.binary_pass is True
    assert response.grade.score == pytest.approx(0.7)
    assert response.grade.reasons.startswith("test-execution reward: 0.7000\n\n")
    assert "execution failed" not in response.grade.reasons


def test_composite_stays_on_runner_side_fold(
    service: RunnerServiceImpl, mock_grpc_context: Any
) -> None:
    """A ``grading_method='composite'`` trial does NOT route through the
    grader-kind seam. Scripting ``_run_test_suite_via_agent_tools`` to
    raise would trip if the composite path erroneously dispatched through
    the kind; instead the composite path runs and — since no golden
    actions / hash / tools are declared — the runner's composite fold
    produces its own verdict."""

    def raise_on_call(*args: Any, **kwargs: Any) -> RunTestSuiteResult:  # noqa: ARG001
        raise AssertionError(
            "composite dispatch reached run_test_suite — grader-kind seam "
            "must NOT route composite grading"
        )

    service._run_test_suite_via_agent_tools = raise_on_call  # type: ignore[method-assign]

    trial_id = "composite_stays_inline:0"
    _register_and_return_trial_id(
        service,
        mock_grpc_context,
        trial_id,
        _task("composite", weights={"state_checks": 1.0}),
    )

    response = service.GradeTrial(pb2.GradeTrialRequest(trial_id=trial_id), mock_grpc_context)

    # Composite path ran — the dispatch did not touch the kind seam. The
    # specific verdict depends on the runner's composite fold; the lock
    # here is that no AssertionError from ``raise_on_call`` propagated.
    assert response is not None
