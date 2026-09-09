"""``orchestrator.py`` carries no adapter-name string branches.

Engine dispatch off adapter identity flows through the class-level capability
flags declared on :class:`~tolokaforge.adapters.base.BaseAdapter` (resolved
via :func:`~tolokaforge.adapters.adapter_class`), never through a string
compare against :class:`~tolokaforge.runner.models.AdapterType` members or an
``isinstance`` check on ``self.adapter``. This guard fails on any regression
that reintroduces either shape inside ``tolokaforge/core/orchestrator.py``.

Scoped to ``orchestrator.py`` alone: adapter-name branches in the sibling
readers under ``_task_loader.py``, ``conductor.py``, and ``rubric_migration.py``
are the concern of separate guardrails beside those files. Constructive uses
of :class:`AdapterType` — default naming, imports — are not name-branches and
do not appear in any pattern here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.canonical


_ORCHESTRATOR = Path(__file__).resolve().parents[2] / "tolokaforge/core/orchestrator.py"


def _matching_lines(pattern: re.Pattern[str] | str) -> list[tuple[int, str]]:
    text = _ORCHESTRATOR.read_text()
    if isinstance(pattern, str):
        return [(n, line) for n, line in enumerate(text.splitlines(), start=1) if pattern in line]
    return [
        (n, line)
        for n, line in enumerate(text.splitlines(), start=1)
        if pattern.search(line) is not None
    ]


def _format(hits: list[tuple[int, str]]) -> str:
    return "\n".join(f"  :{n}: {line}" for n, line in hits)


def test_orchestrator_has_no_terminal_bench_name_branch() -> None:
    """``AdapterType.TERMINAL_BENCH`` name-branches are the smell this ticket kills."""
    hits = _matching_lines("AdapterType.TERMINAL_BENCH")
    assert not hits, (
        "tolokaforge/core/orchestrator.py must not name-branch on "
        "AdapterType.TERMINAL_BENCH — dispatch through adapter_class() and "
        "the adapter's `requires_docker_cli_in_runner` capability flag "
        f"instead. Offending lines:\n{_format(hits)}"
    )


def test_orchestrator_has_no_adapter_type_equality_branch() -> None:
    """``adapter_type == AdapterType.<X>`` name-branches are the shape this ticket forbids."""
    hits = _matching_lines(re.compile(r"\badapter_type\s*==\s*AdapterType\."))
    assert not hits, (
        "tolokaforge/core/orchestrator.py must not branch on adapter identity "
        "with `adapter_type == AdapterType.<X>` — dispatch through "
        "adapter_class() and the adapter's own capability flags instead. "
        f"Offending lines:\n{_format(hits)}"
    )


def test_orchestrator_has_no_isinstance_self_adapter_branch() -> None:
    """``isinstance(self.adapter, <NamedAdapter>)`` name-branches are the shape this ticket forbids."""
    hits = _matching_lines(re.compile(r"\bisinstance\(\s*self\.adapter\s*,"))
    assert not hits, (
        "tolokaforge/core/orchestrator.py must not branch on adapter identity "
        "with `isinstance(self.adapter, ...)` — dispatch through the "
        "adapter's own capability flags on the class instead. "
        f"Offending lines:\n{_format(hits)}"
    )
