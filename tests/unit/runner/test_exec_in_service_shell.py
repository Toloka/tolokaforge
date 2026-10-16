"""``RunnerServiceImpl._exec_in_service`` picks the shell by service, via the seam.

A collect hook reaches a trial container through the same staged-stdio
``docker exec`` seam the agent and test suite use (EPIPE-safe stdio,
partial-output read-back), not a raw ``subprocess.run``. The shell it runs under
depends on the service:

* ``service=None`` runs in the agent's own container, which ships bash, so a
  bash-specific collect command keeps working; and
* a named sidecar may be a minimal image with no bash, so its collect runs under
  POSIX ``sh``.

Both land on :func:`tolokaforge.runner.tool_factory._docker_exec_plan`, so the
assertions here read the ``docker exec`` argv at the ``subprocess.run`` boundary.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.core.grading.substrate import SubstrateUnreachableError
from tolokaforge.runner import service as service_module
from tolokaforge.runner.models import ToolSchema
from tolokaforge.runner.tool_factory import (
    DockerComposeExecToolWrapper,
    ToolLifecycleContext,
)

pytestmark = pytest.mark.unit


class _StubRunnerServiceImpl:
    """Bind just ``_exec_in_service`` onto a minimal instance.

    The full constructor spins up a gRPC server, a DB client and an LLM stack —
    none of which this method touches — so the method is bound to a plain object
    carrying only the ``trials`` registry it reads.
    """

    def __init__(self) -> None:
        self.trials: dict[str, object] = {}

    _exec_in_service = service_module.RunnerServiceImpl._exec_in_service


def _compose_wrapper() -> DockerComposeExecToolWrapper:
    schema = ToolSchema(
        name="bash",
        description="x",
        parameters={"type": "object", "properties": {}},
    )
    wrapper = DockerComposeExecToolWrapper(
        tool_schema=schema,
        service="main",
        compose_project_prefix="tbench_",
    )
    wrapper.start(ToolLifecycleContext(trial_id="task-1:0"))
    return wrapper


def _trial_context(bash_tool: object) -> MagicMock:
    ctx = MagicMock()
    ctx.agent_tools = {"bash": bash_tool}
    return ctx


def _fake_completed(stdout: str = "ok\n", stderr: str = "", returncode: int = 0) -> MagicMock:
    fake = MagicMock()
    fake.stdout = stdout
    fake.stderr = stderr
    fake.returncode = returncode
    return fake


def _run_exec_in_service(service: str | None) -> list[str]:
    """Run ``_exec_in_service`` on a compose-backed trial; return the docker argv."""
    svc = _StubRunnerServiceImpl()
    svc.trials["task-1:0"] = _trial_context(_compose_wrapper())
    with patch("subprocess.run", return_value=_fake_completed()) as run_mock:
        exit_code, output = svc._exec_in_service(
            service=service,
            command="collect",
            timeout_s=30.0,
            trial_id="task-1:0",
        )
    assert exit_code == 0
    assert output == "ok\n"
    assert run_mock.call_count == 1
    return run_mock.call_args.args[0]


def test_agent_path_collect_runs_under_bash() -> None:
    """``service=None`` on a compose-backed trial execs into the agent's own
    container under bash — a bash-specific collect command (process
    substitution, ``[[ ]]``, arrays) must not be downgraded to sh."""
    argv = _run_exec_in_service(service=None)
    assert argv[:5] == ["docker", "exec", "-i", "tbench_task-1_0_main", "bash"]
    assert argv[5] == "-c"
    # Last positional is the raw command, handed to the staged-stdio wrapper.
    assert argv[-1] == "collect"


def test_named_sidecar_collect_runs_under_sh() -> None:
    """A named sidecar may ship no bash, so its collect runs under POSIX sh in
    that sibling container of the per-trial stack."""
    argv = _run_exec_in_service(service="postgres")
    assert argv[:5] == ["docker", "exec", "-i", "tbench_task-1_0_postgres", "sh"]
    assert argv[5] == "-c"
    assert argv[-1] == "collect"


def test_operational_exec_failure_is_unreachable() -> None:
    """An OSError from the exec boundary surfaces as SubstrateUnreachableError."""
    svc = _StubRunnerServiceImpl()
    svc.trials["task-1:0"] = _trial_context(_compose_wrapper())
    with patch("subprocess.run", side_effect=OSError("docker gone")):
        with pytest.raises(SubstrateUnreachableError):
            svc._exec_in_service(
                service=None, command="collect", timeout_s=30.0, trial_id="task-1:0"
            )


def test_programming_bug_is_not_masked_as_unreachable() -> None:
    """A genuine bug (TypeError) propagates rather than being converted to a
    SubstrateUnreachableError 'infrastructure' condition."""
    svc = _StubRunnerServiceImpl()
    svc.trials["task-1:0"] = _trial_context(_compose_wrapper())
    with patch("subprocess.run", side_effect=TypeError("bug")):
        with pytest.raises(TypeError):
            svc._exec_in_service(
                service=None, command="collect", timeout_s=30.0, trial_id="task-1:0"
            )
