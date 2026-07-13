"""Resolve Harbor T_BENCH_* environment variables for docker-compose."""

from __future__ import annotations

from tolokaforge.adapters.compose import bundle_compose_artifacts
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
    return bundle_compose_artifacts(
        meta.task_dir,
        compose_file="docker-compose.yaml",
        tests_dir="tests",
        include_compose_context=False,
    )
