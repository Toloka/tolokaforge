"""Locks :func:`is_db_transport_error` — the db_probe error taxonomy classifier.

The classifier decides whether an exception raised while connecting to / querying
a probe DSN is a transport failure (DB unreachable → trial ungradeable) or a
query failure (DB answered, query rejected → FAIL line). asyncpg is not installed
on the host engine (only in the runner image), so the asyncpg-family branch is
exercised against a stub module injected into ``sys.modules`` whose exception
classes carry the same names the real driver exports; the OSError branch needs no
driver.
"""

from __future__ import annotations

import sys
import types

import pytest

from tolokaforge.core.grading.db_probes import is_db_transport_error

pytestmark = pytest.mark.unit


def test_oserror_family_is_transport_without_asyncpg() -> None:
    # ConnectionRefusedError / timeouts / DNS failures surface as OSError on
    # connect; classified transport without needing the driver.
    assert is_db_transport_error(ConnectionRefusedError("refused")) is True
    assert is_db_transport_error(ConnectionError("dropped")) is True
    assert is_db_transport_error(OSError("socket")) is True


def test_non_oserror_without_asyncpg_is_query_failure() -> None:
    # No driver importable + not an OSError → the DB answered; a query failure.
    assert is_db_transport_error(RuntimeError("undefined table")) is False
    assert is_db_transport_error(ValueError("bad shape")) is False


def _fake_asyncpg() -> types.ModuleType:
    """A stub ``asyncpg`` exposing the exception families the classifier names."""
    mod = types.ModuleType("asyncpg")

    class PostgresError(Exception): ...

    class InterfaceError(Exception): ...

    class ConnectionDoesNotExistError(InterfaceError): ...

    class PostgresConnectionError(PostgresError): ...

    class CannotConnectNowError(PostgresError): ...

    class PostgresSyntaxError(PostgresError): ...

    class UndefinedTableError(PostgresError): ...

    mod.PostgresError = PostgresError  # type: ignore[attr-defined]
    mod.InterfaceError = InterfaceError  # type: ignore[attr-defined]
    mod.ConnectionDoesNotExistError = ConnectionDoesNotExistError  # type: ignore[attr-defined]
    mod.PostgresConnectionError = PostgresConnectionError  # type: ignore[attr-defined]
    mod.CannotConnectNowError = CannotConnectNowError  # type: ignore[attr-defined]
    mod.PostgresSyntaxError = PostgresSyntaxError  # type: ignore[attr-defined]
    mod.UndefinedTableError = UndefinedTableError  # type: ignore[attr-defined]
    return mod


@pytest.fixture
def fake_asyncpg(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    mod = _fake_asyncpg()
    monkeypatch.setitem(sys.modules, "asyncpg", mod)
    return mod


def test_asyncpg_connection_families_are_transport(fake_asyncpg: types.ModuleType) -> None:
    for name in (
        "InterfaceError",
        "ConnectionDoesNotExistError",
        "PostgresConnectionError",
        "CannotConnectNowError",
    ):
        exc = getattr(fake_asyncpg, name)("boom")
        assert is_db_transport_error(exc) is True, name


def test_asyncpg_query_errors_are_not_transport(fake_asyncpg: types.ModuleType) -> None:
    for name in ("PostgresSyntaxError", "UndefinedTableError", "PostgresError"):
        exc = getattr(fake_asyncpg, name)("boom")
        assert is_db_transport_error(exc) is False, name
