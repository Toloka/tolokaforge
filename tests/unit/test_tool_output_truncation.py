"""Behaviour lock for the engine-loop's shared tool-output truncation helper.

The marker string, the head/tail split rule and the loud-on-misconfiguration
guard are the contract the loop-layer cap builds on; any change here breaks
the loop tests and the preset routing test on purpose.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.tool_output_truncation import keep_head_and_tail

pytestmark = pytest.mark.unit


def test_short_input_returned_unchanged() -> None:
    assert keep_head_and_tail("abc", 100) == ("abc", 0)


def test_input_equal_to_cap_returned_unchanged() -> None:
    text = "x" * 100
    assert keep_head_and_tail(text, 100) == (text, 0)


def test_middle_elided_with_exact_marker() -> None:
    truncated, omitted = keep_head_and_tail("a" * 500, 100)
    assert omitted == 400
    assert truncated == "a" * 50 + "\n...[400 chars omitted]...\n" + "a" * 50


def test_odd_cap_splits_both_halves_at_floor_division() -> None:
    truncated, omitted = keep_head_and_tail("a" * 500, 99)
    assert omitted == 402
    assert truncated == "a" * 49 + "\n...[402 chars omitted]...\n" + "a" * 49


def test_head_and_tail_preserved_verbatim() -> None:
    text = "HEAD" + "MIDDLE" * 100 + "TAIL"
    truncated, omitted = keep_head_and_tail(text, 8)
    assert omitted == len(text) - 8
    assert truncated.startswith("HEAD")
    assert truncated.endswith("TAIL")
    assert f"[{omitted} chars omitted]" in truncated


@pytest.mark.parametrize("bad_cap", [0, -1, -100])
def test_non_positive_cap_raises(bad_cap: int) -> None:
    with pytest.raises(ValueError, match=str(bad_cap)):
        keep_head_and_tail("abc", bad_cap)
