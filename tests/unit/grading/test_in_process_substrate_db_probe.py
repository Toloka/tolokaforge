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
from tolokaforge.core.grading.db_probes import DbProbeQueryError
from tolokaforge.core.grading.substrate import (
    InProcessGradingSubstrate,
    SubstrateUnreachableError,
)

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


def test_unreachable_db_raises_substrate_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transport/connection failure (ConnectionError is an OSError) marks the
    trial ungradeable, not a scored 0.0 — the in-process half of the taxonomy."""

    async def fake_fetch(dsn: str, query: str) -> list[dict[str, object]]:  # noqa: ARG001
        raise ConnectionError("could not connect to app-db:5432")

    monkeypatch.setattr(db_probes_module, "_fetch_probe_rows", fake_fetch)

    with pytest.raises(SubstrateUnreachableError):
        _make_substrate().db_probe("postgresql://x", "SELECT ...")


def test_query_failure_raises_db_probe_query_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A query failure (the DB answered and rejected the query) is NOT
    unreachable; it raises DbProbeQueryError, which the backend folds to a FAIL
    line scoring 0.0 for that probe — the trial stays gradeable."""

    async def fake_fetch(dsn: str, query: str) -> list[dict[str, object]]:  # noqa: ARG001
        raise RuntimeError('relation "corrective_actions" does not exist')

    monkeypatch.setattr(db_probes_module, "_fetch_probe_rows", fake_fetch)

    with pytest.raises(DbProbeQueryError) as excinfo:
        _make_substrate().db_probe("postgresql://x", "SELECT ...")
    assert not isinstance(excinfo.value, SubstrateUnreachableError)
    assert "does not exist" in str(excinfo.value)
