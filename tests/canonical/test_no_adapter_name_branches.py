"""``_task_loader.py`` carries no adapter-name string branches.

The engine reader group dispatches to each adapter through
:func:`~tolokaforge.adapters.adapter_class` and calls into the adapter's own
contract methods — it must not compare adapter identity by string. This guard
fails on any regression that reintroduces an ``AdapterType.NATIVE.value``
match or an ``if adapter_type ==`` / ``if adapter_type !=`` branch inside
``tolokaforge/adapters/_task_loader.py``.

Scoped to the one file per ticket #1341: each sibling reader smell in
``orchestrator.py`` / ``conductor.py`` (#1342) and ``rubric_migration.py``
(#1343) carries its own guardrail if useful.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.canonical


_TASK_LOADER = Path(__file__).resolve().parents[2] / "tolokaforge/adapters/_task_loader.py"


def _matching_lines(pattern: re.Pattern[str] | str) -> list[tuple[int, str]]:
    text = _TASK_LOADER.read_text()
    if isinstance(pattern, str):
        return [(n, line) for n, line in enumerate(text.splitlines(), start=1) if pattern in line]
    return [
        (n, line)
        for n, line in enumerate(text.splitlines(), start=1)
        if pattern.search(line) is not None
    ]


def _format(hits: list[tuple[int, str]]) -> str:
    return "\n".join(f"  :{n}: {line}" for n, line in hits)


def test_task_loader_has_no_adapter_type_native_value_string_compare() -> None:
    """``AdapterType.NATIVE.value`` string compares are the smell this ticket kills."""
    hits = _matching_lines("AdapterType.NATIVE.value")
    assert not hits, (
        "tolokaforge/adapters/_task_loader.py must not compare adapter identity "
        "by string against AdapterType.NATIVE.value — dispatch through "
        "adapter_class() and the adapter's own contract instead. "
        f"Offending lines:\n{_format(hits)}"
    )


def test_task_loader_has_no_adapter_type_equality_branch() -> None:
    """``if adapter_type == ...`` name-branches are the shape this ticket forbids."""
    hits = _matching_lines(re.compile(r"\bif\s+adapter_type\s*=="))
    assert not hits, (
        "tolokaforge/adapters/_task_loader.py must not branch on adapter identity "
        "with `if adapter_type == ...` — dispatch through adapter_class() and the "
        f"adapter's own contract instead. Offending lines:\n{_format(hits)}"
    )


def test_task_loader_has_no_adapter_type_inequality_branch() -> None:
    """``if adapter_type != ...`` name-branches are the shape this ticket forbids."""
    hits = _matching_lines(re.compile(r"\bif\s+adapter_type\s*!="))
    assert not hits, (
        "tolokaforge/adapters/_task_loader.py must not branch on adapter identity "
        "with `if adapter_type != ...` — dispatch through adapter_class() and the "
        f"adapter's own contract instead. Offending lines:\n{_format(hits)}"
    )
