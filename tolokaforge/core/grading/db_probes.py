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
