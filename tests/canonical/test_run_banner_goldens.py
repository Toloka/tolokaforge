"""Byte-level canonical SVG goldens for the run-start / run-end banners.

Each golden pins the exact ``Console.export_svg`` bytes Rich produces when
:func:`print_run_start_banner` / :func:`print_run_end_banner` are called
against a recording console at fixed 80-column width. Four goldens cover
the four distinct visual shapes:

* ``banner_start.svg`` — two lines: ``→ Run: …`` + ``→ Report: …``.
* ``banner_start_resume.svg`` — two lines: ``→ Resume: …`` +
  ``→ Report: …`` (the ``resumed=True`` variant used by
  ``tolokaforge run --resume``).
* ``banner_end_success.svg`` — ``✓ Run complete in <duration>`` +
  ``→ Report`` + ``→ Browse`` (duration in ``MM:SS``).
* ``banner_end_failure.svg`` — ``✗ Run failed in <duration>`` + same
  trailing lines (duration in ``HH:MM:SS`` — exercises the hour branch).

**Golden regeneration.**

    uv run pytest tests/canonical/test_run_banner_goldens.py --update-canon

Determinism knobs mirror ``test_run_display_goldens.py``:

* ``unique_id="tolokaforge-run-banner"`` fixes the CSS class prefix.
* ``theme=DEFAULT_TERMINAL_THEME`` pins the palette embedded in the
  ``<style>`` block.
* ``force_terminal=True`` + ``color_system="truecolor"`` bypass ambient
  capability probes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console
from rich.terminal_theme import DEFAULT_TERMINAL_THEME

from tolokaforge.cli._display import THEME
from tolokaforge.cli._run_banner import (
    print_run_end_banner,
    print_run_start_banner,
)

pytestmark = pytest.mark.canonical

GOLDEN_DIR = Path(__file__).parent / "golden" / "run_banner"

SVG_UNIQUE_ID = "tolokaforge-run-banner"

_WIDTH = 80

_FIXED_RUN_ID = "run_20260715_120000_20260715_120001"
# Absolute path so the URL is deterministic across CI hosts.
_FIXED_RUN_DIR = Path("/Users/ci/results/run_20260715_120000_20260715_120001")


def _make_recorder() -> Console:
    return Console(
        record=True,
        width=_WIDTH,
        force_terminal=True,
        color_system="truecolor",
        theme=THEME,
    )


def _export(recorder: Console) -> str:
    return recorder.export_svg(
        title="tolokaforge run",
        theme=DEFAULT_TERMINAL_THEME,
        unique_id=SVG_UNIQUE_ID,
    )


def _render_start(*, resumed: bool = False) -> str:
    recorder = _make_recorder()
    print_run_start_banner(
        run_id=_FIXED_RUN_ID,
        run_dir=_FIXED_RUN_DIR,
        console=recorder,
        resumed=resumed,
    )
    return _export(recorder)


def _render_end(*, success: bool, duration_seconds: float) -> str:
    recorder = _make_recorder()
    print_run_end_banner(
        run_id=_FIXED_RUN_ID,
        run_dir=_FIXED_RUN_DIR,
        duration_seconds=duration_seconds,
        success=success,
        console=recorder,
    )
    return _export(recorder)


_CASES: tuple[tuple[str, str], ...] = (
    ("banner_start.svg", "start"),
    ("banner_start_resume.svg", "start_resume"),
    ("banner_end_success.svg", "end_success"),
    ("banner_end_failure.svg", "end_failure"),
)


def _render_case(case: str) -> str:
    if case == "start":
        return _render_start()
    if case == "start_resume":
        return _render_start(resumed=True)
    if case == "end_success":
        # 125.4s → 02:05 (MM:SS branch).
        return _render_end(success=True, duration_seconds=125.4)
    if case == "end_failure":
        # 3665.0s → 01:01:05 (HH:MM:SS branch).
        return _render_end(success=False, duration_seconds=3665.0)
    raise AssertionError(f"unknown case: {case!r}")


@pytest.mark.parametrize(("filename", "case"), _CASES, ids=[c for _, c in _CASES])
def test_run_banner_svg(request: pytest.FixtureRequest, filename: str, case: str) -> None:
    """The rendered SVG matches ``<filename>`` byte-for-byte."""

    actual = _render_case(case)
    golden_path = GOLDEN_DIR / filename

    if request.config.getoption("--update-canon"):
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(actual, encoding="utf-8")
        return

    assert golden_path.exists(), (
        f"Golden missing: {golden_path.relative_to(GOLDEN_DIR.parent.parent.parent)}. "
        "Run `uv run pytest tests/canonical/test_run_banner_goldens.py --update-canon`."
    )
    expected = golden_path.read_text(encoding="utf-8")
    if actual != expected:
        pytest.fail(
            f"SVG golden drift for {filename} — re-run with `--update-canon` "
            "if the change is intentional, then review the diff before committing."
        )
