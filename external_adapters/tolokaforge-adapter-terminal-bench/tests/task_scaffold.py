"""A minimal Terminal-Bench task folder for the adapter's compose-synthesis tests."""

from __future__ import annotations

from pathlib import Path

DEFAULT_ENVIRONMENT = "cpus = 2\nmemory_mb = 4096\n"


def write_task(
    base: Path,
    task_id: str = "task",
    *,
    separate_verifier: bool,
    environment: str = DEFAULT_ENVIRONMENT,
    agent_compose: str | None = None,
) -> Path:
    """Write ``base/task_id`` and return it.

    ``separate_verifier`` gives the task a top-level ``artifacts`` list and a
    ``tests/`` folder with a Dockerfile, the shape that grades in a verifier
    container of its own; without it the task is the single-container shape.
    ``environment`` is the body of ``[environment]``, and ``agent_compose`` the
    task's own ``environment/docker-compose.yaml`` when it ships one.
    """
    task_dir = base / task_id
    (task_dir / "environment").mkdir(parents=True)
    (task_dir / "environment" / "Dockerfile").write_text("FROM python:3.12-slim\n")
    if agent_compose is not None:
        (task_dir / "environment" / "docker-compose.yaml").write_text(agent_compose)
    (task_dir / "instruction.md").write_text("Do the thing.\n")
    toml = (
        '[metadata]\ndifficulty = "medium"\ntags = ["demo"]\n\n'
        "[agent]\ntimeout_sec = 1800.0\n\n"
        "[verifier]\ntimeout_sec = 300.0\n\n"
        f"[environment]\n{environment}"
    )
    if separate_verifier:
        toml = 'artifacts = ["/app"]\n' + toml
        tests_dir = task_dir / "tests"
        tests_dir.mkdir()
        (tests_dir / "Dockerfile").write_text('FROM python:3.12-slim\nCMD ["sleep", "infinity"]\n')
        (tests_dir / "test.sh").write_text("#!/bin/sh\necho 1 > /logs/verifier/reward.txt\n")
    (task_dir / "task.toml").write_text(toml)
    return task_dir
