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
