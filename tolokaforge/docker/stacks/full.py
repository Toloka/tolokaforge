"""Full service stack: Core + RAG service + Mock Web.

Extends the core stack with the two services that browser/mobile/search_kb
tasks need to reach (mock-web at port 8080, rag-service at 8001). All
``core_stack`` kwargs are accepted and forwarded so callers can switch
between ``core_stack`` and ``full_stack`` without losing
``enable_playwright``, ``task_pack_mounts``, etc.

Example:
    >>> from tolokaforge.docker.stacks.full import full_stack
    >>> stack = full_stack(enable_playwright=True)
    >>> stack.start_all(wait=True)
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from tolokaforge.docker.config import DockerConfig
from tolokaforge.docker.health import HealthProbe
from tolokaforge.docker.mount import Mount
from tolokaforge.docker.ports import PortConfig
from tolokaforge.docker.stack import ServiceDefinition, ServiceStack
from tolokaforge.docker.stacks.core import core_stack
from tolokaforge.docker.wheel_resolver import resolve_wheel


def full_stack(
    config: DockerConfig | None = None,
    db_port: int | Literal["auto"] = "auto",
    runner_port: int | Literal["auto"] = "auto",
    rag_port: int = 8001,
    mock_web_port: int = 8080,
    enable_dind: bool = False,
    enable_playwright: bool = False,
    task_pack_mounts: list[Path] | None = None,
    extra_runner_binds: list[tuple[Path, str]] | None = None,
    mount_docker_socket: bool = False,
) -> ServiceStack:
    """Create a full service stack with all services.

    Includes:
    - Core stack (db-service + runner) — every ``core_stack`` kwarg is
      accepted and forwarded.
    - RAG service (hybrid BM25 + FAISS search)
    - Mock Web service (for browser tasks)

    Args:
        config: Optional DockerConfig. Uses defaults if None.
        db_port: Host port for DB service (default: ``"auto"``).
        runner_port: Host port for Runner gRPC (default: ``"auto"``).
        rag_port: Host port for RAG service (default: 8001).
        mock_web_port: Host port for Mock Web service (default: 8080).
        enable_dind: Forwarded to ``core_stack``.
        enable_playwright: Forwarded to ``core_stack``.
        task_pack_mounts: Forwarded to ``core_stack``.
        extra_runner_binds: Forwarded to ``core_stack``.
        mount_docker_socket: Forwarded to ``core_stack``.

    Returns:
        ServiceStack configured with all services.
    """
    stack = core_stack(
        config=config,
        db_port=db_port,
        runner_port=runner_port,
        enable_dind=enable_dind,
        enable_playwright=enable_playwright,
        task_pack_mounts=task_pack_mounts,
        extra_runner_binds=extra_runner_binds,
        mount_docker_socket=mount_docker_socket,
        # The full stack provisions a rag-service below, so the runner's
        # RAG_SERVICE_URL points at a service that actually runs. Both the
        # container name and the ``rag-service`` alias resolve on runner-net;
        # keep the container-name form (changing it only churns tests).
        rag_service_url="http://tolokaforge-rag-service:8001",
    )

    # RAG Service — hybrid BM25 + FAISS search
    # Needs the tolokaforge wheel (for tolokaforge.secrets) + its own
    # service files (requirements.txt + app.py).
    artifact = resolve_wheel()
    rag_service = ServiceDefinition(
        name="rag-service",
        image_name="tolokaforge-rag-service",
        dockerfile="tolokaforge/docker/dockerfiles/rag.Dockerfile",
        context=".",
        context_files=[
            str(artifact.path),
            "tolokaforge/env/rag_service/",
        ],
        build_args={"WHEEL_FILENAME": artifact.path.name},
        ports=[PortConfig(container_port=8001, host_port=rag_port)],
        mounts=[Mount.volume("rag_data", "/env/rag")],
        environment={
            "PYTHONUNBUFFERED": "1",
            "CORPUS_PATH": "/env/rag/corpus",
        },
        health_probe=HealthProbe.http(
            url=f"http://localhost:{rag_port}/health",
            # rag-service warmup loads sentence-transformers + tokenizer
            # (downloads on first run, then cached). 30s is too short for a
            # cold start; 120s comfortably covers the cold-cache case while
            # the warm-cache case still resolves within a few seconds.
            timeout_s=120.0,
            interval_s=1.0,
        ),
        networks=["runner-net"],
        profiles=["rag"],
        network_aliases=["rag-service"],
    )

    # Mock Web Service — for browser tasks
    mock_web_service = ServiceDefinition(
        name="mock-web",
        image_name="tolokaforge-mock-web",
        dockerfile="tolokaforge/docker/dockerfiles/mock_web.Dockerfile",
        context=".",
        # Narrow build-context-hash to what the Dockerfile actually COPYs.
        # Mock-web only bundles its own service code; without this the
        # orchestrator hashes the whole repo and rebuilds on every
        # unrelated edit. Mirrors the pattern db-service / runner already
        # use in core_stack.
        context_files=[
            "tolokaforge/env/mock_web_service/",
        ],
        ports=[PortConfig(container_port=8080, host_port=mock_web_port)],
        environment={
            "PYTHONUNBUFFERED": "1",
            "JSON_DB_URL": "http://tolokaforge-db-service:8000",
        },
        depends_on=["db-service"],
        networks=["runner-net"],
        profiles=["web"],
        network_aliases=["mock-web"],
    )

    stack.add_services([rag_service, mock_web_service])
    return stack
