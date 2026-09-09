"""Locks :meth:`InProcessGradingSubstrate.db_probe`.

Feeds :func:`~tolokaforge.core.grading.db_probes._fetch_probe_rows` a scripted
row set that mixes ``datetime`` scalars with str / int columns; asserts the
returned rows list carries the datetime coerced to ``str(dt)`` — the
JSON-round-trip ``json.dumps(rows, default=str) + json.loads(...)`` the method
applies. Symmetric with the wire-side lock in
``tests/unit/runner/test_substrate_service_run_db_probe.py`` — both legs land
byte-identical values on the same input.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from tolokaforge.core.grading import db_probes as db_probes_module
from tolokaforge.core.grading.substrate import InProcessGradingSubstrate

pytestmark = pytest.mark.unit


def _make_substrate() -> InProcessGradingSubstrate:
    return InProcessGradingSubstrate(
        db_reader=object(),  # type: ignore[arg-type]
        knowledge_search=None,
        filesystem_root=None,
    )


def test_datetime_scalar_is_coerced_to_string(monkeypatch: pytest.MonkeyPatch) -> None:
    scripted_dt = _dt.datetime(2026, 1, 1, 12, 34, 56)

    async def fake_fetch(dsn: str, query: str) -> list[dict[str, object]]:  # noqa: ARG001
        return [{"a": "x", "b": 1, "c": scripted_dt}]

    monkeypatch.setattr(db_probes_module, "_fetch_probe_rows", fake_fetch)

    rows = _make_substrate().db_probe("postgresql://x", "SELECT ...")

    assert rows == [{"a": "x", "b": 1, "c": str(scripted_dt)}]


def test_empty_result_set_returns_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch(dsn: str, query: str) -> list[dict[str, object]]:  # noqa: ARG001
        return []

    monkeypatch.setattr(db_probes_module, "_fetch_probe_rows", fake_fetch)

    assert _make_substrate().db_probe("postgresql://x", "SELECT ...") == []
