"""Shared primitives for the deploy integration suite.

Both the keyless image-level rc-smoke (M14-B) and the local-vs-published parity
runbook (M14-F extends the suite) drive first-party images through the same
``docker`` CLI operations: resolve an image reference, make it available, run it
standalone, wait for Docker health, and exec documented subcommands. Those
primitives live here so every deploy module shares one implementation.

Image references are always ``tolokasoft1/tolokaforge-<component>:<tag>``. In the
publish workflow the tag is the freshly-pushed ``:{version}`` and the images pull
from Docker Hub. For a pre-publish local exercise, tag the locally-built images
``tolokasoft1/tolokaforge-<component>:local`` and set
``TOLOKAFORGE_SMOKE_IMAGE_LOCAL=1`` so the primitives confirm the image is
present locally instead of pulling from the registry.
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from tests.utils.docker_helpers import is_docker_daemon_available

IMAGE_COMPONENTS: tuple[str, ...] = ("runner", "db-service", "rag-service", "mock-web")

HEALTHY_TIMEOUT_S = 120.0
_POLL_INTERVAL_S = 2.0
_TRUTHY = frozenset({"1", "true", "yes"})


def smoke_image_tag() -> str | None:
    """The tag under test, or ``None`` when the rc-smoke run-guard is not set."""
    return os.environ.get("TOLOKAFORGE_SMOKE_IMAGE_TAG") or None


def published_image_ref(component: str, tag: str) -> str:
    """Full Docker Hub reference for a first-party image component and tag."""
    return f"tolokasoft1/tolokaforge-{component}:{tag}"


def _use_local_images() -> bool:
    return os.environ.get("TOLOKAFORGE_SMOKE_IMAGE_LOCAL", "").strip().lower() in _TRUTHY


def obtain_image(ref: str) -> subprocess.CompletedProcess[str]:
    """Make ``ref`` available locally and return the docker process for asserting.

    Pulls ``ref`` from the registry (the workflow path), or — in local-exercise
    mode — inspects it to confirm a locally-built, locally-tagged image is
    present. The caller asserts ``returncode == 0``.
    """
    op = ["image", "inspect", ref] if _use_local_images() else ["pull", ref]
    return subprocess.run(["docker", *op], capture_output=True, text=True)


def wait_for_health(container_id: str, timeout_s: float = HEALTHY_TIMEOUT_S) -> str:
    """Poll ``container_id`` Docker health until ``healthy``/``unhealthy`` or timeout.

    Returns the last observed status. ``unhealthy`` is returned as soon as it is
    seen so a failing probe fails fast; an empty string means the container is
    gone or reports no health.
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


def docker_exec(
    container_id: str, args: list[str], *, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``docker exec`` (``-i`` when feeding stdin) and capture its output."""
    flags = ["-i"] if stdin is not None else []
    return subprocess.run(
        ["docker", "exec", *flags, container_id, *args],
        capture_output=True,
        text=True,
        input=stdin,
    )


@pytest.fixture(scope="session")
def docker_daemon() -> None:
    """Skip the whole deploy test if no Docker daemon is reachable."""
    if not is_docker_daemon_available():
        pytest.skip("Docker daemon not available")
