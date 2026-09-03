"""``SubstrateServicer.RunTestSuite`` — wire contract + outcome-cell coverage.

Drives the servicer's ``RunTestSuite`` handler through a live in-process gRPC
server + :class:`GrpcSubstrateClient` — the same round-trip an independent
grader reaches through in production. Locks the six outcome cells that make
up the substrate-side contract:

- **Happy path (rc=0)** — script exits cleanly, reward file present.
- **rc≠0 + reward-present** — a script that exits non-zero but wrote a valid
  reward is still surfaced with the reward on the wire; the servicer does
  NOT gate on ``exit_code`` (regression check).
- **Tool absent** — no exec-capable lifecycle tool in the trial's tools
  yields ``tool_absent=True`` with an actionable message, NOT an RPC error.
- **Script exec raises** — a subprocess exception (timeout, OSError, …)
  populates ``script_exec_error`` and returns with ``exit_code=-1``, empty
  stdout, empty reward_bytes. The RPC returns OK — the trial completed;
  the script's failure is a first-class outcome, not a substrate transport
  failure.
- **Reward-cat fallback** — the servicer runs the reward-cat command with
  the shell fallback ``|| echo 0.0`` so an absent reward file yields
  ``b"0.0\\n"`` on the wire.
- **Unknown trial** — a request for a trial the runner has not registered
  fails with ``grpc.StatusCode.NOT_FOUND``.
- **stdout wire cap** — the servicer truncates ``stdout`` at 65_536 bytes.
"""

from __future__ import annotations

import subprocess
from concurrent import futures
from contextlib import contextmanager
from typing import Any

import grpc
import pytest

from tolokaforge.runner import (
    add_SubstrateServiceServicer_to_server,
)
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner import runner_pb2_grpc as pb2_grpc
from tolokaforge.runner.models import RunnerInitialStateConfig, TaskDescription
from tolokaforge.runner.service import RunnerServiceImpl, TrialContextRuntime
from tolokaforge.runner.substrate_service import SubstrateServicer
from tolokaforge.runner.tool_factory import DockerComposeExecToolWrapper

pytestmark = pytest.mark.canonical


_TRIAL_ID = "task:0"


class _FakeDBClient:
    async def close(self) -> None:
        return None


class _StubBashTool(DockerComposeExecToolWrapper):
    """Real subclass so :func:`isinstance` recognises it; scripted exec calls.

    ``_exec_sync_with_rc`` returns entries from ``responses``; a ``responses``
    entry may be an :class:`BaseException` to signal a raised exec call.
    """

    def __init__(
        self,
        responses: list[tuple[int, str] | BaseException],
    ) -> None:
        # Skip the base ``__init__`` — its ToolSchemaModel path is out of
        # scope here. The fields the servicer consults are set explicitly.
        self._responses = list(responses)
        self.calls: list[tuple[str, float]] = []
        self._container = "stub_container"
        self._trial_id = _TRIAL_ID

    def _exec_sync_with_rc(  # type: ignore[override]
        self, command: str, timeout: float
    ) -> tuple[int, str]:
        self.calls.append((command, timeout))
        if not self._responses:
            raise AssertionError(f"unexpected exec call: {command!r}")
        entry = self._responses.pop(0)
        if isinstance(entry, BaseException):
            raise entry
        return entry


def _minimal_task_description() -> TaskDescription:
    return TaskDescription.model_validate(
        {
            "task_id": "run_test_suite_e2e",
            "name": "RunTestSuite servicer",
            "category": "test",
            "description": "In-process servicer wire lock",
            "adapter_type": "tau",
            "system_prompt": "You are a test assistant.",
            "initial_state": RunnerInitialStateConfig(tables={}).model_dump(),
            "agent_tools": [],
            "user_tools": [],
        }
    )


@contextmanager
def _running_servicer(*, agent_tools: dict[str, Any] | None):
    """Bring up an in-process gRPC server carrying ``SubstrateService`` and
    pre-register a trial with the given ``agent_tools`` on the runner.
    Yields ``(stub, runner)``."""
    runner = RunnerServiceImpl(db_client=_FakeDBClient())  # type: ignore[arg-type]
    if agent_tools is not None:
        trial_context = TrialContextRuntime(
            trial_id=_TRIAL_ID, task_description=_minimal_task_description()
        )
        trial_context.agent_tools = dict(agent_tools)
        runner.trials[_TRIAL_ID] = trial_context

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    add_SubstrateServiceServicer_to_server(SubstrateServicer(runner), server)
    port = server.add_insecure_port("[::]:0")
    server.start()
    try:
        with grpc.insecure_channel(f"localhost:{port}") as channel:
            yield pb2_grpc.SubstrateServiceStub(channel), runner
    finally:
        server.stop(grace=None)
        if runner._loop.is_running():
            runner._loop.call_soon_threadsafe(runner._loop.stop)


def _request(**overrides: Any) -> pb2.RunTestSuiteRequest:
    return pb2.RunTestSuiteRequest(
        trial_id=overrides.get("trial_id", _TRIAL_ID),
        script_path=overrides.get("script_path", "/tests/test.sh"),
        reward_path=overrides.get("reward_path", "/logs/verifier/reward.txt"),
        timeout_s=overrides.get("timeout_s", 0.0),
        reward_read_timeout_s=overrides.get("reward_read_timeout_s", 0.0),
    )


