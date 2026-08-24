"""Docker-health self-report locks for the four first-party service images.

Every first-party image starts standalone and must transition to Docker health
``healthy``: db-service (curl ``/health``) and runner (gRPC channel probe)
already carried a HEALTHCHECK; rag-service and mock-web gained a python-urllib
``/health`` HEALTHCHECK so all four self-report uniformly. That uniformity is
what lets the publish rc-smoke assert
``docker inspect --format '{{.State.Health.Status}}' == healthy`` per image
instead of a per-service ad-hoc probe.

The fixture builds each image via ``builder.build_image`` (cache-instant after
``make docker-build``) rather than skip-if-absent: mock-web's build context is
the repo root, so its content-hash ref shifts with any working-tree change and a
skip-guard keyed on the exact ref would perpetually skip mock-web — the one
image whose HEALTHCHECK this suite exists to lock.

rag-service is held to a stronger claim, on the same probe: it reaches
``healthy`` with ``--network none``, which is the direct proof that its
embedding model is baked into the image and no download sits in the startup
path.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from tests.utils.docker_helpers import is_docker_daemon_available, wait_for_health
from tolokaforge.docker import builder

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker, pytest.mark.slow]

_SERVICES = ["db-service", "runner", "rag-service", "mock-web"]

_HEALTH_PROBE = (
    "import urllib.request;"
    "print(urllib.request.urlopen('http://localhost:8001/health').read().decode())"
)


@contextmanager
def _detached_container(image_tag: str, *docker_run_args: str) -> Iterator[str]:
    started = subprocess.run(
        ["docker", "run", "-d", "--rm", *docker_run_args, image_tag],
        capture_output=True,
        text=True,
        check=True,
    )
    container_id = started.stdout.strip()
    try:
        yield container_id
    finally:
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)


@pytest.fixture
def running_container(request: pytest.FixtureRequest) -> Iterator[tuple[str, str]]:
    if not is_docker_daemon_available():
        pytest.skip("Docker daemon not available")
    service: str = request.param
    image = builder.build_image(service)
    with _detached_container(image.full_tag) as container_id:
        yield service, container_id


@pytest.fixture
def offline_rag_container() -> Iterator[str]:
    if not is_docker_daemon_available():
        pytest.skip("Docker daemon not available")
    image = builder.build_image("rag-service")
    with _detached_container(image.full_tag, "--network", "none") as container_id:
        yield container_id


@pytest.mark.parametrize("running_container", _SERVICES, indirect=True)
def test_service_image_reports_healthy(running_container: tuple[str, str]) -> None:
    """Each first-party image reaches Docker health ``healthy`` started standalone."""
    service, container_id = running_container
    status = wait_for_health(container_id)
    assert status == "healthy", f"{service} never became healthy (last status: {status!r})"


def test_rag_image_serves_its_baked_model_with_no_network(offline_rag_container: str) -> None:
    """rag-service stands up cut off from the network with its semantic backend loaded."""
    status = wait_for_health(offline_rag_container)
    assert status == "healthy", (
        f"rag-service never became healthy on --network none (last status: {status!r}) — the "
        "embedding model is not in the image, so the load reached for HuggingFace and failed"
    )

    probe = subprocess.run(
        ["docker", "exec", offline_rag_container, "python", "-c", _HEALTH_PROBE],
        capture_output=True,
        text=True,
        check=True,
    )
    body = json.loads(probe.stdout)

    assert body["status"] == "healthy", f"/health reports {body}"
    assert body["faiss_available"] is True, (
        f"the semantic backend is not loaded offline: {body} — Docker health alone cannot tell "
        "a loaded backend from a BM25-only build"
    )
    assert body["reason"] is None, f"/health carries a degraded reason: {body}"
