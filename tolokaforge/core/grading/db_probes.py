"""Substrate SQL probes against a task-declared postgres.

Each probe carries its own ``dsn`` and ``query`` and, at evaluation time,
opens the connection itself, runs the read-only query, shapes the result
into ``{"rows": [...], "row_count": N}``, and applies the probe's ``expect``
JSONPath assertions via
:func:`~tolokaforge.core.grading.jsonpath_evaluators.evaluate_jsonpath_state_checks`.

The DSN is scoped to the task's own postgres — reachable only inside the
task's docker network — not the trial's substrate DB. The runner container
joins that network and opens each probe's DSN directly rather than routing
through the substrate; the host-side core engine does not have that
reachability, which is why :mod:`~tolokaforge.core.grading.key_manifest`
marks the key ``RUNNER_ONLY``. A substrate-mediated route — a
``SubstrateService.RunDbProbe`` RPC — is tracked as issue #1350.

``asyncpg`` is imported lazily inside :func:`_fetch_probe_rows` so importing
this module does not require the driver. The runner Docker image installs
``asyncpg`` at build time; the canonical lock in
``tests/canonical/test_runner_image_db_driver_canon.py`` guards the
requirement.
"""

from __future__ import annotations

from typing import Any

from tolokaforge.core.grading.jsonpath_evaluators import evaluate_jsonpath_state_checks


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


async def evaluate_db_probes(probes: list[dict[str, Any]]) -> tuple[float, str]:
    """Evaluate substrate SQL probes against a task-declared postgres.

    For each probe, connect to its ``dsn``, run its read-only ``query``, shape
    the result into ``{"rows": [...], "row_count": N}``, and apply its ``expect``
    JSONPath assertions via ``evaluate_jsonpath_state_checks``.

    Two-level aggregation: a probe passes iff *every* ``expect`` assertion passes
    (its JSONPath score is 1.0); the component score is the fraction of passing
    probes. A connection/query failure is a FAILED probe with an actionable
    reason (fail loud — never a silent pass). Empty list → -1.0 sentinel,
    matching the file/state evaluators.
    """
    if not probes:
        return -1.0, ""

    passed = 0
    total = len(probes)
    reasons_parts: list[str] = []

    for probe in probes:
        name = probe.get("name", "")
        description = probe.get("description") or name
        expect = probe.get("expect", []) or []

        try:
            rows = await _fetch_probe_rows(probe.get("dsn", ""), probe.get("query", ""))
        except Exception as exc:
            reasons_parts.append(
                f"FAIL: probe {name!r} could not query postgres: "
                f"{type(exc).__name__}: {exc} — {description}"
            )
            continue

        state = {"rows": rows, "row_count": len(rows)}
        probe_score, probe_reasons = evaluate_jsonpath_state_checks(expect, state)
        if probe_score == 1.0:
            passed += 1
            reasons_parts.append(f"PASS: probe {name!r} — {probe_reasons}")
        else:
            reasons_parts.append(f"FAIL: probe {name!r} — {probe_reasons}")

    score = passed / total
    return score, "; ".join(reasons_parts)
