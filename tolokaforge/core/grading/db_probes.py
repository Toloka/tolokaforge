"""asyncpg helper for ``db_probes`` state-check evaluation.

:func:`_fetch_probe_rows` opens a task-declared postgres DSN via asyncpg,
runs one read-only SQL query, and returns the rows as ``list[dict]``. Both
substrate implementations that ship today call it from inside the runner
container:

* :meth:`~tolokaforge.core.grading.substrate.InProcessGradingSubstrate.db_probe`
  calls it directly (aggregate-image path — grader and runner in one process).
* :meth:`~tolokaforge.runner.substrate_service.SubstrateServicer.RunDbProbe`
  calls it from the servicer that answers the independent grader's
  ``SubstrateService.RunDbProbe`` RPC.

The DSN is scoped to the task's own postgres — reachable only inside the
task's docker network — not the trial's substrate DB. The runner container
joins that network; the host-side core engine does not, which is why
:mod:`~tolokaforge.core.grading.key_manifest` marks the ``db_probes`` key
``RUNNER_ONLY``.

``asyncpg`` is imported lazily inside :func:`_fetch_probe_rows` so importing
this module does not require the driver. The runner Docker image installs
``asyncpg`` at build time; the canonical lock in
``tests/canonical/test_runner_image_db_driver_canon.py`` guards the
requirement.
"""

from __future__ import annotations

from typing import Any


class DbProbeQueryError(Exception):
    """A db_probe failed for a reason that is NOT a transport/connection problem.

    The substrate reached the probe's postgres and the *query* itself failed —
    bad SQL, a missing table, a type error shaping the result. Grading folds
    this to a per-probe ``FAIL`` line scoring 0.0 for that probe, exactly as it
    folds any non-transport exception. Distinct from
    :class:`~tolokaforge.core.grading.substrate.SubstrateUnreachableError`,
    which means the DB could not be reached at all and marks the trial
    ungradeable. Keeping the two apart is what stops an unreachable DB from
    laundering to a real 0.0 and a bad query from wrongly booking ungradeable.
    """


def is_db_transport_error(exc: BaseException) -> bool:
    """Classify an exception raised while connecting to / querying a probe DSN.

    Returns ``True`` for a transport/connection failure (the DB could not be
    reached — connection refused, DSN down, the driver's connection dropped or
    never established, the server still starting up); the caller raises
    :class:`~tolokaforge.core.grading.substrate.SubstrateUnreachableError` and
    the trial is ungradeable. Returns ``False`` for a query failure (the DB
    answered and the query failed: bad SQL, missing table, a type error); the
    caller folds it to a ``FAIL`` line.

    Classification is by exception family. ``OSError`` covers socket-level
    failures surfaced on connect (``ConnectionRefusedError``, timeouts, DNS).
    The asyncpg connection families are matched by class when the driver is
    importable; it is absent from the host engine (installed only in the runner
    image), so the import is guarded and the OSError branch still classifies
    the common connection-refused case without it.
    """
    if isinstance(exc, OSError):
        return True
    try:
        import asyncpg
    except ImportError:
        return False
    # Client-side interface errors (connection never established / dropped —
    # ``ConnectionDoesNotExistError`` subclasses this) and server-side
    # connection-class errors (SQLSTATE 08) plus "cannot connect now" (the
    # server is still starting) are transport failures. Any other
    # ``PostgresError`` (syntax error, undefined table, a data/type error) means
    # the server answered and the query failed, so it is left for the FAIL fold.
    connection_family_names = (
        "InterfaceError",
        "PostgresConnectionError",
        "ConnectionDoesNotExistError",
        "CannotConnectNowError",
    )
    connection_families = tuple(
        cls
        for name in connection_family_names
        if (cls := getattr(asyncpg, name, None)) is not None and isinstance(cls, type)
    )
    return bool(connection_families) and isinstance(exc, connection_families)


async def _fetch_probe_rows(dsn: str, query: str) -> list[dict[str, Any]]:
    """Connect to ``dsn`` via asyncpg, run ``query``, return rows as dicts.

    Isolated so unit tests inject rows without a live database; the asyncpg
    import is deferred so importing this module never requires the driver.
    """
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        records = await conn.fetch(query)
    finally:
        await conn.close()
    return [dict(record) for record in records]
