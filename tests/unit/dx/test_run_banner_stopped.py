"""Stage-3 tests for the ``stopped_reason`` variant of the run-end banner.

Locks :func:`tolokaforge.dx.banners.print_run_end_banner`'s new
optional kwarg (default ``None``, preserving A5's signature).

Three variants — success, failure, stopped:

* ``stopped_reason=None`` + ``success=True`` → ``✓ Run complete in <dur>``.
* ``stopped_reason=None`` + ``success=False`` → ``✗ Run failed in <dur>``.
* ``stopped_reason`` set → ``⏸ Run stopped (<reason>) in <dur>``,
  regardless of the success axis (a budget cut a running-fine run).

Report + browse lines are unchanged in every variant.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from tolokaforge.dx._display import THEME
from tolokaforge.dx.banners import print_run_end_banner

pytestmark = pytest.mark.unit


def _make_recording_console(width: int = 240) -> Console:
    return Console(
        file=io.StringIO(),
        record=True,
        force_terminal=True,
        color_system="truecolor",
        width=width,
        theme=THEME,
        soft_wrap=True,
    )


class TestStoppedVariant:
    """The ``stopped_reason`` kwarg produces a ⏸ outcome line."""

    def test_stopped_reason_supersedes_success(self) -> None:
        console = _make_recording_console()
        print_run_end_banner(
            run_id="my_run",
            run_dir=Path("/tmp/results/my_run"),
            duration_seconds=125.0,
            success=True,  # would render "✓ Run complete" without stopped_reason
            console=console,
            stopped_reason="cost limit",
        )
        text = console.export_text()
        assert "⏸ Run stopped (cost limit) in 02:05" in text
        assert "✓" not in text
        assert "✗" not in text

    def test_stopped_reason_supersedes_failure(self) -> None:
        console = _make_recording_console()
        print_run_end_banner(
            run_id="my_run",
            run_dir=Path("/tmp/results/my_run"),
            duration_seconds=1.0,
            success=False,
            console=console,
            stopped_reason="time limit",
        )
        text = console.export_text()
        assert "⏸ Run stopped (time limit) in 00:01" in text
        assert "✗ Run failed" not in text

    @pytest.mark.parametrize(
        "reason",
        ["cost limit", "time limit", "sample limit"],
    )
    def test_reason_string_is_rendered_literally(self, reason: str) -> None:
        console = _make_recording_console()
        print_run_end_banner(
            run_id="my_run",
            run_dir=Path("/tmp/results/my_run"),
            duration_seconds=1.0,
            success=False,
            console=console,
            stopped_reason=reason,
        )
        text = console.export_text()
        assert f"⏸ Run stopped ({reason}) in" in text

    def test_report_and_browse_lines_unchanged_under_stopped(self) -> None:
        console = _make_recording_console()
        print_run_end_banner(
            run_id="my_run",
            run_dir=Path("/tmp/results/my_run"),
            duration_seconds=1.0,
            success=False,
            console=console,
            stopped_reason="cost limit",
        )
        text = console.export_text()
        assert "→ Report:" in text
        assert "→ Browse: tolokaforge browse my_run" in text


class TestBackwardCompatibility:
    """The pre-Stage-3 signature keeps working — ``stopped_reason`` defaults
    to ``None`` and the outcome line is picked by ``success`` alone."""

    def test_no_stopped_reason_success_still_renders_check(self) -> None:
        console = _make_recording_console()
        print_run_end_banner(
            run_id="my_run",
            run_dir=Path("/tmp/results/my_run"),
            duration_seconds=125.0,
            success=True,
            console=console,
        )
        text = console.export_text()
        assert "✓ Run complete in 02:05" in text
        assert "⏸" not in text

    def test_no_stopped_reason_failure_still_renders_cross(self) -> None:
        console = _make_recording_console()
        print_run_end_banner(
            run_id="my_run",
            run_dir=Path("/tmp/results/my_run"),
            duration_seconds=3665.0,
            success=False,
            console=console,
        )
        text = console.export_text()
        assert "✗ Run failed in 01:01:05" in text
        assert "⏸" not in text
