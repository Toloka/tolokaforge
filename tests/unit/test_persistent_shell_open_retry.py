"""Opening a session has to survive a container that is still settling.

A trial whose session never opens is a ``provision_error``: zero turns, zero
cost, nothing measured. Two arms of a six-task benchmark lost a trial each that
way, both on the heaviest container in the set.

Two different failures reach the same dead end. The shell can answer too slowly
— a spawn queued behind an image build eats the round-trip budget — or it can
die instantly, when ``docker exec`` lands on a container that is up but not yet
accepting execs. The second fails in milliseconds, so a retry with no wait just
burns attempts against the same unready container.
"""

from __future__ import annotations

import pytest

from tolokaforge.tools import persistent_shell as mod

pytestmark = pytest.mark.unit


def test_a_container_not_yet_accepting_a_shell_is_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "_OPEN_RETRY_BACKOFF_S", 0.01)
    session = mod.LocalBashSession()
    calls: list[int] = []
    real_open_shell = session._open_shell

    def flaky(cwd):
        calls.append(1)
        if len(calls) < 3:
            session._open_failure = "the shell exited with exit code 126 before answering"
            return False
        return real_open_shell(cwd)

    monkeypatch.setattr(session, "_open_shell", flaky)
    try:
        session.open(str(tmp_path))
        assert len(calls) == 3
        assert session.is_open
        assert session.run("echo alive", 5).output.strip() == "alive"
    finally:
        session.close()


def test_retries_pause_between_attempts(tmp_path, monkeypatch):
    """Without a wait, retrying just burns attempts against the same unready
    container — the pause is the part that works."""
    monkeypatch.setattr(mod, "_OPEN_RETRY_BACKOFF_S", 0.05)
    monkeypatch.setattr(mod, "_OPEN_DEADLINE_S", 1.0)
    slept: list[float] = []
    monkeypatch.setattr(mod.time, "sleep", lambda s: slept.append(s))

    session = mod.LocalBashSession()
    monkeypatch.setattr(session, "_open_shell", lambda cwd: False)
    with pytest.raises(RuntimeError):
        session.open(str(tmp_path))

    assert slept, "retried without ever waiting for the container"
    assert slept == sorted(slept), "backoff must not shrink between attempts"


def test_an_open_that_never_becomes_ready_fails_loud_and_says_why(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "_OPEN_DEADLINE_S", 0.2)
    monkeypatch.setattr(mod, "_OPEN_RETRY_BACKOFF_S", 0.01)
    session = mod.LocalBashSession()

    def dead(cwd):
        session._open_failure = "the shell exited with exit code 126 before answering"
        return False

    monkeypatch.setattr(session, "_open_shell", dead)
    try:
        with pytest.raises(RuntimeError, match="exit code 126 before answering"):
            session.open(str(tmp_path))
    finally:
        session.close()


def test_a_shell_that_opens_first_time_never_waits(tmp_path, monkeypatch):
    """The retry must cost an ordinary trial nothing."""
    slept: list[float] = []
    monkeypatch.setattr(mod.time, "sleep", lambda s: slept.append(s))

    session = mod.LocalBashSession()
    try:
        session.open(str(tmp_path))
        assert session.is_open
        assert not slept
    finally:
        session.close()
