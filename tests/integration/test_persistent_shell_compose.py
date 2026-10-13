"""Behaviour-parity lock for the compose ``bash_session`` engine (#566).

Runs the same behaviours Stage 1 locks for the local engine, but against a real
``docker exec`` into a running container — proving the compose provider is
behaviour-identical to the local one (modulo the documented orphan limitation
on timeout, where pre-timeout state is not preserved).

No mocks: a one-service container is brought up in ``setup`` and torn down after.
Skips cleanly when the Docker daemon is unavailable; a failed assertion is a real
failure, never a silent skip.
"""

from __future__ import annotations

import subprocess
import time

import pytest

from tests.utils.docker_helpers import is_docker_daemon_available
from tolokaforge.runner.models import ToolSchema
from tolokaforge.runner.tool_factory import PersistentShellToolWrapper, ToolLifecycleContext
from tolokaforge.tools.persistent_shell import DockerComposeBashSession

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]

_IMAGE = "bash:5"
_PREFIX = "tf_test_"
_TRIAL_ID = "s2parity"
_SERVICE = "app"
_CONTAINER = PersistentShellToolWrapper._resolve_container_name(_TRIAL_ID, _SERVICE, _PREFIX)


@pytest.fixture(scope="module")
def running_container():
    """Bring up a single bash container the compose engine can exec into."""
    if not is_docker_daemon_available():
        pytest.skip("Docker daemon not available")
    subprocess.run(["docker", "rm", "-f", _CONTAINER], capture_output=True, check=False)
    up = subprocess.run(
        ["docker", "run", "-d", "--name", _CONTAINER, _IMAGE, "sleep", "3600"],
        capture_output=True,
        text=True,
    )
    if up.returncode != 0:
        pytest.skip(f"could not start test container: {up.stderr.strip()}")
    try:
        yield _CONTAINER
    finally:
        subprocess.run(["docker", "rm", "-f", _CONTAINER], capture_output=True, check=False)


@pytest.fixture
def session(running_container):
    s = DockerComposeBashSession(running_container)
    s.open(None)
    yield s
    s.close()


def test_cwd_persists_across_calls(session):
    session.run("mkdir -p /tmp/sub", 10)
    session.run("cd /tmp/sub", 10)
    assert session.run("pwd", 10).output.strip() == "/tmp/sub"


def test_env_persists_across_calls(session):
    session.run("export FOO=bar", 10)
    assert session.run("echo $FOO", 10).output.strip() == "bar"


def test_shell_functions_persist_across_calls(session):
    session.run("greet() { echo hello-fn; }", 10)
    assert session.run("greet", 10).output.strip() == "hello-fn"


def test_restart_yields_fresh_shell(running_container):
    s = DockerComposeBashSession(running_container)
    s.open(None)
    try:
        s.run("export FOO=bar", 10)
        assert s.run("echo $FOO", 10).output.strip() == "bar"
        s.close()
        s.open(None)
        assert s.run("echo $FOO", 10).output.strip() == ""
    finally:
        s.close()


def test_timeout_is_kill_safe_and_session_usable(session):
    """Orphan limitation: the runaway is abandoned and the in-container session
    restarted, so the next command works (pre-timeout state is not preserved)."""
    result = session.run("sleep 300", 3)
    assert result.timed_out is True
    assert result.exit_code is None
    assert session.run("echo alive", 10).output.strip() == "alive"


def _pid_alive(session, pid_file: str) -> bool:
    return session.run(f"kill -0 $(cat {pid_file})", 10).exit_code == 0


def test_timeout_reports_the_reset_and_leaves_the_command_running(session):
    """S1: a command past its budget is still running after the reset, the
    result says the session was reset, and what it printed before the deadline
    is relayed."""
    session.run("export BEFORE=yes", 10)
    result = session.run("sleep 300 & echo $! > /tmp/s1.pid; echo partial; wait", 3)
    try:
        assert result.timed_out is True
        assert result.session_reset is True
        assert "partial" in result.output
        assert _pid_alive(session, "/tmp/s1.pid")
        assert session.run("echo $BEFORE", 10).output.strip() == ""
    finally:
        session.run("kill -9 $(cat /tmp/s1.pid); rm -f /tmp/s1.pid", 10)


