"""Core service stack: DB service + Runner.

This is the minimum stack needed for integration tests and local development.
Maps from the docker-compose.yaml db-service and runner definitions.

Example:
    >>> from tolokaforge.docker.stacks.core import core_stack
    >>> stack = core_stack()
    >>> stack.start_all(wait=True)
    >>> url = stack.get_service_url("db-service", 8000)
"""

from __future__ import annotations

from typing import Literal

from tolokaforge.docker.config import DockerConfig
from tolokaforge.docker.health import HealthProbe
from tolokaforge.docker.mount import Mount
from tolokaforge.docker.policy import Capability, ResourcePolicy
from tolokaforge.docker.ports import PortConfig
from tolokaforge.docker.stack import ServiceDefinition, ServiceStack


def core_stack(
    config: DockerConfig | None = None,
    db_port: int | Literal["auto"] = "auto",
    runner_port: int | Literal["auto"] = "auto",
    enable_dind: bool = False,
    enable_playwright: bool = False,
) -> ServiceStack:
    """Create a core service stack with DB service and Runner.

    Args:
        config: Optional DockerConfig. Uses defaults if None.
        db_port: Host port for DB service (default: ``"auto"``).
            When ``"auto"``, a free port is allocated at container start.
        runner_port: Host port for Runner gRPC (default: ``"auto"``).
            When ``"auto"``, a free port is allocated at container start.
        enable_dind: Add a Docker-in-Docker sidecar so the Runner can
            manage Docker Compose stacks for terminal-bench tasks.
            Runner connects via ``DOCKER_HOST=tcp://dind:2375``.
        enable_playwright: Install Playwright + Chromium in the Runner
            image for browser tool support. Detected automatically from tasks.

    Returns:
        ServiceStack configured with db-service and runner.
    """
    stack = ServiceStack(config=config or DockerConfig())

    # Build health probe only when the host port is known upfront.
    # When auto-allocated, _start_service will construct the probe
    # after port resolution.
    db_health: HealthProbe | None = None
    if isinstance(db_port, int):
        db_health = HealthProbe.http(
            url=f"http://localhost:{db_port}/health",
            timeout_s=30.0,
            interval_s=1.0,
        )

    # DB Service — state storage with trial isolation
    db_service = ServiceDefinition(
        name="db-service",
        image_name="tolokaforge-db-service",
        dockerfile="docker/db_service.Dockerfile",
        context=".",
        context_files=[
            "tolokaforge/env/json_db_service/",
        ],
        ports=[PortConfig(container_port=8000, host_port=db_port)],
        environment={"PYTHONUNBUFFERED": "1"},
        health_probe=db_health,
        networks=["runner-net"],
    )

    # Runner — gRPC tool execution + grading
    runner_mounts: list[Mount] = []
    runner_env = {
        "PYTHONUNBUFFERED": "1",
        "DB_SERVICE_URL": "http://tolokaforge-db-service:8000",
        "RAG_SERVICE_URL": "http://tolokaforge-rag-service:8001",
    }
    runner_depends = ["db-service"]
    runner_resources = ResourcePolicy(
        cap_drop=[Capability.ALL],
        cap_add=[Capability.NET_BIND_SERVICE],
    )

    services: list[ServiceDefinition] = [db_service]

    if enable_dind:
        # Docker-in-Docker sidecar — runs dockerd for terminal-bench tasks.
        # Uses non-TLS (DOCKER_TLS_CERTDIR="") on the internal runner-net.
        # Runner and DinD share a named volume at /workspace for task files
        # and compose bind-mount paths (logs, etc.).
        dind = ServiceDefinition(
            name="dind",
            image_name="docker",
            use_prebuilt_image=True,
            prebuilt_tag="dind",
            privileged=True,
            command=["dockerd", "--host=tcp://0.0.0.0:2375", "--tls=false"],
            environment={
                "DOCKER_TLS_CERTDIR": "",
            },
            mounts=[
                Mount.volume("tbench-workspace", "/workspace"),
            ],
            networks=["runner-net"],
        )
        services.append(dind)

        # Runner talks to DinD daemon, shares workspace volume
        runner_env["DOCKER_HOST"] = "tcp://tolokaforge-dind:2375"
        runner_mounts.append(Mount.volume("tbench-workspace", "/workspace"))
        runner_depends.append("dind")
        runner_resources = ResourcePolicy()  # relaxed

    runner_build_args: dict[str, str] = {}
    if enable_playwright:
        runner_build_args["INSTALL_PLAYWRIGHT"] = "true"

    runner = ServiceDefinition(
        name="runner",
        image_name="tolokaforge-runner",
        dockerfile="docker/runner.Dockerfile",
        context=".",
        context_files=[
            "pyproject.toml",
            "README.md",
            "tolokaforge/",
        ],
        ports=[PortConfig(container_port=50051, host_port=runner_port)],
        environment=runner_env,
        depends_on=runner_depends,
        mounts=runner_mounts,
        resources=runner_resources,
        networks=["runner-net"],
        build_args=runner_build_args,
    )
    services.append(runner)

    stack.add_services(services)
    return stack