def test_happy_path_rc_zero_with_reward_present() -> None:
    tool = _StubBashTool(
        responses=[
            (0, "PASS: 42/42 tests"),
            (0, "0.85\n"),
        ]
    )
    with _running_servicer(agent_tools={"bash_env": tool}) as (stub, _runner):
        response = stub.RunTestSuite(_request())

    assert response.exit_code == 0
    assert response.reward_bytes == b"0.85\n"
    assert response.stdout == "PASS: 42/42 tests"
    assert response.tool_absent is False
    assert response.tool_absent_reason == ""
    assert response.script_exec_error == ""


def test_rc_nonzero_with_reward_is_surfaced_by_reward_not_exit_code() -> None:
    """The servicer does NOT gate on ``exit_code`` — a rc≠0 script that
    wrote a valid reward.txt is still surfaced with the reward on the wire.
    ``exit_code`` rides on the wire but ``script_exec_error`` is empty.

    The stub emits the ``\\n[exit code: N]\\n{stderr}`` suffix the real
    :meth:`DockerComposeExecToolWrapper._exec_sync_with_rc` appends on
    rc≠0, so the wire stdout preserves the exit-code marker — the same
    suffix a reader would see in ``Grade.reasons`` when the kind renders
    the merged stdout in its "test output (truncated):" block."""
    tool = _StubBashTool(
        responses=[
            (1, "FAIL: 1/42 tests\n[exit code: 1]\n"),
            (0, "0.5\n"),
        ]
    )
    with _running_servicer(agent_tools={"bash_env": tool}) as (stub, _runner):
        response = stub.RunTestSuite(_request())

    assert response.exit_code == 1
    assert response.reward_bytes == b"0.5\n"
    assert response.stdout == "FAIL: 1/42 tests\n[exit code: 1]\n"
    assert response.script_exec_error == ""


def test_tool_absent_is_first_class_outcome_not_rpc_error() -> None:
    """No exec-capable lifecycle tool in ``agent_tools`` — the RPC succeeds
    with ``tool_absent=True`` and an actionable reason. NOT ``UNIMPLEMENTED``
    / ``INTERNAL`` / ``NOT_FOUND``: the trial completed, it's just ungradeable
    via test-execution."""
    with _running_servicer(agent_tools={}) as (stub, _runner):
        response = stub.RunTestSuite(_request())

    assert response.tool_absent is True
    assert "test-execution grading was requested" in response.tool_absent_reason
    assert "no exec-capable env tool was found" in response.tool_absent_reason
    assert response.exit_code == 0
    assert response.reward_bytes == b""
    assert response.script_exec_error == ""


def test_script_exec_exception_populates_error_field_without_grpc_internal() -> None:
    """A subprocess exception (TimeoutExpired, OSError, …) from the script
    call populates ``script_exec_error``. The RPC returns OK — a gRPC
    ``INTERNAL`` status would flip the wire to ``success=False`` and lose
    the observable grade outcome (``Grade(0.0, "test.sh execution failed:
    {e}")``) the kind renders from this first-class field."""
    tool = _StubBashTool(
        responses=[
            subprocess.TimeoutExpired(cmd="bash test.sh", timeout=300.0),
        ]
    )
    with _running_servicer(agent_tools={"bash_env": tool}) as (stub, _runner):
        response = stub.RunTestSuite(_request())

    assert response.script_exec_error == str(
        subprocess.TimeoutExpired(cmd="bash test.sh", timeout=300.0)
    )
    assert "TimeoutExpired" not in response.script_exec_error
    assert response.exit_code == -1
    assert response.reward_bytes == b""
    assert response.stdout == ""
    assert response.tool_absent is False


def test_reward_file_absent_uses_shell_fallback_bytes() -> None:
    """The servicer runs the reward-cat command with the shell fallback
    ``cat X 2>/dev/null || echo 0.0``. An absent reward file therefore
    yields ``b"0.0\\n"`` on the wire (the same bytes callers see when the
    file is present with content ``0.0``)."""
    tool = _StubBashTool(
        responses=[
            (0, "test run OK"),
            (0, "0.0\n"),
        ]
    )
    with _running_servicer(agent_tools={"bash_env": tool}) as (stub, _runner):
        response = stub.RunTestSuite(_request())

    assert response.reward_bytes == b"0.0\n"
    assert response.script_exec_error == ""
    reward_call = tool.calls[1][0]
    assert "|| echo 0.0" in reward_call
    assert "2>/dev/null" in reward_call


def test_unknown_trial_returns_not_found() -> None:
    with _running_servicer(agent_tools=None) as (stub, _runner):
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.RunTestSuite(_request(trial_id="unknown_trial"))
    assert exc_info.value.code() == grpc.StatusCode.NOT_FOUND


def test_stdout_is_wire_capped_at_65536_bytes() -> None:
    big = "A" * 100_000
    tool = _StubBashTool(
        responses=[
            (0, big),
            (0, "0.0\n"),
        ]
    )
    with _running_servicer(agent_tools={"bash_env": tool}) as (stub, _runner):
        response = stub.RunTestSuite(_request())

    assert len(response.stdout) == 65_536
    assert response.stdout == big[:65_536]
