"""Every selectable agent tool is also the trial's grading executor.

A terminal-bench trial hands its agent exactly one tool, and grades by running
the pack's verifier inside the same environment. Grading finds its executor by
capability — :func:`~tolokaforge.runner.env_exec.first_env_exec_tool` over the
trial's registered ``agent_tools`` — so the agent's one tool is also the only
candidate. A selectable tool that does not satisfy
:class:`~tolokaforge.runner.env_exec.SupportsEnvExec` makes every trial run on
it ungradeable, with the failure surfacing only after a real container has run
a real model: the lookup and the tool menu are in different packages and
nothing else joins them.

This file joins them. For each value ``adapter_params.agent_tool`` accepts it
builds the schema the adapter emits, runs that schema through the runner's own
factory dispatch, and asserts the wrapper that comes back can exec.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from tolokaforge_adapter_terminal_bench.adapter import AGENT_TOOLS, TerminalBenchAdapter

from tolokaforge.runner.compose_naming import compose_container_name
from tolokaforge.runner.env_exec import SupportsEnvExec, first_env_exec_tool
from tolokaforge.runner.tool_factory import (
    _STAGED_STDIO_SCRIPT,
    PersistentShellToolWrapper,
    ToolFactory,
    ToolLifecycleContext,
)

pytestmark = pytest.mark.canonical

_TASK_ID = "echo-hello"
_TRIAL_ID = "echo-hello:0"


@pytest.fixture
def fixture_dir() -> Path:
    return Path(__file__).parent.parent / "data" / "terminal_bench_tasks"


def _adapter(fixture_dir: Path, tmp_path: Path, agent_tool: str) -> TerminalBenchAdapter:
    return TerminalBenchAdapter(
        {
            "terminal_bench_dir": str(fixture_dir),
            "staging_root": str(tmp_path),
            "agent_tool": agent_tool,
        }
    )


def _wrapper(adapter: TerminalBenchAdapter):
    """The wrapper the runner builds for this adapter's one agent tool.

    ``_new_session`` is stubbed because the persistent shell opens a real
    ``docker exec`` bash against a container no test brings up; nothing below
    reads the session, and a test that asserted grading avoids it would be
    vacuous if the session were absent.
    """
    schema = adapter.to_task_description(_TASK_ID).agent_tools[0]
    factory = ToolFactory(db_client=MagicMock(), trial_id=_TRIAL_ID)
    wrapper = factory._create_wrapper(schema)
    with patch.object(PersistentShellToolWrapper, "_new_session", return_value=MagicMock()):
        wrapper.start(ToolLifecycleContext(trial_id=_TRIAL_ID))
    return wrapper


@pytest.mark.parametrize("agent_tool", AGENT_TOOLS)
def test_every_selectable_agent_tool_can_exec_for_grading(agent_tool, fixture_dir, tmp_path):
    wrapper = _wrapper(_adapter(fixture_dir, tmp_path, agent_tool))

    assert isinstance(wrapper, SupportsEnvExec)
    # Through the lookup grading actually uses, not just the isinstance above:
    # the trial registers one tool, so a miss here is the "no exec-capable env
    # tool was found" refusal on a trial that ran to completion.
    assert first_env_exec_tool([wrapper]) is wrapper


@pytest.mark.parametrize("agent_tool", AGENT_TOOLS)
def test_grading_execs_into_the_same_container_whichever_tool_the_agent_had(
    agent_tool, fixture_dir, tmp_path
):
    """Same argv shape, same container, either way.

    The point of the tool selector is that swapping the agent's tool changes
    what the *agent* can do, not where grading runs. Both wrappers must target
    the container the per-trial runtime brought up for this trial's agent
    service.
    """
    adapter = _adapter(fixture_dir, tmp_path, agent_tool)
    wrapper = _wrapper(adapter)
    expected_container = compose_container_name(
        _TRIAL_ID, adapter._environment(_TASK_ID).agent_service, "tbench_"
    )

    fake = MagicMock()
    fake.communicate.return_value = ("", "")
    fake.returncode = 0
    with patch("subprocess.Popen", return_value=fake) as popen_mock:
        wrapper.exec_in_env("echo hi", 30.0)

    argv = popen_mock.call_args.args[0]
    assert argv[:6] == ["docker", "exec", "-i", expected_container, "bash", "-c"]
    assert argv[6] == _STAGED_STDIO_SCRIPT
    assert argv[-1] == "echo hi"


def test_grading_does_not_run_inside_the_agents_live_shell(fixture_dir, tmp_path):
    """The persistent shell's exec is a fresh one, not the held session.

    The agent has had that session for the whole trial: its cwd, exported
    environment and shell functions are the agent's, and a command that timed
    out leaves it needing a restart. Grading through it would score the
    verifier against a mutated shell, and would fail for reasons the agent
    caused rather than for reasons the solution did.
    """
    wrapper = _wrapper(_adapter(fixture_dir, tmp_path, "bash_session"))
    assert isinstance(wrapper, PersistentShellToolWrapper)

    session = wrapper._session
    assert session is not None, "the open session is what this test asserts is left alone"

    fake = MagicMock()
    fake.stdout = ""
    fake.stderr = ""
    fake.returncode = 0
    with patch("subprocess.run", return_value=fake):
        wrapper.exec_in_env_with_exit_code("bash /tests/run-tests.sh", 30.0)

    session.run.assert_not_called()
