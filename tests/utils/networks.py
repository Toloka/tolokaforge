"""Docker network fixtures using testcontainers for test isolation.

This module provides pytest fixtures for managing Docker networks to ensure
proper isolation between test containers and external networks.
"""

import logging

import pytest
from testcontainers.core.network import Network

logger = logging.getLogger(__name__)


@pytest.fixture(scope="session")
def env_network():
    """Isolated internal network for service containers.

    Creates a Docker network that simulates the env-net from docker-compose.
    This network provides container-to-container communication.

    The network is created once per test session and shared across all tests.

    Returns:
        Network: Testcontainers network object for env-net
    """
    with Network() as network:
        yield network


@pytest.fixture(scope="session")
def env_files_volume():
    """Named volume for environment files storage.

    Creates a persistent Docker volume for storing environment files
    that executor needs access to.

    Returns:
        str: Volume name
    """
    import docker

    client = docker.from_env()
    volume = client.volumes.create(
        name="env_files_testcontainers",
        labels={"managed_by": "testcontainers"},
    )

    yield volume.name

    # Cleanup after test session
    try:
        volume.remove(force=True)
    except Exception:  # noqa: BLE001 - Best-effort Docker volume cleanup
        logger.warning("Failed to clean up Docker volume %s", volume.name, exc_info=True)


@pytest.fixture(scope="session")
def rag_data_volume():
    """Named volume for RAG service corpus data.

    Creates a persistent Docker volume for RAG service to store
    corpus data.

    Returns:
        str: Volume name
    """
    import docker

    client = docker.from_env()
    volume = client.volumes.create(
        name="rag_data_testcontainers",
        labels={"managed_by": "testcontainers"},
    )

    yield volume.name

    # Cleanup after test session
    try:
        volume.remove(force=True)
    except Exception:  # noqa: BLE001 - Best-effort Docker volume cleanup
        logger.warning("Failed to clean up Docker volume %s", volume.name, exc_info=True)
