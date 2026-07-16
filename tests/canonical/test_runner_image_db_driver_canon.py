"""Driver-availability guard for the state_checks.db_probes primitive.

``evaluate_db_probes`` connects to postgres via ``asyncpg`` at grade time, so
the runner image MUST ship that driver. Unit tests inject rows and never import
asyncpg, and the end-to-end integration test auto-skips without Docker — so a
Dockerfile edit that drops asyncpg would otherwise fail silently at grade time.
This locks the harness↔image coupling here, independent of Docker running.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.canonical

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER_DOCKERFILE = REPO_ROOT / "tolokaforge" / "docker" / "dockerfiles" / "runner.Dockerfile"


def test_runner_image_installs_asyncpg():
    text = RUNNER_DOCKERFILE.read_text()
    assert "asyncpg" in text, (
        "runner.Dockerfile must install asyncpg — evaluate_db_probes "
        "(state_checks.db_probes) connects to postgres via asyncpg at grade time"
    )
