"""Behaviour lock for the batching persistent shell (``bash_session_batch``).

Real bash subprocesses, no mocks, mirroring ``test_persistent_shell_local``.
Covers what distinguishes this tool from both parents: state carries *within*
one call as well as across calls (``bash_batch`` has neither), several commands
run per call (``bash_session`` runs one), and a command that times out, fails or
ends the shell does not stop the ones after it.
"""

from __future__ import annotations

import os
import time

import pytest

from tolokaforge.runner.models import ToolSchema
from tolokaforge.runner.tool_factory import (
    PersistentShellToolWrapper,
    ToolExecutionError,
    ToolLifecycleContext,
)
from tolokaforge.tools.persistent_shell import MAX_SESSION_BATCH_COMMANDS

pytestmark = pytest.mark.unit


def _schema(timeout_s: float = 120.0, max_items: int = MAX_SESSION_BATCH_COMMANDS) -> ToolSchema:
    return ToolSchema(
        name="bash_session_batch",
        description="x",
        parameters={
            "type": "object",
            "properties": {
                "note": {"type": "string"},
                "commands": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": max_items,
                },
            },
        },
        tool_config={"timeout_s": timeout_s},
    )


@pytest.fixture
def wrapper(tmp_path):
    w = PersistentShellToolWrapper(_schema())
    w.start(ToolLifecycleContext(trial_id="t", work_dir=str(tmp_path)))
    yield w
    w.stop()


async def test_cwd_persists_within_one_call(wrapper, tmp_path):
    """The property neither parent tool has: command 2 sees command 1's ``cd``."""
    sub = tmp_path / "sub"
    sub.mkdir()
    out = await wrapper.execute({"note": "n", "commands": [f"cd {sub}", "pwd"]})
    assert out.splitlines()[-1] == os.path.realpath(str(sub))


