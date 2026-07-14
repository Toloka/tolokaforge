"""Real-container test for the ``filesystem_dir`` reset recipe.

Boots an ``alpine`` service with a mounted ``/workspace`` directory,
seeds it from a host fixture tree, mutates a file, then dispatches
:class:`~tolokaforge.runtime.reset_recipes.filesystem_dir.FilesystemDirDispatcher`
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
      workspace:
        image: "alpine:3.20"
        command: ["sh", "-c", "mkdir -p /workspace && sleep 3600"]
        healthcheck:
          test: ["CMD", "test", "-d", "/workspace"]
          interval: 2s
          timeout: 3s
          retries: 30
    """
).strip()


def _cat(compose: DockerCompose, path: str) -> str:
    """Return the contents of ``path`` inside the workspace container."""
    cmd = [
        *compose.docker_compose_command(),
        "exec",
        "-T",
        "workspace",
        "cat",
        path,
    ]
    result = subprocess.run(cmd, capture_output=True, check=True, text=True)
    return result.stdout


def _write(compose: DockerCompose, path: str, contents: str) -> None:
    """Overwrite ``path`` inside the workspace container with
    ``contents``."""
    cmd = [
        *compose.docker_compose_command(),
        "exec",
        "-T",
        "workspace",
        "sh",
        "-c",
        f"printf %s '{contents}' > {path}",
    ]
    subprocess.run(cmd, capture_output=True, check=True)


def _seed_ref_from_tree(tree: Path) -> SeedRef:
    """Build a :class:`SeedRef` for a directory tree; the digest hashes
    the sorted concatenation of every file's bytes so a tree edit
    reliably flips the digest."""
    hasher = hashlib.sha256()
    for entry in sorted(tree.rglob("*")):
        if entry.is_file():
            hasher.update(entry.read_bytes())
    return SeedRef(
        path=tree,
        kind="filesystem_dir",
        digest=f"sha256:{hasher.hexdigest()}",
    )


def test_filesystem_dir_recipe_restores_baseline(tmp_path: Path) -> None:
    seed_tree = tmp_path / "seed_tree"
    seed_tree.mkdir()
    (seed_tree / "hello.txt").write_text("baseline")

    compose_dir = tmp_path / "compose"
    compose_dir.mkdir()
    (compose_dir / "docker-compose.yml").write_text(COMPOSE + "\n")

    seed = _seed_ref_from_tree(seed_tree)

    with DockerCompose(
        context=str(compose_dir),
        compose_file_name="docker-compose.yml",
        pull=True,
        build=False,
        wait=True,
    ) as compose:
        RECIPE_REGISTRY["filesystem_dir"].apply(seed, "workspace", compose)
        assert _cat(compose, "/workspace/hello.txt") == "baseline"

        _write(compose, "/workspace/hello.txt", "mutated")
        assert _cat(compose, "/workspace/hello.txt") == "mutated"

        RECIPE_REGISTRY["filesystem_dir"].apply(seed, "workspace", compose)
        assert _cat(compose, "/workspace/hello.txt") == "baseline"
