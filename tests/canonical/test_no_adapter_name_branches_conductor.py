"""``conductor.py`` carries no adapter-name branches.

The conductor dispatches off the class-level capability flags declared on
:class:`~tolokaforge.adapters.base.BaseAdapter` — a plain attribute-read on
``self.adapter`` — never an ``isinstance`` check against a named adapter
class or a string compare against :class:`~tolokaforge.runner.models.AdapterType`
members. This guard fails on any regression that reintroduces either shape
inside ``tolokaforge/core/conductor.py``.

Scoped to ``conductor.py`` alone: adapter-name branches in the sibling
readers under ``_task_loader.py``, ``orchestrator.py``, and
``rubric_migration.py`` are the concern of separate guardrails beside those
files.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.canonical


_CONDUCTOR = Path(__file__).resolve().parents[2] / "tolokaforge/core/conductor.py"


def _matching_lines(pattern: re.Pattern[str] | str) -> list[tuple[int, str]]:
    text = _CONDUCTOR.read_text()
    if isinstance(pattern, str):
        return [(n, line) for n, line in enumerate(text.splitlines(), start=1) if pattern in line]
    return [
        (n, line)
        for n, line in enumerate(text.splitlines(), start=1)
        if pattern.search(line) is not None
    ]


def _format(hits: list[tuple[int, str]]) -> str:
    return "\n".join(f"  :{n}: {line}" for n, line in hits)


def test_conductor_has_no_isinstance_self_adapter_branch() -> None:
    """``isinstance(self.adapter, <NamedAdapter>)`` name-branches are the smell this ticket kills."""
    hits = _matching_lines(re.compile(r"\bisinstance\(\s*self\.adapter\s*,"))
    assert not hits, (
        "tolokaforge/core/conductor.py must not branch on adapter identity "
        "with `isinstance(self.adapter, ...)` — read the adapter's own "
        "capability flag off the class instead. "
        f"Offending lines:\n{_format(hits)}"
    )


def test_conductor_has_no_native_adapter_import() -> None:
    """``conductor.py`` imports no :class:`NativeAdapter` — the adapter-env sync gate reads the ``syncs_adapter_env_to_state`` flag off the class."""
    hits = _matching_lines("from tolokaforge.adapters.native import NativeAdapter")
    assert not hits, (
        "tolokaforge/core/conductor.py must not import NativeAdapter — the "
        "adapter-env sync gate reads the ``syncs_adapter_env_to_state`` "
        "capability flag instead of matching adapter identity. "
        f"Offending lines:\n{_format(hits)}"
    )


def test_conductor_has_no_adapter_type_equality_branch() -> None:
    """``adapter_type == AdapterType.<X>`` name-branches are the shape this ticket forbids."""
    hits = _matching_lines(re.compile(r"\badapter_type\s*==\s*AdapterType\."))
    assert not hits, (
        "tolokaforge/core/conductor.py must not branch on adapter identity "
        "with `adapter_type == AdapterType.<X>` — read the adapter's own "
        "capability flag instead. "
        f"Offending lines:\n{_format(hits)}"
    )
