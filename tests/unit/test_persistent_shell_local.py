"""Behaviour lock for the local ``bash_session`` persistent shell (#566).

Real bash subprocesses, no mocks. Covers state persistence (cwd, env,
functions), restart, kill-safe timeout with state survival (mechanism M1:
PTY + job control), output truncation, and the wrapper's config-driven
timeout. Also locks that the schema provider advertises ``command`` +
``restart`` even when a compose ``service`` config is present.
"""

from __future__ import annotations

import os
import subprocess
import time

import psutil
import pytest

from tolokaforge.adapters.native import _builtin_tool_schemas
from tolokaforge.runner.models import ToolSchema
from tolokaforge.runner.tool_factory import (
    PersistentShellToolWrapper,
    ToolLifecycleContext,
)
from tolokaforge.tools.persistent_shell import DockerComposeBashSession, LocalBashSession

pytestmark = pytest.mark.unit


@pytest.fixture
def session(tmp_path):
    s = LocalBashSession()
    s.open(str(tmp_path))
    yield s
    s.close()


def test_cwd_persists_across_calls(session, tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    session.run(f"cd {sub}", 5)
    result = session.run("pwd", 5)
    assert result.output.strip() == os.path.realpath(str(sub))


def test_env_persists_across_calls(session):
    session.run("export FOO=bar", 5)
    result = session.run("echo $FOO", 5)
    assert result.output.strip() == "bar"


def test_shell_functions_persist_across_calls(session):
    session.run("greet() { echo hello-fn; }", 5)
    result = session.run("greet", 5)
    assert result.output.strip() == "hello-fn"


def test_restart_yields_fresh_shell(tmp_path):
    session = LocalBashSession()
    session.open(str(tmp_path))
    try:
        session.run("export FOO=bar", 5)
        assert session.run("echo $FOO", 5).output.strip() == "bar"
        session.close()
        session.open(str(tmp_path))
        assert session.run("echo $FOO", 5).output.strip() == ""
    finally:
        session.close()


def test_timeout_is_kill_safe_and_state_survives(session):
    """M1: the runaway is terminated, no process leaks, the session stays
    usable, and env set before the timeout survives (same session)."""
    session.run("export SURVIVES=yes", 5)
    proc = psutil.Process(session.pid)

    start = time.monotonic()
    result = session.run("sleep 300", 3)
    elapsed = time.monotonic() - start

    assert result.timed_out is True
    assert result.exit_code is None
    assert elapsed < 15  # enforced near the 3s budget, not left to run 300s

    # No leaked runaway: the shell has no surviving children.
    assert proc.children(recursive=True) == []

    # Session still usable and pre-timeout state survived (M1 guarantee).
    assert session.run("echo alive", 5).output.strip() == "alive"
    assert session.run("echo $SURVIVES", 5).output.strip() == "yes"


def test_output_is_middle_truncated_with_grep_hint(session):
    result = session.run("for i in $(seq 1 20000); do printf X; done", 15)
    out = result.output
    assert len(out) < 20000
    assert out.startswith("X" * 100)
    assert out.endswith("X" * 100)
    assert "truncated" in out
    assert "grep" in out


def test_large_command_survives_short_writes(session, monkeypatch):
    """``os.write`` on the PTY master may write fewer bytes than requested, so
    ``_write`` must loop until the buffer drains. Cap every write at 4096 bytes
    to force that short-write condition deterministically, then round-trip a
    large heredoc: a single un-looped write would drop the command tail (the
    heredoc terminator never arrives → timeout), while the loop delivers it in
    full. ``wc -c`` keeps the output under the 16 KB truncation budget so the
    write path is what's under test."""
    import tolokaforge.tools.persistent_shell as ps

    real_write = os.write
    monkeypatch.setattr(ps.os, "write", lambda fd, buf: real_write(fd, buf[:4096]))

    payload = "A" * 100_000
    result = session.run(f"cat <<'EOF' | wc -c\n{payload}\nEOF", 15)
    assert result.timed_out is False
    assert result.exit_code == 0
    assert result.output.strip() == str(len(payload) + 1)


async def test_wrapper_timeout_resolved_from_tool_config(tmp_path):
    """A 5s ``tool_config`` timeout is enforced — proves the wrapper reads
    tool_config, not self.timeout_s (pinned to 30s by the adapter)."""
    schema = ToolSchema(
        name="bash_session",
        description="x",
        parameters={"type": "object", "properties": {}},
        tool_config={"timeout_s": 5},
    )
    wrapper = PersistentShellToolWrapper(schema)
    wrapper.start(ToolLifecycleContext(trial_id="t", work_dir=str(tmp_path)))
    try:
        start = time.monotonic()
        output = await wrapper.execute({"command": "sleep 300"})
        elapsed = time.monotonic() - start
        assert 4 < elapsed < 20
        assert "timed out" in output
    finally:
        wrapper.stop()


async def test_wrapper_restart_resets_state(tmp_path):
    schema = ToolSchema(
        name="bash_session",
        description="x",
        parameters={"type": "object", "properties": {}},
    )
    wrapper = PersistentShellToolWrapper(schema)
    wrapper.start(ToolLifecycleContext(trial_id="t", work_dir=str(tmp_path)))
    try:
        await wrapper.execute({"command": "export FOO=bar"})
        assert (await wrapper.execute({"command": "echo $FOO"})).strip() == "bar"
        assert "restarted" in await wrapper.execute({"restart": True})
        assert (await wrapper.execute({"command": "echo $FOO"})).strip() == ""
    finally:
        wrapper.stop()


async def test_wrapper_requires_command_or_restart(tmp_path):
    from tolokaforge.runner.tool_factory import ToolExecutionError

    schema = ToolSchema(
        name="bash_session",
        description="x",
        parameters={"type": "object", "properties": {}},
    )
    wrapper = PersistentShellToolWrapper(schema)
    wrapper.start(ToolLifecycleContext(trial_id="t", work_dir=str(tmp_path)))
    try:
        with pytest.raises(ToolExecutionError):
            await wrapper.execute({})
    finally:
        wrapper.stop()


class _DeadExecSession(DockerComposeBashSession):
    """Compose session whose backing process exits immediately.

    Stands in for ``docker exec`` into a container that is not running (writes
    an error, exits) without needing a container runtime, so every PTY read
    hits EOF promptly. Exercises the compose engine's real ``open`` /
    ``_terminate_runaway`` code — only the process spawn is swapped.
    """

    def _popen(self, cwd, slave_fd):
        return subprocess.Popen(
            ["sh", "-c", "echo 'no such container' >&2; exit 1"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=os.setsid,
            close_fds=True,
        )


def test_compose_open_on_dead_exec_raises_not_recurses():
    """Regression: a dead ``docker exec`` (EOF, no prompt) must surface a clean
    failure, not blow the stack. An empty read is the shell process exiting, not
    a runaway command; routing it through ``_terminate_runaway`` made the compose
    engine reopen + re-run per EOF, forking one ``docker exec`` per level until
    ``RecursionError``. A low recursion limit keeps a regression cheap to detect."""
    import sys

    session = _DeadExecSession("not-running-container")
    original_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(80)
    try:
        with pytest.raises(RuntimeError, match="failed to become ready"):
            session.open("/work")
    finally:
        sys.setrecursionlimit(original_limit)
        session.close()


def test_schema_advertises_command_and_restart_locally():
    schemas = _builtin_tool_schemas(["bash_session"], {})
    assert "bash_session" in schemas
    props = schemas["bash_session"]["parameters"]["properties"]
    assert set(props) == {"command", "restart"}


def test_schema_advertised_when_compose_service_configured():
    """The schema provider tolerates the compose ``service`` kwarg, so the
    tool is not dropped from the LLM's view for a compose task."""
    schemas = _builtin_tool_schemas(["bash_session"], {"bash_session": {"service": "app"}})
    assert "bash_session" in schemas
    props = schemas["bash_session"]["parameters"]["properties"]
    assert set(props) == {"command", "restart"}
