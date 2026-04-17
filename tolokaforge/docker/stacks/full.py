"""Full service stack: Core + RAG service + Mock Web.

Extends the core stack with optional services for full functionality.

Example:
    >>> from tolokaforge.docker.stacks.full import full_stack
    >>> stack = full_stack()
    >>> stack.start_all(wait=True)
"""

from __future__ import annotations

from tolokaforge.docker.config import DockerConfig
from tolokaforge.docker.health import HealthProbe
from tolokaforge.docker.mount import Mount
from tolokaforge.docker.ports import PortConfig
from tolokaforge.docker.stack import ServiceDefinition, ServiceStack
from tolokaforge.docker.stacks.core import core_stack


def full_stack(
    config: DockerConfig | None = None,
    db_port: int = 8000,
    runner_port: int = 50051,
    rag_port: int = 8001,
    mock_web_port: int = 8080,
) -> ServiceStack:
    """Create a full service stack with all services.

    Includes:
    - Core stack (db-service + runner)
    - RAG service (hybrid BM25 + FAISS search)
    - Mock Web service (for browser tasks)

    Args:
        config: Optional DockerConfig. Uses defaults if None.
        db_port: Host port for DB service (default: 8000).
        runner_port: Host port for Runner gRPC (default: 50051).
        rag_port: Host port for RAG service (default: 8001).
        mock_web_port: Host port for Mock Web service (default: 8080).

    Returns:
        ServiceStack configured with all services.
    """
    stack = core_stack(config=config, db_port=db_port, runner_port=runner_port)

    # RAG Service — hybrid BM25 + FAISS search
    rag_service = ServiceDefinition(
        name="rag-service",
        image_name="tolokaforge-rag-service",
        dockerfile="docker/rag.Dockerfile",
        context=".",
        ports=[PortConfig(container_port=8001, host_port=rag_port)],
        mounts=[Mount.volume("rag_data", "/env/rag")],
        environment={
            "PYTHONUNBUFFERED": "1",
            "CORPUS_PATH": "/env/rag/corpus",
        },
        health_probe=HealthProbe.http(
            url=f"http://localhost:{rag_port}/health",
            timeout_s=30.0,
            interval_s=1.0,
        ),
        networks=["runner-net"],
        profiles=["rag"],
    )

    # Mock Web Service — for browser tasks
    mock_web_service = ServiceDefinition(
        name="mock-web",
        image_name="tolokaforge-mock-web",
        dockerfile="docker/mock_web.Dockerfile",
        context=".",
        ports=[PortConfig(container_port=8080, host_port=mock_web_port)],
        environment={
            "PYTHONUNBUFFERED": "1",
            "JSON_DB_URL": "http://tolokaforge-db-service:8000",
        },
        depends_on=["db-service"],
        networks=["runner-net"],
        profiles=["web"],
    )

    stack.add_services([rag_service, mock_web_service])
    return stack
