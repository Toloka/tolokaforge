"""Tests for Docker build context isolation and container conflict resolution.

Integration tests that require a Docker daemon.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.utils.docker_helpers import is_docker_daemon_available
from tolokaforge.docker.container import Container
from tolokaforge.docker.image import Image

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
# cleanup_resources is provided by tests/integration/docker/conftest.py


@pytest.fixture
def temp_dockerfile_dir(tmp_path: Path) -> Path:
    """Create a temporary directory with a minimal Dockerfile."""
    dockerfile_content = """\
FROM python:3.10-slim

RUN mkdir -p /app /work

WORKDIR /app

CMD ["sleep", "3600"]
"""
    (tmp_path / "Dockerfile").write_text(dockerfile_content)
    return tmp_path


# =========================================================================
# Test 1 — Container 409 conflict auto-resolved  (should PASS)
# =========================================================================


@pytest.mark.integration
@pytest.mark.requires_docker
@pytest.mark.skipif(not is_docker_daemon_available(), reason="Docker not available")
def test_container_409_conflict_auto_resolved(
    temp_dockerfile_dir: Path,
    cleanup_resources: dict,
) -> None:
    """Container.create() auto-removes stale container with same name and retries."""
    # Build a simple image
    image = Image.build(
        dockerfile=str(temp_dockerfile_dir / "Dockerfile"),
        context=str(temp_dockerfile_dir),
        name="test-409-conflict",
    )
    cleanup_resources["images"].append(image)

    # Create first container with a specific name
    container_name = "test-409-conflict-container"
    container1 = Container.create(image=image, name=container_name)
    cleanup_resources["containers"].append(container1)

    # Try to create second container with the SAME name — should auto-resolve
    container2 = Container.create(image=image, name=container_name)
    cleanup_resources["containers"].append(container2)

    assert container2.container_id != container1.container_id
    assert container2.name == container_name


# =========================================================================
# Test 2 — Image caching: no-change build is a cache hit  (should PASS)
# =========================================================================


@pytest.mark.integration
@pytest.mark.requires_docker
@pytest.mark.skipif(not is_docker_daemon_available(), reason="Docker not available")
def test_image_caching_no_change_skips_build(
    temp_dockerfile_dir: Path,
    cleanup_resources: dict,
) -> None:
    """Building same image twice without changes should skip the second build."""
    image1 = Image.build(
        dockerfile=str(temp_dockerfile_dir / "Dockerfile"),
        context=str(temp_dockerfile_dir),
        name="test-caching-no-change",
    )
    cleanup_resources["images"].append(image1)

    image2 = Image.build(
        dockerfile=str(temp_dockerfile_dir / "Dockerfile"),
        context=str(temp_dockerfile_dir),
        name="test-caching-no-change",
    )
    # Don't add to cleanup — same image

    # Same content hash means same tag and same image ID
    assert image1.tag == image2.tag
    assert image1.image_id == image2.image_id
