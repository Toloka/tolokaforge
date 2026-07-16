"""Unit tests for the needs-human 'why it did not converge' report composer."""

from __future__ import annotations

import automation.resolve_report as rr
import pytest

pytestmark = pytest.mark.unit

STALLED_ALL = """\
- Iter 1: no overlay produced (agent stalled or hit its turn limit / throttled)
- Iter 2: no overlay produced (agent stalled or hit its turn limit / throttled)
- Iter 3: no overlay produced (agent stalled or hit its turn limit / throttled)
"""

MIXED = """\
- Iter 1: no overlay produced (agent stalled or hit its turn limit / throttled)
- Iter 2: fix produced, reprobe verdict = RED:discriminated_union
- Iter 3: fix produced, reprobe verdict = RED:discriminated_union
"""

ALL_RED = """\
- Iter 1: fix produced, reprobe verdict = RED:dict_map
- Iter 2: fix produced, reprobe verdict = RED:dict_map
"""


class TestCounts:
    def test_all_stalled(self):
        assert rr.counts(STALLED_ALL) == (3, 3, 0)

    def test_mixed(self):
        assert rr.counts(MIXED) == (3, 1, 2)

    def test_all_red(self):
        assert rr.counts(ALL_RED) == (2, 0, 2)

    def test_empty(self):
        assert rr.counts("") == (0, 0, 0)


class TestDiagnose:
    def test_all_stalled_points_at_anthropic_throttle(self):
        msg = rr.diagnose(3, 3, 0, 8)
        assert "NO overlay" in msg and "THROTTLE" in msg.upper()

    def test_mixed_mentions_both(self):
        msg = rr.diagnose(3, 1, 2, 8)
        assert "RED" in msg and "no overlay" in msg.lower()

    def test_all_red_is_hard_quirk(self):
        msg = rr.diagnose(2, 0, 2, 8)
        assert "never went green" in msg or "did not resolve" in msg

    def test_no_iterations(self):
        assert "did not run" in rr.diagnose(0, 0, 0, 8)


class TestBuildReport:
    def test_includes_header_summary_diagnosis(self):
        out = rr.build_report(STALLED_ALL, None, 8)
        assert "did not converge (ran 3/8 iterations)" in out
        assert "Per-iteration outcome:" in out
        assert "Iter 1: no overlay" in out
        assert "Diagnosis." in out

    def test_includes_agent_decision_when_present(self):
        decision = {"fix_targets": ["discriminated_union", "dict_map"], "notes": "stringified item"}
        out = rr.build_report(MIXED, decision, 8)
        assert "`discriminated_union`" in out and "`dict_map`" in out
        assert "stringified item" in out

    def test_no_decision_is_fine(self):
        out = rr.build_report(STALLED_ALL, None, 8)
        assert "Agent's last decision" not in out