async def test_state_persists_from_one_call_into_the_next(wrapper, tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    await wrapper.execute({"note": "n", "commands": [f"cd {sub}", "export FOO=bar"]})
    out = await wrapper.execute({"note": "n", "commands": ["pwd", "echo $FOO"]})
    assert os.path.realpath(str(sub)) in out
    assert "bar" in out


async def test_exported_env_function_and_source_persist_within_one_call(wrapper, tmp_path):
    (tmp_path / "env.sh").write_text("export SOURCED=yes\n")
    out = await wrapper.execute(
        {
            "note": "n",
            "commands": [
                "export FOO=bar",
                "greet() { echo hello-fn; }",
                f"source {tmp_path / 'env.sh'}",
                "echo $FOO $SOURCED; greet",
            ],
        }
    )
    assert "bar yes" in out
    assert "hello-fn" in out


async def test_each_command_is_labelled_in_order(wrapper):
    """Byte-identical to the shape ``bash_batch`` returns: ``$ cmd\\n<output>``
    sections joined by a blank line, each command's own trailing newline kept."""
    out = await wrapper.execute({"note": "n", "commands": ["echo one", "echo two"]})
    assert out == "\n\n".join(["$ echo one\none\n", "$ echo two\ntwo\n"])


async def test_a_failing_command_does_not_stop_the_rest(wrapper):
    out = await wrapper.execute(
        {"note": "n", "commands": ["echo first", "exit_code_please || false", "echo third"]}
    )
    assert "first" in out
    assert "[exit code: 1]" in out
    assert "third" in out


async def test_a_timed_out_command_still_lets_the_next_one_run(tmp_path):
    w = PersistentShellToolWrapper(_schema(timeout_s=3.0))
    w.start(ToolLifecycleContext(trial_id="t", work_dir=str(tmp_path)))
    try:
        out = await w.execute({"note": "n", "commands": ["sleep 300", "echo after"]})
        assert "timed out after 3s" in out
        assert "after" in out
    finally:
        w.stop()


async def test_a_shell_exit_mid_batch_is_reported_and_the_next_command_reopens(wrapper):
    out = await wrapper.execute(
        {"note": "n", "commands": ["export FOO=bar; exit 3", "echo back$FOO"]}
    )
    assert "shell exited with exit code 3" in out
    assert "next call starts clean" in out
    # The reopened shell is clean, so $FOO is gone but the command still ran.
    assert "back\n" in out


async def test_restart_precedes_the_batch_and_clears_state(wrapper):
    await wrapper.execute({"note": "n", "commands": ["export FOO=bar"]})
    out = await wrapper.execute({"note": "n", "restart": True, "commands": ["echo [$FOO]"]})
    assert out.startswith("Shell session restarted")
    assert "[]" in out


async def test_a_spent_budget_names_every_command_it_did_not_run(tmp_path):
    w = PersistentShellToolWrapper(_schema(timeout_s=2.0, max_items=2))
    w.start(ToolLifecycleContext(trial_id="t", work_dir=str(tmp_path)))
    try:
        start = time.monotonic()
        out = await w.execute({"note": "n", "commands": ["sleep 300", "echo b", "echo c"]})
        assert time.monotonic() - start < 30
        assert "2 command(s) remain" in out
        assert "$ echo b" in out and "$ echo c" in out
        assert "[not run]" in out
    finally:
        w.stop()


async def test_malformed_commands_runs_nothing(wrapper):
    with pytest.raises(ToolExecutionError, match="must be an array of strings"):
        await wrapper.execute({"note": "n", "commands": "echo one"})


async def test_single_command_calls_are_untouched(tmp_path):
    """``bash_session``'s own contract must not move."""
    schema = ToolSchema(name="bash_session", description="x", parameters={"type": "object"})
    w = PersistentShellToolWrapper(schema)
    w.start(ToolLifecycleContext(trial_id="t", work_dir=str(tmp_path)))
    try:
        assert w.own_budget_s == 120.0
        assert (await w.execute({"command": "echo hi"})).strip() == "hi"
        assert "restarted" in await w.execute({"restart": True})
    finally:
        w.stop()


def test_declared_batch_size_sets_the_call_band(tmp_path):
    assert PersistentShellToolWrapper(_schema(timeout_s=120.0)).own_budget_s == 1200.0
    assert PersistentShellToolWrapper(_schema(timeout_s=120.0)).effective_timeout_s == 1205.0
    bare = ToolSchema(name="bash_session", description="x", parameters={"type": "object"})
    assert PersistentShellToolWrapper(bare).declared_batch_size == 1


def test_a_slow_first_open_is_retried_rather_than_losing_the_trial(tmp_path, monkeypatch):
    """One failed readiness round-trip must not cost the whole trial.

    A trial whose session never opens is a `provision_error`: zero turns, zero
    cost, nothing measured. Opening is one sentinel round-trip under a fixed
    budget, so a loaded host can lose it to a slow spawn alone.
    """
    from tolokaforge.tools.persistent_shell import LocalBashSession

    session = LocalBashSession()
    calls: list[int] = []
    real_open_shell = session._open_shell

    def flaky(cwd):
        calls.append(1)
        if len(calls) == 1:
            return False
        return real_open_shell(cwd)

    monkeypatch.setattr(session, "_open_shell", flaky)
    try:
        session.open(str(tmp_path))
        assert len(calls) == 2
        assert session.is_open
        assert session.run("echo alive", 5).output.strip() == "alive"
    finally:
        session.close()


def test_an_open_that_never_becomes_ready_still_fails_loud(tmp_path, monkeypatch):
    from tolokaforge.tools.persistent_shell import LocalBashSession

    session = LocalBashSession()
    monkeypatch.setattr(session, "_open_shell", lambda cwd: False)
    try:
        with pytest.raises(RuntimeError, match="failed to become ready"):
            session.open(str(tmp_path))
    finally:
        session.close()
