"""Canonical invariants for the shared CLI display layer.

Guards keeping future contributors on the shared path:

* ``test_no_ad_hoc_console_in_cli`` walks ``tolokaforge/dx/**/*.py`` and
  asserts that no module outside :mod:`tolokaforge.dx._display` constructs
  its own ``rich.Console`` — every CLI surface routes through the shared
  instance so theme, stream posture, and progress plumbing stay uniform.
* ``test_display_module_exports_public_surface`` pins the public names
  ``console``, ``THEME``, ``make_progress``, and ``make_live`` so a rename
  fails here rather than at every call site.
* ``test_no_bare_stdout_write_in_cli`` forbids ``print(`` and
  ``sys.stdout.write(`` in any CLI module outside :mod:`tolokaforge.dx._display`
  — the sanctioned mechanism is :func:`tolokaforge.dx._display.emit_artifact_path`,
  keeping the stdout=artifact contract intact.
* ``test_emit_artifact_path_is_exported`` pins ``emit_artifact_path`` on the
  ``_display`` public surface alongside ``console`` / ``THEME`` / ``make_*``.
* ``test_display_mode_enum_surface`` pins the :class:`DisplayMode` member
  order and CLI-literal values — a silent rename or reorder fails here
  before it can drift the ``--display`` choice set or the operator
  ``TOLOKAFORGE_DISPLAY`` protocol.
* ``test_select_display_mode_is_exported`` and
  ``test_silence_root_logging_is_exported`` pin the display-mode selector
  and silencing helpers on their respective modules.
* ``test_display_env_var_literal_is_TOLOKAFORGE_DISPLAY`` locks the
  ``TOLOKAFORGE_DISPLAY`` operator env-var literal.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.canonical

CLI_DIR = Path(__file__).resolve().parents[2] / "tolokaforge" / "dx"
_EXEMPT_FILES = frozenset({"_display.py", "__init__.py"})
_CONSOLE_CALL = re.compile(r"\bConsole\s*\(")
_BARE_STDOUT_WRITE = re.compile(r"(?:^|\s)(print\s*\(|sys\.stdout\.write\s*\()")


def test_no_ad_hoc_console_in_cli() -> None:
    """Every CLI module routes through ``tolokaforge.dx._display.console``.

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
        "Ad-hoc rich.Console constructions found in tolokaforge/dx/ — "
        "import `console` from `tolokaforge.dx._display` instead:\n  " + "\n  ".join(offenders)
    )


def test_display_module_exports_public_surface() -> None:
    """The four public names of ``_display`` must stay stable."""

    import tolokaforge.dx._display as display

    assert hasattr(display, "console"), "missing shared `console`"
    assert hasattr(display, "THEME"), "missing `THEME`"
    assert callable(getattr(display, "make_progress", None)), "`make_progress` not callable"
    assert callable(getattr(display, "make_live", None)), "`make_live` not callable"


def test_no_bare_stdout_write_in_cli() -> None:
    """Every CLI stdout write goes through ``emit_artifact_path``.

    A bare ``print(`` or ``sys.stdout.write(`` outside ``_display.py`` would
    break the ``stdout=artifact-path`` shell-composition contract locked in
    ``docs/CLI.md`` § stdout / stderr contract. The regex is anchored to
    start-of-line or whitespace so identifiers like ``pprint``, ``sprint``,
    ``imprint``, and attribute calls like ``console.print`` do not match.
    The failure message names every offending ``file:line``.
    """

    offenders: list[str] = []
    for path in sorted(CLI_DIR.rglob("*.py")):
        if path.name in _EXEMPT_FILES:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if _BARE_STDOUT_WRITE.search(line):
                offenders.append(
                    f"{path.relative_to(CLI_DIR.parent.parent)}:{lineno}: {line.strip()}"
                )

    assert not offenders, (
        "Bare `print(` or `sys.stdout.write(` found in tolokaforge/dx/ — "
        "route the emission through `emit_artifact_path` in "
        "`tolokaforge.dx._display`, or send human/progress/log output through "
        "the shared `console` (stderr):\n  " + "\n  ".join(offenders)
    )


def test_emit_artifact_path_is_exported() -> None:
    """``emit_artifact_path`` is the sanctioned stdout write helper."""

    from tolokaforge.dx._display import emit_artifact_path

    assert callable(emit_artifact_path), "`emit_artifact_path` not callable"


def test_display_mode_enum_surface() -> None:
    """``DisplayMode`` member order and CLI-literal values are locked.

    Order matters — ``click.Choice([m.value for m in DisplayMode])`` renders
    the modes in enum order in ``--help``, and consumers grep the operator
    surface (`TOLOKAFORGE_DISPLAY=full/rich/plain/log/none`) against the
    literals below. Reordering or renaming a member without a CHANGELOG
    entry fails here.
    """

    from tolokaforge.dx._display import DisplayMode

    assert list(DisplayMode.__members__) == ["FULL", "RICH", "PLAIN", "LOG", "NONE"]
    assert DisplayMode.FULL.value == "full"
    assert DisplayMode.RICH.value == "rich"
    assert DisplayMode.PLAIN.value == "plain"
    assert DisplayMode.LOG.value == "log"
    assert DisplayMode.NONE.value == "none"


def test_select_display_mode_is_exported() -> None:
    """``select_display_mode`` and ``silence_console`` live in ``_display``.

    B1 (Rich Live panel, #285) and C3 (Textual TUI, #289) both import
    ``select_display_mode`` to derive a fresh mode from an explicit
    override in library-mode entry points; ``silence_console`` is the
    ``--display=none`` knob. Pinning both here fails a silent module move.
    """

    from tolokaforge.dx._display import select_display_mode, silence_console

    assert callable(select_display_mode), "`select_display_mode` not callable"
    assert callable(silence_console), "`silence_console` not callable"


def test_silence_root_logging_is_exported() -> None:
    """``silence_root_logging`` lives in :mod:`tolokaforge.core.logging`.

    Paired with :func:`silence_console` under ``--display=none`` — one
    silences the shared Rich console, the other raises the tolokaforge
    root log handler above ``CRITICAL``. Pinning the module location here
    fails a silent move to ``tolokaforge.dx`` or ``tolokaforge.dx._display``.
    """

    from tolokaforge.core.logging import silence_root_logging

    assert callable(silence_root_logging), "`silence_root_logging` not callable"


def test_display_env_var_literal_is_TOLOKAFORGE_DISPLAY() -> None:
    """``TOLOKAFORGE_DISPLAY`` is the operator env-var literal.

    A silent rename to ``TOLOKAFORGE_DISPLAY_MODE`` (or similar) would
    break every wrapper script and CI pipeline that exports the current
    name. The selector is called with an explicit ``env`` dict so the
    assertion is deterministic and independent of the ambient shell.
    """

    from tolokaforge.dx._display import DisplayMode, select_display_mode

    resolved = select_display_mode(explicit=None, env={"TOLOKAFORGE_DISPLAY": "log"})
    assert resolved is DisplayMode.LOG
