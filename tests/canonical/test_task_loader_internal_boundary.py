"""Lock that ``_task_loader`` stays internal to ``tolokaforge.adapters``.

External-facing surfaces — the shipped ``examples/`` scripts and the
``docs/`` prose downstream harnesses copy verbatim — must reach a
``TaskConfig`` through the public ``tolokaforge.runner.load_task`` helper, never
by importing the private ``tolokaforge.adapters._task_loader`` module.

The guard asserts the bare module token ``_task_loader`` appears nowhere under
``examples/`` or ``docs/``, so it catches every re-introduction form at once:
the ``from … import load_task_yaml`` line, the dotted
``tolokaforge.adapters._task_loader.load_task_yaml`` reference, an aliased
import, and the ``from …adapters import _task_loader`` module import.

Architecture Decision Records under ``docs/adr/`` are internal design records
that legitimately name the internals they decide on, so the scan skips them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.canonical

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCANNED_ROOTS = ("examples", "docs")
_SCAN_EXCLUDED_DIRS = ("docs/adr",)
_TEXT_SUFFIXES = {".py", ".md", ".rst", ".txt", ".yaml", ".yml", ".sh", ".toml"}
_FORBIDDEN_TOKEN = "_task_loader"


def _scanned_files() -> list[Path]:
    excluded = tuple((_REPO_ROOT / part).resolve() for part in _SCAN_EXCLUDED_DIRS)
    files: list[Path] = []
    for root in _SCANNED_ROOTS:
        files.extend(
            path
            for path in (_REPO_ROOT / root).rglob("*")
            if path.is_file()
            and path.suffix in _TEXT_SUFFIXES
            and not any(
                str(path.resolve()).startswith(str(excluded_root)) for excluded_root in excluded
            )
        )
    return files


def test_task_loader_not_referenced_outside_adapters():
    offenders = [
        str(path.relative_to(_REPO_ROOT))
        for path in _scanned_files()
        if _FORBIDDEN_TOKEN in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert not offenders, (
        f"{_FORBIDDEN_TOKEN!r} is internal to tolokaforge.adapters; reach a "
        "TaskConfig through tolokaforge.runner.load_task instead. Found in: "
        + ", ".join(sorted(offenders))
    )