def test_background_writer_survives_a_later_timeout_and_stays_out_of_later_output(session):
    """S2: a chatty service started with ``&`` neither blocks the session nor
    dies when a later command times out and the shell is replaced, and its
    output never lands in another command's result."""
    session.run("(while :; do echo spam; sleep 0.01; done) & echo $! > /tmp/s2.pid", 10)
    try:
        session.run("sleep 1", 10)
        assert session.run("sleep 300", 3).session_reset is True
        later = session.run("echo clean", 10)
        assert later.output.strip() == "clean"
        assert later.exit_code == 0
        assert _pid_alive(session, "/tmp/s2.pid")
    finally:
        session.run("kill -9 $(cat /tmp/s2.pid); rm -f /tmp/s2.pid", 10)


def test_stdin_reader_gets_eof_and_the_session_stays_usable(session):
    """S3: a command that reads stdin sees EOF at once instead of consuming the
    sentinel and waiting out the budget."""
    start = time.monotonic()
    result = session.run("read -r line; echo rc=$?", 10)
    assert time.monotonic() - start < 5
    assert result.timed_out is False
    assert result.output.strip() == "rc=1"
    assert session.run("cat", 10).exit_code == 0
    assert session.run("echo alive", 10).output.strip() == "alive"


def test_shell_exit_is_reported_and_closes_the_session(session):
    result = session.run("echo before; exit 3", 10)
    assert result.shell_exited is True
    assert result.timed_out is False
    assert result.exit_code == 3
    assert "before" in result.output
    assert session.is_open is False


def test_output_is_middle_truncated_with_grep_hint(session):
    result = session.run("for i in $(seq 1 20000); do printf X; done", 15)
    out = result.output
    assert len(out) < 20000
    assert "truncated" in out
    assert "grep" in out


async def test_wrapper_drives_compose_backend_end_to_end(running_container):
    """Full wrapper path: config selects the compose backend, resolves the
    running container's name, and runs a command against the real daemon."""
    schema = ToolSchema(
        name="bash_session",
        description="x",
        parameters={"type": "object", "properties": {}},
        tool_config={"service": _SERVICE, "compose_project_prefix": _PREFIX},
    )
    wrapper = PersistentShellToolWrapper(schema)
    wrapper.start(ToolLifecycleContext(trial_id=_TRIAL_ID))
    try:
        await wrapper.execute({"command": "export TOKEN=xyz"})
        assert (await wrapper.execute({"command": "echo $TOKEN"})).strip() == "xyz"
        assert "restarted" in await wrapper.execute({"restart": True})
        assert (await wrapper.execute({"command": "echo $TOKEN"})).strip() == ""
    finally:
        wrapper.stop()


async def test_wrapper_tells_the_agent_about_resets_and_exits(running_container):
    """The text the agent reads names the reset after a timeout (with the
    command possibly still running) and after a shell exit, and the call after
    an exit runs in a fresh shell."""
    schema = ToolSchema(
        name="bash_session",
        description="x",
        parameters={"type": "object", "properties": {}},
        tool_config={"service": _SERVICE, "compose_project_prefix": _PREFIX, "timeout_s": 3},
    )
    wrapper = PersistentShellToolWrapper(schema)
    wrapper.start(ToolLifecycleContext(trial_id=_TRIAL_ID))
    try:
        timed_out = await wrapper.execute({"command": "sleep 300 & echo $! > /tmp/w.pid; wait"})
        assert "timed out after 3s" in timed_out
        assert "session was reset" in timed_out
        assert "may still be running" in timed_out
        await wrapper.execute({"command": "kill -9 $(cat /tmp/w.pid); rm -f /tmp/w.pid"})

        exited = await wrapper.execute({"command": "exit 3"})
        assert "shell exited with exit code 3" in exited
        assert "next call starts clean" in exited
        assert "timed out" not in exited
        assert (await wrapper.execute({"command": "echo back"})).strip() == "back"
    finally:
        wrapper.stop()
