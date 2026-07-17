"""Unit tests for the candidate parallelism calibration (429 staircase)."""

from __future__ import annotations

import automation.calibrate as cal
import pytest

pytestmark = pytest.mark.unit


class TestClassify:
    def test_429_is_rate_limited(self):
        assert cal.classify(429) == "rate_limited"

    def test_2xx_is_ok(self):
        assert cal.classify(200) == "ok"
        assert cal.classify(201) == "ok"

    def test_5xx_and_transport_are_error(self):
        assert cal.classify(500) == "error"
        assert cal.classify(None) == "error"


class TestLevelClean:
    def test_clean_when_no_429_and_no_errors(self):
        assert cal.level_clean({"ok": 8, "rate_limited": 0, "error": 0})

    def test_one_transient_error_is_tolerated(self):
        assert cal.level_clean({"ok": 7, "rate_limited": 0, "error": 1})

    def test_any_429_is_dirty(self):
        assert not cal.level_clean({"ok": 7, "rate_limited": 1, "error": 0})

    def test_two_errors_are_dirty(self):
        assert not cal.level_clean({"ok": 6, "rate_limited": 0, "error": 2})


class TestChoose:
    def test_highest_clean_level_wins(self):
        assert cal.choose([2, 4, 6], floor=2, ceiling=10) == 6

    def test_ceiling_caps_the_recommendation(self):
        assert cal.choose([2, 4, 6, 8], floor=2, ceiling=6) == 6

    def test_no_clean_level_falls_to_floor(self):
        assert cal.choose([], floor=2, ceiling=10) == 2


class TestStaircase:
    def test_all_clean_recommends_highest_probed_level(self):
        result = cal.staircase(lambda: "ok", [2, 4, 6], waves=1, floor=2, ceiling=10)
        assert result["recommended"] == 6
        assert [r["level"] for r in result["levels"]] == [2, 4, 6]

    def test_stops_at_first_dirty_level(self):
        # Clean through level 4, then every request 429s.
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            return "ok" if calls["n"] <= 2 + 4 else "rate_limited"

        result = cal.staircase(fn, [2, 4, 6, 8], waves=1, floor=2, ceiling=10)
        assert result["recommended"] == 4
        # Level 8 was never probed (6 was dirty).
        assert [r["level"] for r in result["levels"]] == [2, 4, 6]

    def test_fully_throttled_model_gets_the_floor(self):
        result = cal.staircase(lambda: "rate_limited", [2, 4], waves=1, floor=2, ceiling=10)
        assert result["recommended"] == 2
        assert len(result["levels"]) == 1  # stopped immediately

    def test_levels_above_the_ceiling_are_not_probed(self):
        result = cal.staircase(lambda: "ok", [2, 4, 16], waves=1, floor=2, ceiling=10)
        assert [r["level"] for r in result["levels"]] == [2, 4]
        assert result["recommended"] == 4

    def test_waves_multiply_the_request_count(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            return "ok"

        cal.staircase(fn, [3], waves=2, floor=2, ceiling=10)
        assert calls["n"] == 6  # 2 waves x 3 concurrent
