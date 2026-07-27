"""Docker-health self-report locks for the four first-party service images.

Every first-party image starts standalone and must transition to Docker health
``healthy``: db-service (curl ``/health``) and runner (gRPC channel probe)
already carried a HEALTHCHECK; rag-service and mock-web gained a python-urllib
``/health`` HEALTHCHECK so all four self-report uniformly. That uniformity is
what lets the M14-B publish rc-smoke assert
``docker inspect --format '{{.State.Health.Status}}' == healthy`` per image
instead of a per-service ad-hoc probe.

The fixture builds each image via ``builder.build_image`` (cache-instant after
``make docker-build``) rather than skip-if-absent: mock-web's build context is
the repo root, so its content-hash ref shifts with any working-tree change and a
skip-guard keyed on the exact ref would perpetually skip mock-web — the one
image whose HEALTHCHECK this suite exists to lock.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterator

import pytest

from tests.utils.docker_helpers import is_docker_daemon_available
from tolokaforge.docker import builder

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker, pytest.mark.slow]

_SERVICES = ["db-service", "runner", "rag-service", "mock-web"]
_HEALTHY_TIMEOUT_S = 120.0
_POLL_INTERVAL_S = 2.0


def _wait_for_health(container_id: str, timeout_s: float) -> str:
    """Poll the container's Docker health until ``healthy`` or a terminal state.

    Returns the last observed status. An empty string means the container is
    gone or reports no health (inspect failed); ``unhealthy`` is returned as
    soon as it is seen so a failing probe fails fast instead of waiting out the
    full timeout.
    """
    deadline = time.monotonic() + timeout_s
    status = ""
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", container_id],
            capture_output=True,
            text=True,
        )
        status = result.stdout.strip()
        if status in {"healthy", "unhealthy"}:
            return status
        time.sleep(_POLL_INTERVAL_S)
    return status


@pytest.fixture
def running_container(request: pytest.FixtureRequest) -> Iterator[tuple[str, str]]:
    if not is_docker_daemon_available():
        pytest.skip("Docker daemon not available")
    service: str = request.param
    image = builder.build_image(service)
    started = subprocess.run(
        ["docker", "run", "-d", "--rm", image.full_tag],
        capture_output=True,
        text=True,
        check=True,
    )
    container_id = started.stdout.strip()
    try:
        yield service, container_id
    finally:
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)


@pytest.mark.parametrize("running_container", _SERVICES, indirect=True)
def test_service_image_reports_healthy(running_container: tuple[str, str]) -> None:
    """Each first-party image reaches Docker health ``healthy`` started standalone."""
    service, container_id = running_container
    status = _wait_for_health(container_id, _HEALTHY_TIMEOUT_S)
    assert status == "healthy", f"{service} never became healthy (last status: {status!r})"
