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


def current_runner_image_id() -> str | None:
    """Docker id of the runner image the *current tree* produces, or ``None``.

    Resolves the exact content-hash ref ``builder.expected_image_ref("runner")``
    — the tag a real ``build_image("runner")`` assigns — and inspects that one
    ref. It is a single exact-ref lookup: no ``docker images`` enumeration, no
    ``.Created`` ranking, no candidate filtering. A foreign image built from a
    different Dockerfile hashes to a different tag, so it cannot match the ref;
    provenance-correctness is structural, not a heuristic.

    ``None`` means the current-tree image is not built (the legitimate skip
    case). The full ``sha256:...`` digest is returned so it compares equal to
    the Docker SDK's ``Image.image_id``.
    """
    import subprocess

    from tolokaforge.docker import builder

    ref = builder.expected_image_ref("runner")
    result = subprocess.run(
        ["docker", "image", "inspect", ref, "--format", "{{.Id}}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None
