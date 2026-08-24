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

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tolokaforge.core.models.docker_config import DockerConfig
from tolokaforge.docker.builder import get_image_definition
from tolokaforge.docker.health import HealthProbe
from tolokaforge.docker.mount import Mount
from tolokaforge.docker.policy import Capability, ResourcePolicy
from tolokaforge.docker.ports import PortConfig
from tolokaforge.docker.stack import EngineStack, ServiceDefinition
from tolokaforge.secrets import container_secrets_env


@dataclass(frozen=True)
class TypeSenseAddress:
    """Where the runner container reaches the run's TypeSense server.

    Host and port stay separate because the consumer takes them separately
    (``initialize_typesense_for_domain(host=…, port=…)``). Both halves travel
    together so a stack can never be given one without the other.
    """

    host: str
    port: int


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
    typesense_address: TypeSenseAddress | None = None,
    expose_substrate: bool = False,
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
        typesense_address: In-network address of the run's TypeSense server,
            injected into the runner container as ``TYPESENSE_HOST`` /
            ``TYPESENSE_PORT``. ``None`` (the default) leaves both unset —
            the run configured no TypeSense plane, and the runner falls back
            to whatever connection details its task descriptions carry. The
            API key is not a parameter: it reaches the runner only inside
            ``TOLOKAFORGE_SECRETS_JSON``, by being registered with the
            SecretManager before the stack is built.
        expose_substrate: When true, injects
            ``RUNNER_EXPOSE_SUBSTRATE=true`` so the runner registers its
            :class:`SubstrateService` gRPC servicer on the same listen port
            as :class:`RunnerService`. ``False`` (the default) leaves the
            env var unset — the runner starts with the substrate surface
            off and every ``SubstrateService/*`` call returns
            ``UNIMPLEMENTED``, matching the honest-absence rule
            ``RAG_SERVICE_URL`` uses.

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
        published_image_repo="tolokasoft1/tolokaforge-db-service",
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
    # Same honest-absence rule: present iff the run configured a TypeSense
    # plane, so "variable present" == "a plane was configured".
    if typesense_address is not None:
        runner_env["TYPESENSE_HOST"] = typesense_address.host
        runner_env["TYPESENSE_PORT"] = str(typesense_address.port)
    # Same honest-absence rule: injected iff the run asked to expose the
    # read-only SubstrateService surface. Runner reads this env var in
    # ``get_config()`` and registers the servicer on the same listen port.
    if expose_substrate:
        runner_env["RUNNER_EXPOSE_SUBSTRATE"] = "true"
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
    # bootstraps its own SecretManager singleton from it. Credentials cross
    # the host→container boundary only inside this payload — never via build
    # args, mounts, or image bake-in.
    runner_env.update(container_secrets_env())

    # The runner Dockerfile is a multi-stage build: its wheel-builder stage
    # runs ``hatch build --target custom`` against the source tree to produce
    # the runner-subset wheel; the runtime stage installs it. The build
    # context ships the sources hatch needs — the subset wheel is a
    # Docker-only artifact (ADR-0025), never a host-side input.
    runner_build_args: dict[str, str] = {}
    if enable_playwright:
        runner_build_args["INSTALL_PLAYWRIGHT"] = "true"
    if enable_docker_cli:
        runner_build_args["INSTALL_DOCKER_CLI"] = "true"

    runner = ServiceDefinition(
        name="runner",
        image_name="tolokaforge-runner",
        published_image_repo="tolokasoft1/tolokaforge-runner",
        dockerfile="tolokaforge/docker/dockerfiles/runner.Dockerfile",
        context=".",
        # Sources ``hatch build --target custom`` consumes in the wheel-builder
        # stage. Resolved by the builder rather than spelled out here: on a
        # wheel install the repo-root paths do not exist (``repo_root()`` is
        # ``site-packages``) and the factory swaps in the packaged copies. This
        # list used to be duplicated here, which is how v0.14.0/v0.14.1 kept
        # failing on an installed engine even after the builder was fixed —
        # this is the code path the orchestrator's service stack actually takes.
        context_files=get_image_definition("runner")["context_files"],
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
