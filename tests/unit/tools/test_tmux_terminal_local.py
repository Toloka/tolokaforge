"""Behaviour lock for the persistent tmux terminal against a real tmux.

Real panes, real bash, no mocks — the point of the module is what a one-shot
``bash(command)`` tool cannot do, and none of it survives being simulated:
a command left pending without a newline, a REPL entered and exited, ``vi``
actually writing a file, ``C-c`` interrupting a foreground command with the
session intact, and a saturated scrollback saying so.

The tier is gated on a real ``tmux`` and skips loudly without one. It is not
an optional nicety: the whole module is a wrapper over tmux semantics, so a
run that skips this file has verified none of them.
"""

from __future__ import annotations

import shutil
import time

import pytest

from tolokaforge.tools.tmux_terminal import (
    EVICTED_MARKER,
    LocalTmuxBackend,
    TmuxTerminalSession,
)
from tolokaforge.tools.tmux_terminal.pure import (
    CURRENT_SCREEN_HEADER,
    NEW_OUTPUT_HEADER,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(
        shutil.which("tmux") is None,
        reason=(
            "tmux is not on PATH — the persistent-terminal tier exercises real tmux "
            "semantics (send-keys, capture-pane, scrollback eviction) and cannot be "
            "simulated, so this run has verified none of them. Install tmux to cover "
            "it: `brew install tmux` / `apt-get install -y tmux`."
        ),
    ),
]


@pytest.fixture
def session(tmp_path):
    session = TmuxTerminalSession(LocalTmuxBackend(), pane_height=20)
    session.open(cwd=str(tmp_path))
    session.observe()  # a trial's first observation is the screen; establish the watermark
    try:
        yield session
    finally:
        session.close()


def _run(session, command, *, wait_s=10.0):
    """Type *command*, wait for the shell to finish it, and observe once."""
    session.type_keys([command, "Enter"])
    time.sleep(0.3)  # let the shell pick the line up before asking whether it is idle
    session.wait_until_idle(wait_s)
    return session.observe()


def test_the_first_observation_of_a_fresh_session_is_the_screen(tmp_path):
    session = TmuxTerminalSession(LocalTmuxBackend(), pane_height=20)
    session.open(cwd=str(tmp_path))
    try:
        opening = session.observe()
    finally:
        session.close()

    assert CURRENT_SCREEN_HEADER in opening, "there is no 'since last time' yet"
    assert "idle=yes" in opening
    assert "$" in opening, "the cleared pane shows a prompt"


def test_echo_round_trips_through_the_pane(session):
    observation = _run(session, "echo hello-terminal")

    assert NEW_OUTPUT_HEADER in observation
    assert "hello-terminal" in observation
    assert "last_exit=0" in observation


def test_a_command_without_a_newline_is_pending_not_executed(session):
    typed = session.send_keys(["echo not-yet-executed"], min_wait_s=0.4)

    assert CURRENT_SCREEN_HEADER in typed
    assert typed.count("not-yet-executed") == 1, "echoed on the command line, not run"
    assert "idle=yes" in typed

    executed = session.send_keys(["Enter"], min_wait_s=0.6)
    assert (
        executed.count("not-yet-executed") >= 2
    ), "the Enter alone submits the pending line: the command echo plus its output"


def test_the_shell_reports_a_running_foreground_then_returns_to_idle(session):
    running = session.send_keys(["sleep 3", "Enter"], min_wait_s=0.8)

    assert "foreground=sleep" in running
    assert "idle=no" in running

    assert session.wait_until_idle(8.0) is True
    idle = session.observe()
    assert "idle=yes" in idle
    assert "foreground=bash" in idle


def test_polling_a_running_command_returns_a_distinguishable_observation(session):
    session.send_keys(["sleep 3", "Enter"], min_wait_s=0.5)

    first = session.observe()
    time.sleep(1.0)
    second = session.observe()

    assert first != second, (
        "two polls of an unchanged screen must differ, or the stuck detector "
        "hashes them into one signature and forces the score to zero"
    )
    assert "idle=no" in first and "idle=no" in second


