"""``RunnerServiceImpl._exec_in_service`` picks the shell and user, via the seam.

A collect hook reaches a trial container through the same staged-stdio
``docker exec`` seam the agent and test suite use (EPIPE-safe stdio,
partial-output read-back), not a raw ``subprocess.run``. The shell it runs under
depends on the service:

* ``service=None`` runs in the agent's own container, which ships bash, so a
  bash-specific collect command keeps working; and
* a named sidecar may be a minimal image with no bash, so its collect runs under
  POSIX ``sh``.

The user rides the same seam as ``--user``, so a pack that grades as root reads
its own sealed reward directory rather than being denied by the verifier image's
``USER``. An executor that resolves no container cannot carry one, and says so.

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


class _LocalExecTool:
    """An exec-capable tool that resolves no container (a local environment)."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def exec_in_env(self, command: str, timeout_s: float) -> str:
        return self.exec_in_env_with_exit_code(command, timeout_s)[1]

    def exec_in_env_with_exit_code(self, command: str, timeout_s: float) -> tuple[int, str]:
        del timeout_s
        self.commands.append(command)
        return 0, "ok\n"


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


def _run_exec_in_service(service: str | None, user: str | None = None) -> list[str]:
    """Run ``_exec_in_service`` on a compose-backed trial; return the docker argv."""
    svc = _StubRunnerServiceImpl()
    svc.trials["task-1:0"] = _trial_context(_compose_wrapper())
    with patch("subprocess.run", return_value=_fake_completed()) as run_mock:
        exit_code, output = svc._exec_in_service(
            service=service,
            command="collect",
            timeout_s=30.0,
            user=user,
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


def test_the_declared_user_reaches_docker_exec() -> None:
    """``--user`` lands on the exec that reads the verifier's own directory.

    The reward is read as the pack's user; a report beside it read as anyone
    else is denied on exactly the packs that seal their reward directory, and
    the denial is recorded as the report's contents.
    """
    argv = _run_exec_in_service(service="verifier", user="root")
    assert argv[:7] == [
        "docker",
        "exec",
        "--user",
        "root",
        "-i",
        "tbench_task-1_0_verifier",
        "sh",
    ]
    assert argv[-1] == "collect"


def test_no_user_leaves_the_container_default() -> None:
    """``user=None`` sends no ``--user``, so the container's own user runs it."""
    assert "--user" not in _run_exec_in_service(service="verifier")


def test_a_user_without_a_resolvable_container_is_refused() -> None:
    """An executor that names no container cannot carry a user, and says so.

    Running as the container's own user instead would read a path the task
    sealed against that user and record the denial as the command's output —
    the silence this parameter exists to end.
    """
    svc = _StubRunnerServiceImpl()
    tool = _LocalExecTool()
    svc.trials["task-1:0"] = _trial_context(tool)

    with pytest.raises(SubstrateUnreachableError, match="resolves no container"):
        svc._exec_in_service(
            service=None, command="collect", timeout_s=30.0, user="root", trial_id="task-1:0"
        )
    assert tool.commands == []


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
