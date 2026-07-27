"""Size budget for the built ``tolokaforge-runner`` image (ADR-0022 §4b).

Enforces the uncompressed-size ceiling the slim-runner work commits to. The
lock measures the image the *current tree's* runner definition produces,
resolved by the builder's content-hash SSOT (``current_runner_image_id`` →
``builder.expected_image_ref("runner")``), not by build timestamp — so a fat
image another worktree/branch built into the shared ``tolokaforge-runner`` name
can never be the one measured. It inspects that exact ref's size straight from
the Docker daemon and asserts it stays under a single named ceiling.

The current multi-stage runner Dockerfile measures ~391 MB, a 40.8% reduction
from the pre-slim 659 MB baseline recorded in ADR-0022 §4b (build-only apt +
docker CLI kept out of the runtime stage, wheel installed with ``--no-compile``,
the pip/setuptools toolchain stripped). This module is the behaviour lock that
proves the reduction stays real; the SSOT-consistency lock below proves the
resolver keeps agreeing with what a real build tags.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.utils.docker_helpers import current_runner_image_id, is_docker_daemon_available
from tolokaforge.docker import builder

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]

RUNNER_IMAGE_SIZE_CEILING_MB = 405
"""≤ 405 MB per ADR-0022 §4b — the multi-stage slim runner budget.

The slim image measures 390.2 MB (40.8% under the 659 MB baseline); the ceiling
is that measurement plus ~15 MB slack, which absorbs future minor dependency
bumps without a spurious CI break while still catching a real size regression.
"""


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
    image_id = current_runner_image_id()
    if image_id is None:
        pytest.skip("tolokaforge-runner image not built; size lock needs a built engine image")
    size_mb = _image_size_mb(image_id)
    message = f"runner image {size_mb:.1f} MB exceeds ceiling {RUNNER_IMAGE_SIZE_CEILING_MB} MB"
    assert size_mb <= RUNNER_IMAGE_SIZE_CEILING_MB, message


def test_current_runner_image_id_matches_real_build() -> None:
    """The content-hash resolver agrees with what a real build tags.

    A cache hit after the lane's ``make docker-build`` (``build_image`` returns
    the existing image id), so this is cheap. It FAILS — not skips — if the
    resolver's predicted ref and a real build ever diverge, so a hashing-path
    bug can never masquerade as the legitimate ``None``-then-skip case.
    """
    if not is_docker_daemon_available():
        pytest.skip("Docker daemon not available")
    resolved = current_runner_image_id()
    if resolved is None:
        pytest.skip("tolokaforge-runner image not built")
    built = builder.build_image("runner")
    drift = (
        f"resolver id {resolved!r} != real build id {built.image_id!r} — content-hash SSOT drift"
    )
    assert resolved == built.image_id, drift
