"""Unit tests for :class:`tolokaforge.session.SessionRegistry` — M1 sub-4a.

Idempotent lookup, concurrent creation, missing-vs-present semantics,
and diagnostics accessors.
"""

from __future__ import annotations

import threading

import pytest

from tolokaforge.session import InProcessTrialSession, SessionRegistry

pytestmark = pytest.mark.unit


class TestGetOrCreate:
    def test_first_call_creates_a_session(self):
        reg = SessionRegistry()
        session = reg.get_or_create("t:0")
        assert isinstance(session, InProcessTrialSession)
        assert session.trial_id == "t:0"

    def test_second_call_returns_same_instance(self):
        reg = SessionRegistry()
        s1 = reg.get_or_create("t:0")
        s2 = reg.get_or_create("t:0")
        assert s1 is s2

    def test_different_trial_ids_get_different_sessions(self):
        reg = SessionRegistry()
        s1 = reg.get_or_create("t:0")
        s2 = reg.get_or_create("t:1")
        assert s1 is not s2
        assert s1.trial_id == "t:0"
        assert s2.trial_id == "t:1"

    def test_concurrent_create_returns_same_instance_per_trial_id(self):
        reg = SessionRegistry()
        results: dict[int, InProcessTrialSession] = {}
        barrier = threading.Barrier(8)

        def worker(idx: int) -> None:
            barrier.wait()
            results[idx] = reg.get_or_create("shared")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        first = results[0]
        assert all(s is first for s in results.values())
        assert len(reg) == 1


class TestGet:
    def test_missing_returns_none(self):
        reg = SessionRegistry()
        assert reg.get("nope") is None

    def test_get_does_not_create(self):
        reg = SessionRegistry()
        assert reg.get("t:0") is None
        assert len(reg) == 0

    def test_get_returns_existing(self):
        reg = SessionRegistry()
        created = reg.get_or_create("t:0")
        assert reg.get("t:0") is created


class TestDiagnostics:
    def test_all_trial_ids_snapshot(self):
        reg = SessionRegistry()
        reg.get_or_create("a:0")
        reg.get_or_create("b:0")
        reg.get_or_create("c:0")
        assert sorted(reg.all_trial_ids()) == ["a:0", "b:0", "c:0"]

    def test_len_reflects_created_sessions(self):
        reg = SessionRegistry()
        assert len(reg) == 0
        reg.get_or_create("a:0")
        assert len(reg) == 1
        reg.get_or_create("a:0")  # idempotent
        assert len(reg) == 1
        reg.get_or_create("b:0")
        assert len(reg) == 2

    def test_contains_only_matches_present_string_trial_ids(self):
        reg = SessionRegistry()
        reg.get_or_create("t:0")
        assert "t:0" in reg
        assert "t:99" not in reg
        assert 42 not in reg  # non-string is False, not TypeError
        assert None not in reg
