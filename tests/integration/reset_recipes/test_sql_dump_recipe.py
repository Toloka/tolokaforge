"""Real-container test for the ``sql_dump`` reset recipe.

Boots a ``postgres:16-alpine`` service via ``testcontainers.compose``,
seeds it from a ``.sql`` fixture, mutates a row, then dispatches
:class:`~tolokaforge.runtime.reset_recipes.sql_dump.SqlDumpDispatcher`
and asserts the mutation is gone.
"""

from __future__ import annotations

import hashlib
import subprocess
import textwrap
from pathlib import Path

import pytest
from testcontainers.compose import DockerCompose

from tolokaforge.core.models import SeedRef
from tolokaforge.runtime.reset_recipes import RECIPE_REGISTRY

pytestmark = [pytest.mark.integration, pytest.mark.docker]


COMPOSE = textwrap.dedent(
    """
    services:
      postgres:
        image: "postgres:16-alpine"
        environment:
          POSTGRES_USER: "postgres"
          POSTGRES_PASSWORD: "example"
          POSTGRES_DB: "app"
        healthcheck:
          test: ["CMD-SHELL", "pg_isready -U postgres -d app"]
          interval: 2s
          timeout: 3s
          retries: 30
    """
).strip()


BASELINE_SQL = textwrap.dedent(
    """
    DROP TABLE IF EXISTS widgets;
    CREATE TABLE widgets (id INT PRIMARY KEY, name TEXT);
    INSERT INTO widgets (id, name) VALUES (1, 'baseline');
    """
).strip()


def _psql(compose: DockerCompose, sql: str) -> str:
    """Run ``sql`` inside the postgres container via ``psql`` and
    return stdout stripped."""
    cmd = [
        *compose.docker_compose_command(),
        "exec",
        "-T",
        "postgres",
        "psql",
        "-t",
        "-A",
        "-U",
        "postgres",
        "-d",
        "app",
        "-c",
        sql,
    ]
    result = subprocess.run(cmd, capture_output=True, check=True, text=True, cwd=compose.context)
    return result.stdout.strip()


def test_sql_dump_recipe_restores_baseline(tmp_path: Path) -> None:
    """Mutate a row, apply the recipe, verify the baseline row is back."""
    seed_file = tmp_path / "seed.sql"
    seed_file.write_text(BASELINE_SQL + "\n")
    seed = SeedRef(
        path=seed_file,
        kind="sql_dump",
        digest="sha256:" + hashlib.sha256(seed_file.read_bytes()).hexdigest(),
    )

    compose_dir = tmp_path / "compose"
    compose_dir.mkdir()
    (compose_dir / "docker-compose.yml").write_text(COMPOSE + "\n")

    with DockerCompose(
        context=str(compose_dir),
        compose_file_name="docker-compose.yml",
        pull=True,
        build=False,
        wait=True,
    ) as compose:
        _psql(compose, "SELECT 1;")
        # Prime the baseline before the mutation to establish a
        # deterministic starting state.
        RECIPE_REGISTRY["sql_dump"].apply(seed, "postgres", compose)
        assert _psql(compose, "SELECT name FROM widgets WHERE id = 1;") == "baseline"

        _psql(compose, "UPDATE widgets SET name = 'mutated' WHERE id = 1;")
        assert _psql(compose, "SELECT name FROM widgets WHERE id = 1;") == "mutated"

        RECIPE_REGISTRY["sql_dump"].apply(seed, "postgres", compose)
        assert _psql(compose, "SELECT name FROM widgets WHERE id = 1;") == "baseline"
