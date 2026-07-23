"""Unit tests for :func:`tolokaforge.core.duration.parse_duration`."""

from __future__ import annotations

import pytest

from tolokaforge.core.duration import parse_duration

pytestmark = pytest.mark.unit


class TestValidSpecs:
    """Every accepted shape lands on the expected seconds value."""

    @pytest.mark.parametrize(
        "spec,expected_seconds",
        [
            ("30m", 1800.0),
            ("2h", 7200.0),
            ("1h30m", 5400.0),
            ("90s", 90.0),
            ("1.5h", 5400.0),
            ("1d12h", 129600.0),
            ("1h30m45s", 5445.0),
            ("1d", 86400.0),
            ("0s", 0.0),
        ],
    )
    def test_parses_to_seconds(self, spec: str, expected_seconds: float) -> None:
        assert parse_duration(spec) == pytest.approx(expected_seconds)

    def test_returns_float(self) -> None:
        assert isinstance(parse_duration("30s"), float)


class TestInvalidSpecs:
    """Every rejected shape raises :class:`ValueError` and names the input."""

    @pytest.mark.parametrize(
        "spec,match_phrase",
        [
            ("", "empty duration"),
            ("   ", "empty duration"),
            ("abc", "unparseable token"),
            ("30", "bare number"),
            ("30x", "unknown unit"),
            ("-30m", "negative"),
            ("1x", "unknown unit"),
            ("30m10", "bare number"),
            ("1h 30m", "unparseable token"),
        ],
    )
    def test_raises_value_error(self, spec: str, match_phrase: str) -> None:
        with pytest.raises(ValueError, match=match_phrase):
            parse_duration(spec)

    def test_error_message_includes_spec(self) -> None:
        with pytest.raises(ValueError, match="'30x'"):
            parse_duration("30x")
