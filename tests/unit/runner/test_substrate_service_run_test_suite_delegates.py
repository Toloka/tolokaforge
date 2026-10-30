"""``SubstrateServicer.RunTestSuite`` delegates to the in-process helper.

The detached (wire) lane and the in-process grading substrate must run the
pack verifier through one implementation —
:meth:`RunnerServiceImpl._run_test_suite_via_agent_tools` — so the run-suite +
``cat {reward_path} 2>/dev/null || echo 0.0`` reward fallback cannot drift
between the two lanes. These tests pin that the servicer:

- threads ``script_path`` / ``reward_path`` / timeouts (with the servicer's
  ``0.0 → default`` fallback) / ``trial_id`` / ``service`` onto the helper,
- maps the helper's :class:`RunTestSuiteResult` onto the wire response
  field-for-field, applying the 65_536-byte stdout wire cap,
- carries the ``tool_absent`` / ``script_exec_error`` flags through, and
- answers gRPC ``NOT_FOUND`` for an unregistered trial without delegating.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import grpc
import pytest

from tolokaforge.core.grading.substrate import RunTestSuiteResult
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.substrate_service import SubstrateServicer

pytestmark = pytest.mark.unit


class _FakeRunner:
    """Stands in for ``RunnerServiceImpl``: a trials registry plus a recording
    ``_run_test_suite_via_agent_tools`` that returns a scripted result."""

    def __init__(self, result: RunTestSuiteResult, *, registered: bool = True) -> None:
        self.trials = {"trial-1": object()} if registered else {}
        self._result = result
        self.calls: list[dict[str, Any]] = []

    def _run_test_suite_via_agent_tools(
        self,
        script_path: str,
        reward_path: str,
        timeout_s: float,
        reward_read_timeout_s: float,
        *,
        trial_id: str,
        service: str | None = None,
        user: str | None = None,
        require_reward: bool = False,
    ) -> RunTestSuiteResult:
        self.calls.append(
            {
                "script_path": script_path,
                "reward_path": reward_path,
                "timeout_s": timeout_s,
                "reward_read_timeout_s": reward_read_timeout_s,
                "trial_id": trial_id,
                "service": service,
                "user": user,
                "require_reward": require_reward,
            }
        )
        return self._result


def _ok_result(stdout: str = "ok", reward: bytes = b"1.0\n") -> RunTestSuiteResult:
    return RunTestSuiteResult(
        exit_code=0,
        reward_bytes=reward,
        stdout=stdout,
        tool_absent=False,
        tool_absent_reason="",
        script_exec_error="",
    )


def test_delegates_with_threaded_params_and_default_timeouts() -> None:
    runner = _FakeRunner(_ok_result())
    servicer = SubstrateServicer(runner)  # type: ignore[arg-type]
    request = pb2.RunTestSuiteRequest(
        trial_id="trial-1",
        script_path="/t/run_tests.sh",
        reward_path="/t/reward.txt",
        timeout_s=0.0,
        reward_read_timeout_s=0.0,
        service="verifier",
    )
    context = MagicMock()

    servicer.RunTestSuite(request, context)

    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["script_path"] == "/t/run_tests.sh"
    assert call["reward_path"] == "/t/reward.txt"
    assert call["timeout_s"] == 300.0  # 0.0 → servicer default
    assert call["reward_read_timeout_s"] == 10.0  # 0.0 → servicer default
    assert call["trial_id"] == "trial-1"
    assert call["service"] == "verifier"
    # Unset on the wire → the helper's pre-existing defaults, so a caller that
    # knows nothing of these fields behaves exactly as it did before they existed.
    assert call["user"] is None
    assert call["require_reward"] is False
    context.set_code.assert_not_called()


def test_explicit_timeouts_pass_through_unchanged() -> None:
    runner = _FakeRunner(_ok_result())
    servicer = SubstrateServicer(runner)  # type: ignore[arg-type]
    request = pb2.RunTestSuiteRequest(
        trial_id="trial-1",
        script_path="/t/run.sh",
        reward_path="/t/reward",
        timeout_s=42.0,
        reward_read_timeout_s=7.0,
    )

    servicer.RunTestSuite(request, MagicMock())

    call = runner.calls[0]
    assert call["timeout_s"] == 42.0
    assert call["reward_read_timeout_s"] == 7.0
    assert call["service"] is None  # empty service string → None


def test_result_maps_onto_response_with_stdout_wire_cap() -> None:
    long_stdout = "x" * 100_000
    runner = _FakeRunner(_ok_result(stdout=long_stdout, reward=b"0.5\n"))
    servicer = SubstrateServicer(runner)  # type: ignore[arg-type]
    request = pb2.RunTestSuiteRequest(
        trial_id="trial-1", script_path="/t/run.sh", reward_path="/t/reward"
    )

    response = servicer.RunTestSuite(request, MagicMock())

    assert response.exit_code == 0
    assert response.reward_bytes == b"0.5\n"
    assert response.stdout == long_stdout[:65_536]
    assert response.tool_absent is False
    assert response.tool_absent_reason == ""
    assert response.script_exec_error == ""


def test_tool_absent_flag_rides_the_response() -> None:
    result = RunTestSuiteResult(
        exit_code=0,
        reward_bytes=b"",
        stdout="",
        tool_absent=True,
        tool_absent_reason="no exec-capable tool",
        script_exec_error="",
    )
    servicer = SubstrateServicer(_FakeRunner(result))  # type: ignore[arg-type]
    request = pb2.RunTestSuiteRequest(
        trial_id="trial-1", script_path="/t/run.sh", reward_path="/t/reward"
    )

    response = servicer.RunTestSuite(request, MagicMock())

    assert response.tool_absent is True
    assert response.tool_absent_reason == "no exec-capable tool"


def test_script_exec_error_rides_the_response() -> None:
    result = RunTestSuiteResult(
        exit_code=-1,
        reward_bytes=b"",
        stdout="",
        tool_absent=False,
        tool_absent_reason="",
        script_exec_error="TimeoutError: timed out",
    )
    servicer = SubstrateServicer(_FakeRunner(result))  # type: ignore[arg-type]
    request = pb2.RunTestSuiteRequest(
        trial_id="trial-1", script_path="/t/run.sh", reward_path="/t/reward"
    )

    response = servicer.RunTestSuite(request, MagicMock())

    assert response.exit_code == -1
    assert response.script_exec_error == "TimeoutError: timed out"


def test_unregistered_trial_is_not_found_and_does_not_delegate() -> None:
    runner = _FakeRunner(_ok_result(), registered=False)
    servicer = SubstrateServicer(runner)  # type: ignore[arg-type]
    request = pb2.RunTestSuiteRequest(
        trial_id="missing", script_path="/t/run.sh", reward_path="/t/reward"
    )
    context = MagicMock()

    response = servicer.RunTestSuite(request, context)

    context.set_code.assert_called_once_with(grpc.StatusCode.NOT_FOUND)
    assert runner.calls == []
    assert response.exit_code == 0
    assert response.reward_bytes == b""


def test_verifier_user_and_require_reward_cross_the_wire() -> None:
    """Both new request fields reach the shared helper.

    The detached lane has to carry them or a wire-graded trial silently falls
    back to the agent's user — the exact failure the fields exist to end, just
    one process further away.
    """
    runner = _FakeRunner(_ok_result())
    servicer = SubstrateServicer(runner)  # type: ignore[arg-type]
    request = pb2.RunTestSuiteRequest(
        trial_id="trial-1",
        script_path="/tests/test.sh",
        reward_path="/logs/verifier/reward.txt",
        user="root",
        require_reward=True,
    )

    servicer.RunTestSuite(request, MagicMock())

    assert runner.calls[0]["user"] == "root"
    assert runner.calls[0]["require_reward"] is True
