"""Canonical invariants for the shared CLI display layer.

Two guards keep future contributors on the shared path:

* ``test_no_ad_hoc_console_in_cli`` walks ``tolokaforge/cli/**/*.py`` and
  asserts that no module outside :mod:`tolokaforge.cli._display` constructs
  its own ``rich.Console`` — every CLI surface routes through the shared
  instance so theme, stream posture, and progress plumbing stay uniform.
* ``test_display_module_exports_public_surface`` pins the public names
  ``console``, ``THEME``, ``make_progress``, and ``make_live`` so a rename
  fails here rather than at every call site.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.canonical

CLI_DIR = Path(__file__).resolve().parents[2] / "tolokaforge" / "cli"
_EXEMPT_FILES = frozenset({"_display.py", "__init__.py"})
_CONSOLE_CALL = re.compile(r"\bConsole\s*\(")


def test_no_ad_hoc_console_in_cli() -> None:
    """Every CLI module routes through ``tolokaforge.cli._display.console``.

    Any ``Console(...)`` construction outside ``_display.py`` breaks the
    shared-theme invariant B1/B2/C1/D1/D4 build on. The failure message
    names every offending ``file:line`` so the fix is obvious.
    """

    offenders: list[str] = []
    for path in sorted(CLI_DIR.rglob("*.py")):
        if path.name in _EXEMPT_FILES:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if _CONSOLE_CALL.search(line):
                offenders.append(
                    f"{path.relative_to(CLI_DIR.parent.parent)}:{lineno}: {line.strip()}"
                )

    assert not offenders, (
        "Ad-hoc rich.Console constructions found in tolokaforge/cli/ — "
        "import `console` from `tolokaforge.cli._display` instead:\n  " + "\n  ".join(offenders)
    )


def test_display_module_exports_public_surface() -> None:
    """The four public names of ``_display`` must stay stable."""

    import tolokaforge.cli._display as display

    assert hasattr(display, "console"), "missing shared `console`"
    assert hasattr(display, "THEME"), "missing `THEME`"
    assert callable(getattr(display, "make_progress", None)), "`make_progress` not callable"
    assert callable(getattr(display, "make_live", None)), "`make_live` not callable"
