"""Resolve Harbor T_BENCH_* environment variables for docker-compose."""

from __future__ import annotations

import base64

from tolokaforge_adapter_terminal_bench.task_parser import TerminalBenchTask


def resolve_tbench_env_vars(
    meta: TerminalBenchTask,
    image_registry: str | None = None,
    logs_host_root: str = "/workspace",
) -> dict[str, str]:
    """Build env-var dict that docker-compose.yaml expects.

    Harbor injects ``T_BENCH_*`` variables into docker-compose.  We replicate
    the same mapping so the compose files work unchanged.

    ``logs_host_root`` is the directory on the Docker daemon's filesystem
    where per-task log bind-mounts are created. Defaults to ``/workspace``
    (DinD-compatible). For host-socket passthrough, pass a path that is
    visible both in the Runner container and on the host daemon (e.g.
    ``/tmp/tolokaforge-tbench-logs``).
    """
    if image_registry:
        image_name = f"{image_registry}/{meta.task_id}:latest"
    else:
        image_name = f"tbench_{meta.task_id}"

    # The wrapper overrides container_name with the trial-specific project_name.
    return {
        "T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME": image_name,
        "T_BENCH_TASK_DOCKER_CLIENT_CONTAINER_NAME": f"tbench_{meta.task_id}_main",
        "T_BENCH_CONTAINER_LOGS_PATH": "/logs",
        "T_BENCH_TASK_LOGS_PATH": f"{logs_host_root}/logs/{meta.task_id}",
        "T_BENCH_CONTAINER_AGENT_LOGS_PATH": "/logs/agent",
        "T_BENCH_TASK_AGENT_LOGS_PATH": f"{logs_host_root}/agent_logs/{meta.task_id}",
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
