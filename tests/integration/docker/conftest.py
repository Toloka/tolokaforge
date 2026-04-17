"""Shared fixtures for Docker integration tests.

Provides resource-tracking fixtures that ensure proper cleanup of Docker
images, containers, and networks after each test — even when individual
cleanup calls fail.
"""

from __future__ import annotations

import logging

import pytest

logger = logging.getLogger(__name__)


@pytest.fixture
def cleanup_images():
    """Track and clean up Docker images after each test.

    Usage::

        def test_something(cleanup_images):
            image = Image.build(...)
            cleanup_images.append(image)
            ...
    """
    images: list = []
    yield images
    for img in images:
        try:
            img.remove(force=True)
        except Exception:  # noqa: BLE001 - Best-effort Docker cleanup
            logger.warning("Failed to clean up Docker image: %s", img, exc_info=True)


@pytest.fixture
def cleanup_resources():
    """Track and clean up Docker resources (containers, networks, images) after each test.

    Usage::

        def test_something(cleanup_resources):
            image = Image.build(...)
            cleanup_resources["images"].append(image)
            container = Container.create(image=image, ...)
            cleanup_resources["containers"].append(container)
            ...
    """
    resources: dict[str, list] = {"containers": [], "networks": [], "images": []}
    yield resources

    # Cleanup in reverse dependency order: containers → networks → images
    for container in resources["containers"]:
        try:
            container.stop(timeout_s=5)
        except Exception:  # noqa: BLE001 - Best-effort Docker cleanup
            logger.warning("Failed to stop container %s", container, exc_info=True)
        try:
            container.destroy()
        except Exception:  # noqa: BLE001 - Best-effort Docker cleanup
            logger.warning("Failed to destroy container %s", container, exc_info=True)

    for network in resources["networks"]:
        try:
            network.destroy()
        except Exception:  # noqa: BLE001 - Best-effort Docker cleanup
            logger.warning("Failed to destroy network %s", network, exc_info=True)

    for image in resources["images"]:
        try:
            image.remove(force=True)
        except Exception:  # noqa: BLE001 - Best-effort Docker cleanup
            logger.warning("Failed to remove image %s", image, exc_info=True)
