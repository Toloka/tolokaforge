"""Image Builder module for Docker Foundation Layer.

Provides a single source of truth for all project Docker image definitions
and utility functions for building images individually or in bulk.

This replaces scripts/release/build_docker_images.sh with a Python-native
builder that uses the foundation layer's ImageRegistry for content-hash caching.

Example:
    >>> from tolokaforge.docker.builder import build_all_images, build_image
    >>>
    >>> # Build all images
    >>> images = build_all_images()
    >>>
    >>> # Build core images only
    >>> images = build_all_images(core_only=True)
    >>>
    >>> # Build a single image
    >>> image = build_image("db-service")
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

from tolokaforge.docker.image import Image
from tolokaforge.docker.registry import ImageRegistry

logger = logging.getLogger(__name__)


# =============================================================================
# Image Definitions — Single Source of Truth
# =============================================================================

IMAGE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "db-service": {
        "name": "tolokaforge-db-service",
        "dockerfile": "docker/db_service.Dockerfile",
        "context": ".",
        "context_files": [
            "tolokaforge/env/json_db_service/",
        ],
    },
    "runner": {
        "name": "tolokaforge-runner",
        "dockerfile": "docker/runner.Dockerfile",
        "context": ".",
        "context_files": [
            "pyproject.toml",
            "README.md",
            "tolokaforge/",
            "contrib/tau-bench/",
            "tasks/telecom/tau_tools/",
            "tasks/telecom/data/",
        ],
    },
    "rag-service": {
        "name": "tolokaforge-rag-service",
        "dockerfile": "docker/rag.Dockerfile",
        "context": ".",
        "context_files": [],
    },
    "mock-web": {
        "name": "tolokaforge-mock-web",
        "dockerfile": "docker/mock_web.Dockerfile",
        "context": ".",
        "context_files": [],
    },
}

# Service groups for selective building
CORE_IMAGES: list[str] = ["db-service", "runner"]
EXTENDED_IMAGES: list[str] = ["rag-service", "mock-web"]


def build_all_images(
    core_only: bool = False,
    force: bool = False,
    registry: ImageRegistry | None = None,
) -> dict[str, Image]:
    """Build all project Docker images.

    Uses ImageRegistry for content-hash caching. Only rebuilds when
    Dockerfile or context files change.

    Args:
        core_only: If True, only build core images (db-service + runner).
        force: If True, force rebuild even if cached.
        registry: Optional ImageRegistry instance (creates new if None).

    Returns:
        Dictionary mapping service name to built Image.

    Example:
        >>> images = build_all_images(core_only=True)
        >>> images["db-service"].full_tag
        'tolokaforge-db-service:a3b8f2c1'
    """
    if registry is None:
        registry = ImageRegistry()

    services_to_build = list(CORE_IMAGES)
    if not core_only:
        services_to_build.extend(EXTENDED_IMAGES)

    logger.info(
        "Building %d images (core_only=%s, force=%s)",
        len(services_to_build),
        core_only,
        force,
    )

    images: dict[str, Image] = {}
    failed: list[str] = []

    for service_name in services_to_build:
        try:
            image = build_image(service_name, registry=registry, force=force)
            images[service_name] = image
            logger.info("✓ Built %s → %s", service_name, image.full_tag)
        except Exception as e:
            logger.error("✗ Failed to build %s: %s", service_name, e)
            failed.append(service_name)

    if failed:
        logger.warning(
            "Image build completed with %d failure(s): %s",
            len(failed),
            failed,
        )
    else:
        logger.info("All %d images built successfully", len(images))

    return images


def build_image(
    service_name: str,
    registry: ImageRegistry | None = None,
    force: bool = False,
) -> Image:
    """Build a single service image.

    Args:
        service_name: Name of the service (key in IMAGE_DEFINITIONS).
        registry: Optional ImageRegistry instance.
        force: If True, force rebuild.

    Returns:
        Built Image instance.

    Raises:
        KeyError: If service_name is not in IMAGE_DEFINITIONS.
        ImageError: If build fails.

    Example:
        >>> image = build_image("db-service")
        >>> image.exists()
        True
    """
    if service_name not in IMAGE_DEFINITIONS:
        raise KeyError(
            f"Unknown service '{service_name}'. Available: {list(IMAGE_DEFINITIONS.keys())}"
        )

    definition = IMAGE_DEFINITIONS[service_name]

    if force:
        # Force rebuild bypasses registry cache
        logger.info("Force building image for '%s'", service_name)
        return Image.build(
            dockerfile=definition["dockerfile"],
            context=definition["context"],
            name=definition["name"],
        )

    if registry is None:
        registry = ImageRegistry()

    return registry.get_or_build(
        name=definition["name"],
        dockerfile=definition["dockerfile"],
        context=definition["context"],
    )


def assemble_build_context(
    repo_root: Path,
    dockerfile: str,
    context_files: list[str],
) -> Path:
    """Create a temporary build directory with only the declared files.

    Instead of using the entire repo as Docker build context, this function
    creates a self-contained temp directory with only the files needed for
    the build. This makes content hashing deterministic and builds faster.

    Args:
        repo_root: Repository root directory.
        dockerfile: Path to Dockerfile (relative to repo_root).
        context_files: List of file/directory paths to include (relative to repo_root).

    Returns:
        Path to temporary build directory. Caller is responsible for cleanup
        (use shutil.rmtree or pass to Image.build which handles it).
    """
    build_dir = Path(tempfile.mkdtemp(prefix="tolokaforge-build-"))

    # Copy Dockerfile
    src_dockerfile = repo_root / dockerfile
    dst_dockerfile = build_dir / dockerfile
    dst_dockerfile.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_dockerfile, dst_dockerfile)

    # Copy declared context files
    for rel_path in context_files:
        src = repo_root / rel_path
        dst = build_dir / rel_path
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        elif src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        else:
            logger.warning("Context file not found, skipping: %s", src)

    return build_dir


def list_built_images(
    registry: ImageRegistry | None = None,
) -> list[dict[str, str]]:
    """List status of all project images.

    Returns a list of dicts with service name, image tag, and whether
    the image exists in the local Docker daemon.

    Args:
        registry: Optional ImageRegistry to check cache state.

    Returns:
        List of status dictionaries with keys: service, image_name,
        dockerfile, status.

    Example:
        >>> statuses = list_built_images()
        >>> for s in statuses:
        ...     print(f"{s['service']}: {s['status']}")
    """
    statuses: list[dict[str, str]] = []

    for service_name, definition in IMAGE_DEFINITIONS.items():
        status_info: dict[str, str] = {
            "service": service_name,
            "image_name": definition["name"],
            "dockerfile": definition["dockerfile"],
            "status": "not_built",
        }

        if registry:
            images = registry.get_images_by_name(definition["name"])
            if images:
                status_info["status"] = "cached"
                status_info["tag"] = images[0].full_tag
            else:
                status_info["status"] = "not_cached"

        statuses.append(status_info)

    return statuses
