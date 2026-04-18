"""Docker container fixtures using testcontainers for integration tests.

This module provides pytest fixtures for managing Docker containers using
the testcontainers library. Containers are managed automatically with proper
lifecycle handling and cleanup.

Images are auto-built from the project's Dockerfiles when not found locally.
"""

import logging
from datetime import timedelta

import pytest
from docker.errors import ImageNotFound
from testcontainers.core.generic import DockerContainer
from testcontainers.core.wait_strategies import (
    HttpWaitStrategy,
    LogMessageWaitStrategy,
)

import docker

logger = logging.getLogger(__name__)


def _check_image_available(image_name: str) -> None:
    """Ensure a Docker image is available locally, building it if necessary.

    Uses tolokaforge.docker.builder to auto-build images from the project's
    Dockerfiles when they are not found locally.

    Args:
        image_name: Full image name with tag (e.g., "tolokaforge-rag-service:latest")

    Raises:
        pytest.skip: If the image cannot be built (e.g., missing Dockerfile or build error)
    """
    try:
        client = docker.from_env()
        client.images.get(image_name)
        return  # Image exists
    except ImageNotFound:
        pass
    except Exception as exc:
        pytest.skip(f"Docker not available: {exc}")
        return

    # Image not found — try to build it from the project's Dockerfiles
    from tolokaforge.docker.builder import IMAGE_DEFINITIONS

    # Resolve image name (without tag) to service name
    base_name = image_name.split(":")[0]
    service_name = None
    for svc, defn in IMAGE_DEFINITIONS.items():
        if defn["name"] == base_name:
            service_name = svc
            break

    if service_name is None:
        pytest.skip(f"Docker image '{image_name}' not found and no build definition exists for it")
        return

    logger.info(
        "Image '%s' not found — building from Dockerfile (service: %s)", image_name, service_name
    )
    try:
        from tolokaforge.docker.builder import build_image

        image = build_image(service_name, force=True)
        # Builder uses content-hash tags (e.g., :a3b8f2c1).
        # Tag as :latest so testcontainers can find it.
        client = docker.from_env()
        docker_image = client.images.get(image.full_tag)
        docker_image.tag(base_name, tag="latest")
        logger.info("Built and tagged '%s:latest' (from %s)", base_name, image.full_tag)
    except Exception as exc:
        pytest.skip(
            f"Failed to build Docker image '{image_name}' for service '{service_name}': {exc}"
        )


@pytest.fixture(scope="session")
def json_db_container(env_network):
    """JSON database service container.

    Provides a containerized JSON database service with health checks.
    Maps port 8000 and connects to the runner-net network.

    Returns:
        DockerContainer: Running db-service container with exposed port

    Note:
        Skips gracefully if the Docker image is not available locally.
    """
    image_name = "tolokaforge-db-service:latest"
    _check_image_available(image_name)

    container = DockerContainer(image_name)
    container.with_exposed_ports(8000)
    container.with_env("PYTHONUNBUFFERED", "1")
    container.with_network(env_network)
    container.with_network_aliases("json-db", "db-service")
    container.waiting_for(
        HttpWaitStrategy(8000, path="/health")
        .for_status_code(200)
        .with_startup_timeout(timedelta(seconds=30))
    )

    container.start()

    yield container

    container.stop()


@pytest.fixture(scope="session")
def rag_service_container(env_network, rag_data_volume):
    """RAG (Retrieval-Augmented Generation) service container.

    Provides a containerized RAG service for knowledge retrieval.
    Uses a named volume for persistent corpus data.

    Returns:
        DockerContainer: Running rag-service container with exposed port

    Note:
        Skips gracefully if the Docker image is not available locally.
    """
    image_name = "tolokaforge-rag-service:latest"
    _check_image_available(image_name)

    container = DockerContainer(image_name)
    container.with_exposed_ports(8001)
    container.with_env("PYTHONUNBUFFERED", "1")
    container.with_env("CORPUS_PATH", "/env/rag/corpus")
    container.with_network(env_network)
    container.with_network_aliases("rag-service")
    container.with_volume_mapping(rag_data_volume, "/env/rag")
    container.waiting_for(
        HttpWaitStrategy(8001, path="/health")
        .for_status_code(200)
        .with_startup_timeout(timedelta(seconds=120))
    )

    container.start()

    yield container

    container.stop()


@pytest.fixture(scope="session")
def runner_container(
    env_network,
    json_db_container,
    rag_service_container,
    env_files_volume,
):
    """Runner service container.

    Provides a containerized gRPC Runner service that handles:
    - Trial registration with TaskDescription
    - Tool execution (Tau, MCP async, MCP server styles)
    - Trial grading via golden path comparison
    - State management via DB Service

    Depends on json-db and rag services. Only connected to env-net (no external access).

    Returns:
        DockerContainer: Running runner container with exposed gRPC port

    Note:
        Skips gracefully if the Docker image is not available locally.
    """
    image_name = "tolokaforge-runner:latest"
    _check_image_available(image_name)

    container = DockerContainer(image_name)
    container.with_exposed_ports(50051)
    container.with_env("PYTHONUNBUFFERED", "1")
    container.with_env("DB_SERVICE_URL", "http://db-service:8000")
    container.with_env("RAG_SERVICE_URL", "http://rag-service:8001")
    container.with_network(env_network)
    container.with_network_aliases("runner")
    container.with_volume_mapping(env_files_volume, "/env/fs")
    container.waiting_for(
        LogMessageWaitStrategy("Starting Runner server").with_startup_timeout(timedelta(seconds=60))
    )

    container.start()

    yield container

    container.stop()


# Backward compatibility alias
executor_container = runner_container


@pytest.fixture(scope="session")
def services_stack(
    json_db_container,
    rag_service_container,
    runner_container,
):
    """Complete service stack for E2E tests.

    Provides all services in a single fixture for convenience.
    Services are started in dependency order.

    Returns:
        Dict[str, DockerContainer]: Dictionary of service name to container
    """
    return {
        "json_db": json_db_container,
        "db_service": json_db_container,  # Alias for new naming
        "rag_service": rag_service_container,
        "runner": runner_container,
        "executor": runner_container,  # Backward compatibility alias
    }
