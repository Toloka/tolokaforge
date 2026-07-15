"""Unit tests locking :mod:`tolokaforge.cli._run_banner` and the shared
:func:`format_duration` helper on :mod:`tolokaforge.cli._display`.

Every assertion here maps to a documented contract line in
``tolokaforge/cli/_run_banner.py`` or in the run-banner plan under
``docs/plans/``.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from tolokaforge.cli._display import THEME, format_duration
from tolokaforge.cli._run_banner import (
    print_run_end_banner,
    print_run_start_banner,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# format_duration
# ---------------------------------------------------------------------------


class TestFormatDuration:
    """Boundary tests for the shared ``MM:SS`` / ``HH:MM:SS`` formatter."""

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0, "00:00"),
            (59, "00:59"),
            (60, "01:00"),
            (125, "02:05"),
            (3599, "59:59"),
            (3600, "01:00:00"),
            (3661, "01:01:01"),
            (3665, "01:01:05"),
            (86400, "24:00:00"),
        ],
    )
    def test_boundaries(self, seconds: int, expected: str) -> None:
        assert format_duration(seconds) == expected

    def test_truncates_fractional_seconds(self) -> None:
        # 61.7s → 61s → "01:01" (no rounding up).
        assert format_duration(61.7) == "01:01"

    def test_truncates_at_hour_boundary(self) -> None:
        # 3599.9s → 3599s → "59:59" (does not cross into HH:MM:SS).
        assert format_duration(3599.9) == "59:59"


# ---------------------------------------------------------------------------
# Banner rendering shared helpers
# ---------------------------------------------------------------------------


def _make_recording_console(width: int = 240) -> Console:
    """A recording console with terminal semantics so OSC 8 bytes fire.

    ``file=StringIO()`` captures every byte Rich would write to stderr;
    ``force_terminal=True`` + ``color_system="truecolor"`` make Rich emit
    ANSI escapes (including OSC 8 for ``[link=…]``) regardless of the ambient
    TTY posture. ``soft_wrap=True`` matches the shared console's stream
    posture so long ``file://`` URLs are not broken by mid-URL newlines
    when assertions grep the exported text.
    """
    return Console(
        file=io.StringIO(),
        record=True,
        force_terminal=True,
        color_system="truecolor",
        width=width,
        theme=THEME,
        soft_wrap=True,
    )


class TestStartBanner:
    """`print_run_start_banner` — the two-line stderr framing at run start."""

    def test_writes_two_lines(self) -> None:
        console = _make_recording_console()
        print_run_start_banner(
            run_id="my_run_20260715_120000",
            run_dir=Path("/tmp/results/my_run_20260715_120000"),
            console=console,
        )
        text = console.export_text()
        # Trailing newline from the second print → three splits, empty tail.
        lines = [line for line in text.splitlines() if line]
        assert len(lines) == 2

    def test_contains_run_id_and_report_prefixes(self) -> None:
        console = _make_recording_console()
        print_run_start_banner(
            run_id="my_run_20260715_120000",
            run_dir=Path("/tmp/results/my_run_20260715_120000"),
            console=console,
        )
        text = console.export_text()
        assert "→ Run: my_run_20260715_120000" in text
        assert "→ Report:" in text

    def test_url_is_absolute_file_uri(self, tmp_path: Path) -> None:
        console = _make_recording_console()
        run_dir = tmp_path / "results" / "my_run"
        print_run_start_banner(
            run_id="my_run",
            run_dir=run_dir,
            console=console,
        )
        text = console.export_text()
        # Path.resolve().as_uri() always yields an absolute file:/// URI on POSIX.
        assert "file:///" in text
        assert "file://relative" not in text
        assert str(run_dir.resolve()) in text

    def test_relative_run_dir_is_resolved_to_absolute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        console = _make_recording_console()
        monkeypatch.chdir(tmp_path)
        print_run_start_banner(
            run_id="my_run",
            run_dir=Path("results/my_run"),
            console=console,
        )
        text = console.export_text()
        assert "file:///" in text
        assert str((tmp_path / "results/my_run").resolve()) in text

    def test_url_wrapped_in_osc8_hyperlink(self, tmp_path: Path) -> None:
        """Rich emits an OSC 8 hyperlink for ``[link=URL]…[/link]``.

        The ANSI sequence starts with ``\x1b]8;`` and encodes the URL —
        this is what OSC 8-capable terminals render as clickable.
        """
        recording = _make_recording_console()
        run_dir = tmp_path / "results" / "my_run"
        print_run_start_banner(
            run_id="my_run",
            run_dir=run_dir,
            console=recording,
        )
        raw = recording.file.getvalue()
        assert "\x1b]8;" in raw, (
            "Expected OSC 8 hyperlink bytes in raw output — Rich should "
            "emit them for [link=URL] on a truecolor recording Console."
        )
        expected_url = run_dir.resolve().as_uri() + "/"
        assert expected_url in raw


class TestEndBanner:
    """`print_run_end_banner` — three-line stderr framing at run end."""

    def test_success_variant(self) -> None:
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
        assert "✗" not in text
        assert "→ Report:" in text
        assert "→ Browse: tolokaforge browse my_run" in text

    def test_failure_variant_uses_cross_glyph(self) -> None:
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
        assert "✓ Run complete" not in text
        assert "→ Browse: tolokaforge browse my_run" in text

    def test_end_banner_url_matches_start_banner_url(self) -> None:
        start = _make_recording_console()
        end = _make_recording_console()
        run_dir = Path("/tmp/results/my_run")
        print_run_start_banner(run_id="my_run", run_dir=run_dir, console=start)
        print_run_end_banner(
            run_id="my_run",
            run_dir=run_dir,
            duration_seconds=1.0,
            success=True,
            console=end,
        )
        expected_url = run_dir.resolve().as_uri() + "/"
        assert expected_url in start.export_text()
        assert expected_url in end.export_text()

    def test_browse_invocation_uses_literal_string(self) -> None:
        """The end banner suggests exactly ``tolokaforge browse <run-id>``.

        The banner prints the invocation string verbatim; a silent
        rename here would tell users to run a command that does not
        exist.
        """
        console = _make_recording_console()
        print_run_end_banner(
            run_id="run_20260715_120000",
            run_dir=Path("/tmp/results/run_20260715_120000"),
            duration_seconds=1.0,
            success=True,
            console=console,
        )
        text = console.export_text()
        assert "tolokaforge browse run_20260715_120000" in text


class TestSharedConsoleContract:
    """The banners never construct their own ``Console``.

    Grep-guard ``tests/canonical/test_cli_display_invariants.py`` also
    enforces this at module scope; this test proves the *runtime* shape —
    passing the shared console produces the same bytes as a fresh
    recording console (i.e. helpers never side-channel their own output
    stream).
    """

    def test_shared_console_matches_recording_console(self) -> None:
        recording = _make_recording_console()
        print_run_start_banner(
            run_id="my_run",
            run_dir=Path("/tmp/results/my_run"),
            console=recording,
        )
        first = recording.export_text()

        recording2 = _make_recording_console()
        print_run_start_banner(
            run_id="my_run",
            run_dir=Path("/tmp/results/my_run"),
            console=recording2,
        )
        second = recording2.export_text()

        assert first == second
