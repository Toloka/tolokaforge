"""Unit tests for the trial-identity helpers.

:func:`format_trial_id` and :func:`trial_output_subpath` are the single source
of truth for a trial's opaque label and its bundle location. Both branch on the
harness entry: empty → today's two-level, no-prefix forms; non-empty → the
entry-prefixed forms. Identity is carried in explicit fields, never parsed back
out of the label, so a ``task_id`` containing a colon is unambiguous.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.core.trial_identity import format_trial_id, trial_output_subpath

pytestmark = pytest.mark.unit


class TestFormatTrialId:
    def test_single_adapter_has_no_entry_prefix(self) -> None:
        assert format_trial_id("", "task-1", 0) == "task-1:0"

    def test_harness_entry_prefixes_the_label(self) -> None:
        assert format_trial_id("claude", "task-1", 2) == "claude:task-1:2"

    def test_task_id_with_colon_is_not_ambiguous(self) -> None:
        # The label is opaque — a colon in the task id never needs to be parsed
        # back out, because identity comes from the explicit (entry, task_id,
        # trial_index) fields, not from splitting this string.
        single = format_trial_id("", "pack:task:v2", 1)
        harness = format_trial_id("aider", "pack:task:v2", 1)
        assert single == "pack:task:v2:1"
        assert harness == "aider:pack:task:v2:1"
        # Two different identities that a naive rsplit would conflate stay
        # distinct because the fields, not the string, decide.
        assert format_trial_id("aider", "pack:task", 1) != harness


class TestTrialOutputSubpath:
    def test_single_adapter_is_two_level(self) -> None:
        assert trial_output_subpath("", "task-1", 0) == Path("task-1") / "0"

    def test_harness_entry_nests_under_the_entry(self) -> None:
        assert trial_output_subpath("claude", "task-1", 3) == Path("claude") / "task-1" / "3"

    def test_colon_task_id_becomes_one_path_segment(self) -> None:
        # The colon-bearing task id is a single directory component under both
        # layouts; nothing splits it.
        assert trial_output_subpath("", "pack:task:v2", 0) == Path("pack:task:v2") / "0"
        assert (
            trial_output_subpath("aider", "pack:task:v2", 0) == Path("aider") / "pack:task:v2" / "0"
        )

    def test_same_task_under_two_entries_does_not_collide(self) -> None:
        a = trial_output_subpath("alpha", "shared", 0)
        b = trial_output_subpath("beta", "shared", 0)
        assert a != b
