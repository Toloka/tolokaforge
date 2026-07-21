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

from pathlib import Path
from typing import Literal

from tolokaforge.docker.config import DockerConfig
from tolokaforge.docker.health import HealthProbe
from tolokaforge.docker.mount import Mount
from tolokaforge.docker.policy import Capability, ResourcePolicy
from tolokaforge.docker.ports import PortConfig
from tolokaforge.docker.stack import EngineStack, ServiceDefinition
from tolokaforge.docker.wheel_resolver import resolve_wheel


def core_stack(
    config: DockerConfig | None = None,
    db_port: int | Literal["auto"] = "auto",
    runner_port: int | Literal["auto"] = "auto",
    enable_dind: bool = False,
    enable_playwright: bool = False,
    enable_docker_cli: bool = False,
    task_pack_mounts: list[Path] | None = None,
    extra_runner_binds: list[tuple[Path, str]] | None = None,
    mount_docker_socket: bool = False,
    rag_service_url: str | None = None,
) -> EngineStack:
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
        enable_docker_cli: Install the docker CLI + compose plugin in the
            Runner image so terminal-bench tasks can shell out to the host
            Docker daemon via the mounted socket. Detected automatically from
            the configured adapter type; the default image ships without it.
        task_pack_mounts: Host directories to bind-mount into the Runner at
            the same absolute path. Used by the ``terminal_bench`` adapter so
            the host Docker daemon (reached via a mounted socket) and the
            Runner both resolve task files from an identical path.
        extra_runner_binds: Additional ``(host_path, container_path)`` bind
            mounts for the Runner (e.g. a shared log directory).
        mount_docker_socket: Bind-mount ``/var/run/docker.sock`` into the
            Runner so ``docker compose`` inside the Runner can talk to the
            host Docker daemon. Relaxes the default cap-drop policy because
            Docker socket access needs additional capabilities.
        rag_service_url: URL of the RAG service to inject into the runner
            container as ``RAG_SERVICE_URL``. ``None`` (the default) leaves it
            unset — the core stack has no rag-service, so the runner builds no
            RAG client and the judge is offered no unreachable ``search_kb``.
            Only ``full_stack`` (which actually starts a rag-service) passes a
            value, keeping "env present" == "rag-service running".

    Returns:
        EngineStack configured with db-service and runner.
    """
    stack = EngineStack(config=config or DockerConfig())

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
        dockerfile="tolokaforge/docker/dockerfiles/db_service.Dockerfile",
        context=".",
        context_files=[
            "tolokaforge/env/json_db_service/",
        ],
        ports=[PortConfig(container_port=8000, host_port=db_port)],
        environment={"PYTHONUNBUFFERED": "1"},
        health_probe=db_health,
        networks=["runner-net"],
        # Container is named ``tolokaforge-db-service``; expose the short
        # ``db-service`` and ``json-db`` aliases so task YAMLs and tools
        # that hardcode either form resolve correctly via Docker DNS.
        network_aliases=["db-service", "json-db"],
    )

    # Runner — gRPC tool execution + grading
    runner_mounts: list[Mount] = []
    runner_env = {
        "PYTHONUNBUFFERED": "1",
        "DB_SERVICE_URL": "http://tolokaforge-db-service:8000",
    }
    # Inject RAG_SERVICE_URL only when a rag-service is actually provisioned
    # (full stack). Absent on the core stack so the runner builds no RAG
    # client and the judge is not offered an unreachable search_kb tool.
    if rag_service_url is not None:
        runner_env["RAG_SERVICE_URL"] = rag_service_url
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

    if task_pack_mounts:
        for root in task_pack_mounts:
            abs_root = str(Path(root).resolve())
            runner_mounts.append(Mount.bind(abs_root, abs_root, read_only=False))

    if extra_runner_binds:
        for host_path, container_path in extra_runner_binds:
            runner_mounts.append(Mount.bind(str(Path(host_path).resolve()), container_path))

    if mount_docker_socket:
        runner_mounts.append(Mount.bind("/var/run/docker.sock", "/var/run/docker.sock"))
        # Docker socket access requires a looser capability profile.
        runner_resources = ResourcePolicy()

    # Serialize host-side secrets into a single env var for the runner
    # container. The runner reads this on startup via __main__.py and
    # bootstraps its own SecretManager singleton from it. This is the
    # *only* place credentials cross the host→container boundary —
    # never via build args, mounts, or image bake-in.
    import json

    from tolokaforge.secrets import get_default

    secrets_payload = get_default().serialize()
    if secrets_payload:
        runner_env["TOLOKAFORGE_SECRETS_JSON"] = json.dumps(secrets_payload)

    # Resolve the tolokaforge wheel for the runner image.
    # The wheel is a local file — Docker never needs to reach the network.
    artifact = resolve_wheel()

    runner_build_args: dict[str, str] = {
        "WHEEL_FILENAME": artifact.path.name,
    }
    if enable_playwright:
        runner_build_args["INSTALL_PLAYWRIGHT"] = "true"
    if enable_docker_cli:
        runner_build_args["INSTALL_DOCKER_CLI"] = "true"

    runner = ServiceDefinition(
        name="runner",
        image_name="tolokaforge-runner",
        dockerfile="tolokaforge/docker/dockerfiles/runner.Dockerfile",
        context=".",
        context_files=[
            str(artifact.path),  # absolute path to the .whl
        ],
        ports=[PortConfig(container_port=50051, host_port=runner_port)],
        environment=runner_env,
        depends_on=runner_depends,
        mounts=runner_mounts,
        resources=runner_resources,
        networks=["runner-net"],
        build_args=runner_build_args,
        network_aliases=["runner"],
    )
    services.append(runner)

    stack.add_services(services)
    return stack
