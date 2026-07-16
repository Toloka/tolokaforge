"""Real-container test for the ``bare`` reset recipe.

``bare`` is intentionally a no-op — the seed sits on the host at the
resolved path and the task's compose file consumes it verbatim. This
test asserts the dispatcher touches nothing (no docker exec, no
docker cp) even against a running compose stack.
"""

from __future__ import annotations

import hashlib
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
from testcontainers.compose import DockerCompose

from tolokaforge.core.models import SeedRef
from tolokaforge.runtime.reset_recipes import RECIPE_REGISTRY

pytestmark = [pytest.mark.integration, pytest.mark.docker]


COMPOSE = textwrap.dedent(
    """
    services:
      canary:
        image: "alpine:3.20"
        command: ["sleep", "3600"]
        healthcheck:
          test: ["CMD", "true"]
          interval: 2s
          timeout: 3s
          retries: 30
    """
).strip()


def test_bare_recipe_is_a_noop_against_a_running_stack(tmp_path: Path) -> None:
    seed_file = tmp_path / "bare_seed.bin"
    seed_file.write_bytes(b"raw bytes")
    seed = SeedRef(
        path=seed_file,
        kind="bare",
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
        # No container-side action allowed; if the dispatcher ever
        # started shelling out we'd see the patched Popen fire.
        with patch.object(subprocess, "run") as run_mock:
            RECIPE_REGISTRY["bare"].apply(seed, "canary", compose)
        assert run_mock.call_count == 0
        assert seed_file.read_bytes() == b"raw bytes"
