"""Resolve Harbor T_BENCH_* environment variables for docker-compose."""

from __future__ import annotations

import base64
from pathlib import Path

from tolokaforge_adapter_terminal_bench.task_parser import TerminalBenchTask


def resolve_tbench_env_vars(
    meta: TerminalBenchTask,
    image_registry: str | None = None,
) -> dict[str, str]:
    """Build env-var dict that docker-compose.yaml expects.

    Harbor injects ``T_BENCH_*`` variables into docker-compose.  We replicate
    the same mapping so the compose files work unchanged.
    """
    if image_registry:
        image_name = f"{image_registry}/{meta.task_id}:latest"
    else:
        image_name = f"tbench_{meta.task_id}"

    # Log paths use /workspace/ so they resolve inside DinD's filesystem
    # (bind mounts in docker-compose.yaml are relative to the Docker daemon).
    # The wrapper overrides container_name with the trial-specific project_name.
    return {
        "T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME": image_name,
        "T_BENCH_TASK_DOCKER_CLIENT_CONTAINER_NAME": f"tbench_{meta.task_id}_main",
        "T_BENCH_CONTAINER_LOGS_PATH": "/logs",
        "T_BENCH_TASK_LOGS_PATH": f"/workspace/logs/{meta.task_id}",
        "T_BENCH_CONTAINER_AGENT_LOGS_PATH": "/logs/agent",
        "T_BENCH_TASK_AGENT_LOGS_PATH": f"/workspace/agent_logs/{meta.task_id}",
        "T_BENCH_TEST_DIR": "/tests",
        "CPUS": str(meta.cpus),
        "MEMORY": f"{meta.memory_mb}M",
    }


def bundle_task_artifacts(meta: TerminalBenchTask) -> dict[str, str]:
    """Bundle compose file + tests/ as base64-encoded artifacts dict.

    Used for cluster deployment (Strategy A) where task files are transmitted
    inside TaskDescription instead of being bind-mounted.
    """
    artifacts: dict[str, str] = {}
    task_dir = meta.task_dir

    # docker-compose.yaml
    compose = task_dir / "docker-compose.yaml"
    if compose.exists():
        artifacts["docker-compose.yaml"] = base64.b64encode(compose.read_bytes()).decode()

    # tests/ directory
    tests_dir = task_dir / "tests"
    if tests_dir.is_dir():
        for path in sorted(tests_dir.rglob("*")):
            if path.is_file():
                rel = path.relative_to(task_dir)
                artifacts[str(rel)] = base64.b64encode(path.read_bytes()).decode()

    return artifacts