def test_a_failing_command_surfaces_its_exit_code(session):
    assert "last_exit=7" in _run(session, "(exit 7)")


@pytest.mark.skipif(shutil.which("python3") is None, reason="python3 is not on PATH")
def test_a_python_repl_can_be_entered_and_left(session):
    entered = session.send_keys(["python3", "Enter"], min_wait_s=2.0)
    assert ">>>" in entered

    evaluated = session.send_keys(["21 * 2", "Enter"], min_wait_s=1.0)
    assert "42" in evaluated
    assert "idle=no" in evaluated, "the REPL is the foreground command, not the shell"

    session.send_keys(["C-d"], min_wait_s=1.0)
    session.wait_until_idle(5.0)
    assert "idle=yes" in session.observe(), "the shell survives the REPL"


@pytest.mark.skipif(shutil.which("vi") is None, reason="vi is not on PATH")
def test_vi_writes_a_file_the_agent_never_could_with_one_shot_bash(session, tmp_path):
    opened = session.send_keys(["vi note.txt", "Enter"], min_wait_s=2.0)
    assert "alternate_screen=yes" in opened, "a full-screen editor holds the alternate screen"

    session.send_keys(["i"], min_wait_s=0.4)
    session.send_keys(["written-from-inside-vi"], min_wait_s=0.4)
    session.send_keys(["Escape"], min_wait_s=0.4)
    session.send_keys([":wq", "Enter"], min_wait_s=1.5)
    session.wait_until_idle(5.0)

    note = tmp_path / "note.txt"
    assert note.exists(), "the editor session must have actually written the file"
    assert note.read_text().strip() == "written-from-inside-vi"
    assert "alternate_screen" not in session.observe()


def test_ctrl_c_interrupts_the_foreground_command_and_the_session_survives(session):
    session.send_keys(["sleep 60", "Enter"], min_wait_s=0.8)
    assert "foreground=sleep" in session.observe()

    interrupted = session.send_keys(["C-c"], min_wait_s=1.0)

    assert "idle=yes" in interrupted
    assert "last_exit=130" in interrupted, "SIGINT is 128 + 2"
    assert "still-alive" in _run(session, "echo still-alive")


def test_state_persists_across_calls_because_it_is_one_shell(session):
    _run(session, "export CARRIED=over")
    _run(session, "mkdir -p sub && cd sub")

    assert "over" in _run(session, "echo $CARRIED")
    assert "/sub" in _run(session, "pwd")


def test_a_saturated_scrollback_says_so_instead_of_pretending(tmp_path):
    session = TmuxTerminalSession(
        LocalTmuxBackend(), pane_height=20, history_limit=10, max_output_chars=2_000
    )
    session.open(cwd=str(tmp_path))
    try:
        session.send_keys(["echo start", "Enter"], min_wait_s=0.5)
        observation = _run(session, "seq 1 500")
    finally:
        session.close()

    assert EVICTED_MARKER in observation
    assert observation.index(EVICTED_MARKER) == 0
    assert "500" in observation, "what is still retained is still reported"


def test_an_oversized_keystroke_is_pasted_rather_than_lost(session, tmp_path):
    payload = "L" * 20_000  # over the ~16 KB tmux send-keys command limit

    session.send_keys(["cat > big.txt <<'TF_EOF'", "Enter"], min_wait_s=0.6)
    session.send_keys([payload], min_wait_s=3.0)
    session.send_keys(["Enter"], min_wait_s=1.0)
    session.send_keys(["TF_EOF", "Enter"], min_wait_s=1.5)
    session.wait_until_idle(10.0)

    written = tmp_path / "big.txt"
    assert written.exists()
    assert written.read_text() == payload + "\n"


def test_a_closed_session_leaves_no_tmux_server_behind(tmp_path):
    session = TmuxTerminalSession(LocalTmuxBackend(), pane_height=20)
    session.open(cwd=str(tmp_path))
    assert session.is_open is True

    session.close()

    assert session.is_open is False
    probe = LocalTmuxBackend().exec(["tmux", "-L", f"tf-{session.session_name}", "list-sessions"])
    assert probe.returncode != 0, "the private tmux server must be gone"
