"""Driver-availability guard for the state_checks.db_probes primitive.

:func:`~tolokaforge.core.grading.db_probes._fetch_probe_rows` connects to
postgres via ``asyncpg`` at grade time, so the runner image MUST ship that
driver. Unit tests inject rows and never import asyncpg, and the end-to-end
integration test auto-skips without Docker — so dropping a domain driver
would otherwise fail silently at grade time. The lazy import
(``core/grading/db_probes.py`` imports ``asyncpg`` only inside the helper)
also means the import-boundary subset test cannot see it. This canonical
test is the ship-lock: it asserts every domain driver is declared in the
runner image's dependency SSOT — ``[project.optional-dependencies].runner``
— so a driver can never silently leave the image.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import toml

pytestmark = pytest.mark.canonical

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"

DOMAIN_DRIVERS = (
    "asyncpg",
    "psycopg2-binary",
    "alembic",
    "python-jose",
    "sqlalchemy",
    "odata-query",
)


def _runner_extra_distributions() -> set[str]:
    pyproject = toml.loads(PYPROJECT.read_text())
    specs = pyproject["project"]["optional-dependencies"]["runner"]
    return {spec.split(">=")[0].split("==")[0].split("[")[0].strip().lower() for spec in specs}


def test_runner_extra_declares_every_domain_driver() -> None:
    declared = _runner_extra_distributions()
    missing = [driver for driver in DOMAIN_DRIVERS if driver not in declared]
    assert not missing, (
        "[project.optional-dependencies].runner must declare every domain driver "
        "(_fetch_probe_rows and domain tool code need them at grade time); "
        f"missing: {missing}"
    )
