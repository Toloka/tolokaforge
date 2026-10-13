"""Behaviour lock for the local ``bash_session`` persistent shell (#566).

Real bash subprocesses, no mocks. Covers state persistence (cwd, env,
functions), restart, kill-safe timeout with state survival (mechanism M1:
PTY + job control), output truncation, and the wrapper's config-driven
timeout. Also locks that the schema provider advertises ``command`` +
``restart`` even when a compose ``service`` config is present.
"""

from __future__ import annotations

import os
import resource
import subprocess
import time

import psutil
import pytest

from tolokaforge.adapters._task_loader import _builtin_tool_schemas
from tolokaforge.runner.models import ToolSchema
from tolokaforge.runner.tool_factory import (
    PersistentShellToolWrapper,
    ToolLifecycleContext,
)
from tolokaforge.tools.persistent_shell import DockerComposeBashSession, LocalBashSession

pytestmark = pytest.mark.unit

_FD_SETSIZE = 1024
"""The descriptor ceiling ``select`` is compiled with; ``poll`` has none."""


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


def test_timeout_relays_what_the_command_printed(session):
    result = session.run("echo partial; sleep 300", 3)
    assert result.timed_out is True
    assert result.session_reset is False
    assert "partial" in result.output
    assert session.run("echo alive", 5).output.strip() == "alive"


def test_background_writer_stays_out_of_later_output_and_survives_a_timeout(session):
    """A service started with ``&`` writes to its own files, not the session
    pipe: later results carry only their own output, and the writer outlives a
    timed-out command."""
    session.run("(while :; do echo spam; sleep 0.01; done) & echo $! > writer.pid", 5)
    try:
        session.run("sleep 1", 5)
        assert session.run("sleep 300", 3).timed_out is True
        later = session.run("echo clean", 5)
        assert later.output.strip() == "clean"
        assert later.exit_code == 0
        assert session.run("kill -0 $(cat writer.pid)", 5).exit_code == 0
    finally:
        session.run("kill -9 $(cat writer.pid); rm -f writer.pid", 5)


def test_a_sigint_ignoring_runaway_does_not_bleed_into_the_next_command(session, monkeypatch):
    """A command that ignores the kill's SIGINT keeps relaying its stage after
    SIGKILL. With the relay still in flight when the grace expires, the timeout
    path must drain it so none of it leaks into the next command."""
    import tolokaforge.tools.persistent_shell as ps

    # Expire the relay grace at once so the drain path runs while the shell is
    # still relaying, which is the window the stale tail would otherwise escape.
    monkeypatch.setattr(ps, "_RELAY_GRACE_S", 0.01)

    result = session.run(
        "trap '' INT; yes BLEED | head -c 2000000; sleep 300",
        2,
    )
    assert result.timed_out is True

    later = session.run("echo clean", 5)
    assert later.output.strip() == "clean"
    assert later.exit_code == 0
    assert "BLEED" not in later.output


def test_stdin_reader_gets_eof(session):
    start = time.monotonic()
    result = session.run("read -r line; echo rc=$?", 10)
    assert time.monotonic() - start < 5
    assert result.timed_out is False
    assert result.output.strip() == "rc=1"


def test_shell_exit_is_reported_with_its_code_and_output(session):
    result = session.run("echo before; exit 3", 5)
    assert result.shell_exited is True
    assert result.timed_out is False
    assert result.exit_code == 3
    assert "before" in result.output
    assert session.is_open is False


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


async def test_wrapper_reports_a_shell_exit_and_reopens_on_the_next_call(tmp_path):
    schema = ToolSchema(
        name="bash_session",
        description="x",
        parameters={"type": "object", "properties": {}},
    )
    wrapper = PersistentShellToolWrapper(schema)
    wrapper.start(ToolLifecycleContext(trial_id="t", work_dir=str(tmp_path)))
    try:
        exited = await wrapper.execute({"command": "export FOO=bar; exit 3"})
        assert "shell exited with exit code 3" in exited
        assert "next call starts clean" in exited
        assert "timed out" not in exited
        assert (await wrapper.execute({"command": "echo back$FOO"})).strip() == "back"
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


def test_a_session_works_on_a_descriptor_past_select_s_ceiling(tmp_path):
    """A busy process hands the pty a high descriptor, and the session still runs.

    ``select`` cannot name a descriptor at or above ``FD_SETSIZE`` (1024) and
    raises ``ValueError`` when handed one, however few are actually open — so a
    runner holding a pty and a socket per concurrent trial breaks on sessions it
    opens late, while nowhere near ``RLIMIT_NOFILE``.

    The descriptors are held open, not merely counted: the point is that the
    session's own pty lands past the ceiling.
    """
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    needed = _FD_SETSIZE + 64
    if hard < needed:
        pytest.skip(f"RLIMIT_NOFILE hard cap {hard} cannot reach {needed}")
    resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, needed), hard))

    held: list[tuple[int, int]] = []
    session = LocalBashSession()
    try:
        # Fill until an allocation actually comes back at or above the
        # ceiling, rather than until some *count* of open descriptors is
        # reached. The two are not the same: the kernel hands out the lowest
        # free number, so a process carrying gaps below the ceiling — which a
        # long test session routinely does — can hold well over
        # ``_FD_SETSIZE`` descriptors while low numbers remain free. Counting
        # them let the pty land at fd 474 on CI, and the test then reported
        # that it had not exercised its own condition and failed for it.
        # Allocating until a descriptor lands past the ceiling leaves every
        # number below it occupied, so the session's pty cannot land low.
        # It also drops the ``/proc`` dependency, which does not exist off
        # Linux.
        while True:
            pipe = os.pipe()
            held.append(pipe)
            if max(pipe) >= _FD_SETSIZE:
                break
            if len(held) > needed:
                pytest.skip(
                    f"could not place a descriptor past {_FD_SETSIZE} after "
                    f"{len(held)} pipes — this run cannot exercise the condition"
                )

        session.open(str(tmp_path))
        assert session._master_fd >= _FD_SETSIZE, (
            f"the pty landed at fd {session._master_fd}, below the {_FD_SETSIZE} ceiling — "
            "this run did not exercise the condition it is here for"
        )

        result = session.run("echo past_the_ceiling", timeout_s=10)
        assert "past_the_ceiling" in result.output
        assert result.timed_out is False
    finally:
        session.close()
        for read_fd, write_fd in held:
            os.close(read_fd)
            os.close(write_fd)
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
