"""Shared Docker availability helpers for integration tests.

This module provides two distinct Docker availability checks:

- ``is_docker_runner_available()`` — checks whether the Docker Runner + DB Service
  containers (gRPC on port 50051 and HTTP on port 8000) are running and healthy.
  Used by tests that exercise the full Runner pipeline via Docker Compose.

- ``is_docker_daemon_available()`` — checks whether the Docker daemon itself is
  reachable (``docker.from_env().ping()``).  Used by tests that build images or
  manage containers directly via the Docker SDK.
"""

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DOCKER_RUNNER_ADDRESS = "localhost:50051"
DOCKER_DB_SERVICE_URL = "http://localhost:8000"


# ---------------------------------------------------------------------------
# Docker Runner container availability (D2)
# ---------------------------------------------------------------------------


def is_docker_runner_available() -> bool:
    """Check if Docker Runner + DB Service containers are running and accessible."""
    try:
        import grpc
        import httpx

        channel = grpc.insecure_channel(DOCKER_RUNNER_ADDRESS)
        grpc.channel_ready_future(channel).result(timeout=2)
        channel.close()

        response = httpx.get(f"{DOCKER_DB_SERVICE_URL}/health", timeout=2)
        return response.status_code == 200
    except Exception:
        return False


@pytest.fixture
def skip_if_no_docker_runner():
    """Skip test if Docker Runner containers are not available."""
    if not is_docker_runner_available():
        pytest.skip("Docker containers not available. Run: make docker-up")


# ---------------------------------------------------------------------------
# Docker daemon availability (D3)
# ---------------------------------------------------------------------------


def is_docker_daemon_available() -> bool:
    """Check if the Docker daemon is reachable and operational.

    Verifies both daemon connectivity (ping) and that Docker operations
    work end-to-end — including credential store access, which can fail
    in devcontainer environments with broken credential helpers.
    """
    try:
        import docker

        client = docker.from_env()
        client.ping()
        # Also verify credential store works — get_all_credentials() is
        # called internally during image builds to set auth headers.
        # A broken credsStore in ~/.docker/config.json causes this to fail.
        docker.auth.load_config().get_all_credentials()
        return True
    except Exception:
        return False
