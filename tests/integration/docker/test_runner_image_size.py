"""Size budget for the built ``tolokaforge-runner`` image (ADR-0019 §4b).

Enforces the uncompressed-size ceiling the slim-runner work commits to. Reads
the size of the locally built runner image straight from the Docker daemon and
asserts it stays under a single named ceiling.

Recorded baseline (provenance, mirroring ADR-0019 §4b):

- **659 MB** — measured 2026-07-20 via ``docker images`` on a freshly built
  ``tolokaforge-runner`` (image ID ``325b9be60fa8``), matching the ADR baseline.
- Layer breakdown (``docker history``):
  - base ``python:3.12-slim`` ≈ 144 MB (fixed floor)
  - apt ``curl``/``git``/``ca-certificates`` ~104 MB (build-only — the runner
    never clones)
  - ``docker-ce-cli`` + compose-plugin 74.7 MB (only terminal-bench shells out
    to docker)
  - ``pip install wheel[docker]`` 298 MB (the dead ``[docker]`` extra pulls the
    full base wheel)
  - 11 hand-listed extra deps 37 MB
  - site-packages 371 MB total (includes ``litellm``/proxy, ``pip``,
    ``setuptools``, ``grpc_tools`` build-time compiler, and ``*.pyc`` bytecode)

The multi-stage runner Dockerfile achieves the reduction (build-only apt +
docker CLI kept out of the runtime stage, wheel installed with ``--no-compile``,
the pip/setuptools toolchain stripped); this module is the behaviour lock that
proves it stays real.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.utils.docker_helpers import is_docker_daemon_available, newest_image_id
from tolokaforge.docker import builder

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]

RUNNER_IMAGE_SIZE_CEILING_MB = 405
"""≤ 405 MB per ADR-0019 §4b — the multi-stage slim runner budget.

The slim image measures 390.2 MB (40.8% under the 659 MB baseline); the ceiling
is that measurement plus ~15 MB slack, which absorbs future minor dependency
bumps without a spurious CI break while still catching a real size regression.
"""


def _resolve_runner_image_id() -> str | None:
    # Read the image name from the builder's static definition table rather
    # than get_image_definition — the latter triggers wheel resolution as a
    # side effect, which a size lock has no need for.
    return newest_image_id(builder.IMAGE_DEFINITIONS["runner"]["name"])


def _image_size_mb(image_id: str) -> float:
    """Uncompressed image size in MB (SI, matching ``docker images`` SIZE)."""
    result = subprocess.run(
        ["docker", "image", "inspect", image_id, "--format", "{{.Size}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(result.stdout.strip()) / 1_000_000


def test_runner_image_within_size_ceiling() -> None:
    if not is_docker_daemon_available():
        pytest.skip("Docker daemon not available")
    image_id = _resolve_runner_image_id()
    if image_id is None:
        pytest.skip("tolokaforge-runner image not built; size lock needs a built engine image")
    size_mb = _image_size_mb(image_id)
    message = f"runner image {size_mb:.1f} MB exceeds ceiling {RUNNER_IMAGE_SIZE_CEILING_MB} MB"
    assert size_mb <= RUNNER_IMAGE_SIZE_CEILING_MB, message
