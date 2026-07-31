"""Real-container test for the ``redis_dump`` reset recipe.

Boots a ``redis:7-alpine`` service, seeds it from an RDB snapshot the
test itself generates, mutates a key, then dispatches
:class:`~tolokaforge.runtime.reset_recipes.redis_dump.RedisDumpDispatcher`
and asserts the mutation is gone.
"""

from __future__ import annotations

import hashlib
import subprocess
import textwrap
import time
from pathlib import Path

import pytest
from testcontainers.compose import DockerCompose

from tolokaforge.core.models import SeedRef
from tolokaforge.runtime.reset_recipes import RECIPE_REGISTRY

pytestmark = [pytest.mark.integration, pytest.mark.docker]


COMPOSE = textwrap.dedent("""
    services:
      redis:
        image: "redis:7-alpine"
        command: ["redis-server", "--save", "", "--appendonly", "no"]
        healthcheck:
          test: ["CMD", "redis-cli", "PING"]
          interval: 2s
          timeout: 3s
          retries: 30
    """).strip()


def _redis_cli(compose: DockerCompose, *args: str) -> str:
    """Run ``redis-cli`` inside the redis container and return stdout
    stripped."""
    cmd = [
        *compose.docker_compose_command(),
        "exec",
        "-T",
        "redis",
        "redis-cli",
        *args,
    ]
    result = subprocess.run(cmd, capture_output=True, check=True, text=True, cwd=compose.context)
    return result.stdout.strip()


def _generate_baseline_rdb(tmp_path: Path) -> Path:
    """Boot a throwaway redis, load a baseline key, ``SAVE``, and copy
    the resulting ``dump.rdb`` out to *tmp_path* — that snapshot is
    what the recipe restores from in the real test."""
    seed_dir = tmp_path / "baseline_stack"
    seed_dir.mkdir()
    (seed_dir / "docker-compose.yml").write_text(COMPOSE + "\n")
    with DockerCompose(
        context=str(seed_dir),
        compose_file_name="docker-compose.yml",
        pull=True,
        build=False,
        wait=True,
    ) as compose:
        # Redis needs a moment past PING to accept SET/SAVE reliably.
        time.sleep(1)
        _redis_cli(compose, "SET", "greeting", "baseline")
        _redis_cli(compose, "SAVE")
        rdb_out = tmp_path / "dump.rdb"
        subprocess.run(
            [
                *compose.docker_compose_command(),
                "cp",
                "redis:/data/dump.rdb",
                str(rdb_out),
            ],
            check=True,
            capture_output=True,
            cwd=compose.context,
        )
    return rdb_out


def test_redis_dump_recipe_restores_baseline(tmp_path: Path) -> None:
    rdb = _generate_baseline_rdb(tmp_path)
    seed = SeedRef(
        path=rdb,
        kind="redis_dump",
        digest="sha256:" + hashlib.sha256(rdb.read_bytes()).hexdigest(),
    )

    live_dir = tmp_path / "live_stack"
    live_dir.mkdir()
    (live_dir / "docker-compose.yml").write_text(COMPOSE + "\n")

    with DockerCompose(
        context=str(live_dir),
        compose_file_name="docker-compose.yml",
        pull=True,
        build=False,
        wait=True,
    ) as compose:
        # Fresh container has no baseline key.
        assert _redis_cli(compose, "GET", "greeting") == ""

        RECIPE_REGISTRY["redis_dump"].apply(seed, "redis", compose)
        assert _redis_cli(compose, "GET", "greeting") == "baseline"

        _redis_cli(compose, "SET", "greeting", "mutated")
        assert _redis_cli(compose, "GET", "greeting") == "mutated"

        RECIPE_REGISTRY["redis_dump"].apply(seed, "redis", compose)
        assert _redis_cli(compose, "GET", "greeting") == "baseline"
