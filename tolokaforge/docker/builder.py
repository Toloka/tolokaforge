"""Image Builder module for Docker Foundation Layer.

Provides a single source of truth for all project Docker image definitions
and utility functions for building images individually or in bulk.

Uses the foundation layer's ImageRegistry for content-hash caching.

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
from tolokaforge.docker.wheel_resolver import resolve_wheel

logger = logging.getLogger(__name__)


def repo_root() -> Path:
    """Repository root, resolved relative to this module — independent of CWD."""
    return Path(__file__).resolve().parents[2]


# =============================================================================
# Image Definitions — Single Source of Truth
# =============================================================================

IMAGE_DEFINITIONS: dict[str, dict[str, Any]] = {
    "db-service": {
        "name": "tolokaforge-db-service",
        "dockerfile": "tolokaforge/docker/dockerfiles/db_service.Dockerfile",
        "context": ".",
        "context_files": [
            "tolokaforge/env/json_db_service/",
        ],
    },
    # "runner" is resolved dynamically — see get_image_definition().
    # "rag-service" is also resolved dynamically (needs wheel + service files).
    "mock-web": {
        "name": "tolokaforge-mock-web",
        "dockerfile": "tolokaforge/docker/dockerfiles/mock_web.Dockerfile",
        "context": ".",
        "context_files": [],
    },
}

# Service groups for selective building
CORE_IMAGES: list[str] = ["db-service", "runner"]
EXTENDED_IMAGES: list[str] = ["rag-service", "mock-web"]

_ALL_KNOWN_SERVICES = {"db-service", "runner", "rag-service", "mock-web"}


def _runner_definition() -> dict[str, Any]:
    """Build the runner image definition dynamically via the wheel resolver.

    The resolver produces a wheel on the host; the Dockerfile installs it.
    The only file in the build context (besides the Dockerfile) is the wheel.
    """
    artifact = resolve_wheel()
    return {
        "name": "tolokaforge-runner",
        "dockerfile": "tolokaforge/docker/dockerfiles/runner.Dockerfile",
        "context": ".",
        "context_files": [
            str(artifact.path),  # absolute path to the .whl
        ],
        "build_args": {
            "WHEEL_FILENAME": artifact.path.name,
        },
    }


def _rag_definition() -> dict[str, Any]:
    """Build the rag-service image definition dynamically.

    The rag service needs both the tolokaforge wheel (for
    ``import tolokaforge.secrets``) and its own service files
    (``requirements.txt`` + ``app.py``).
    """
    artifact = resolve_wheel()
    return {
        "name": "tolokaforge-rag-service",
        "dockerfile": "tolokaforge/docker/dockerfiles/rag.Dockerfile",
        "context": ".",
        "context_files": [
            str(artifact.path),  # wheel (absolute → flat copy)
            "tolokaforge/env/rag_service/",  # service files (relative)
        ],
        "build_args": {
            "WHEEL_FILENAME": artifact.path.name,
        },
    }


def get_image_definition(service_name: str) -> dict[str, Any]:
    """Return the image definition for *service_name*.

    ``runner`` and ``rag-service`` are built dynamically via the wheel
    resolver; all other entries come from the static dict.

    Raises:
        KeyError: If *service_name* is not a known service.
    """
    if service_name == "runner":
        return _runner_definition()
    if service_name == "rag-service":
        return _rag_definition()
    if service_name in IMAGE_DEFINITIONS:
        return IMAGE_DEFINITIONS[service_name]
    raise KeyError(f"Unknown service '{service_name}'. Available: {sorted(_ALL_KNOWN_SERVICES)}")


def service_name_for_image(image_name: str) -> str | None:
    """Return the service whose definition produces ``image_name`` (without tag).

    Searches both static (``IMAGE_DEFINITIONS``) and dynamically-resolved
    (``runner``, ``rag-service``) services uniformly. Returns ``None`` if no
    known service matches.
    """
    for svc in _ALL_KNOWN_SERVICES:
        if get_image_definition(svc)["name"] == image_name:
            return svc
    return None


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
    definition = get_image_definition(service_name)
    context_files = definition.get("context_files", [])
    build_args = definition.get("build_args")

    if context_files:
        build_context = assemble_build_context(
            repo_root=repo_root(),
            dockerfile=definition["dockerfile"],
            context_files=context_files,
        )
        dockerfile_path = build_context / definition["dockerfile"]
        try:
            if force:
                logger.info("Force building image for '%s' with isolated context", service_name)
                return Image.build(
                    dockerfile=str(dockerfile_path),
                    context=str(build_context),
                    name=definition["name"],
                    build_args=build_args,
                )

            if registry is None:
                registry = ImageRegistry()

            return registry.get_or_build(
                name=definition["name"],
                dockerfile=str(dockerfile_path),
                context=str(build_context),
                build_args=build_args,
            )
        finally:
            shutil.rmtree(build_context, ignore_errors=True)

    if force:
        logger.info("Force building image for '%s'", service_name)
        return Image.build(
            dockerfile=definition["dockerfile"],
            context=definition["context"],
            name=definition["name"],
            build_args=build_args,
        )

    if registry is None:
        registry = ImageRegistry()

    return registry.get_or_build(
        name=definition["name"],
        dockerfile=definition["dockerfile"],
        context=definition["context"],
        build_args=build_args,
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
        Path to temporary build directory. The caller owns the directory and
        must remove it with ``shutil.rmtree`` (typically inside a ``finally``).

    Raises:
        FileNotFoundError: If a declared file or directory does not exist.
    """
    build_dir = Path(tempfile.mkdtemp(prefix="tolokaforge-build-"))

    # Copy Dockerfile
    src_dockerfile = repo_root / dockerfile
    dst_dockerfile = build_dir / dockerfile
    dst_dockerfile.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_dockerfile, dst_dockerfile)

    # Copy declared context files.
    # Paths may be relative (resolved against repo_root) or absolute
    # (e.g. a wheel from the wheel-cache — copied flat into build_dir).
    for entry in context_files:
        entry_path = Path(entry)
        if entry_path.is_absolute():
            # Absolute path: copy the file flat into the build dir root.
            if entry_path.is_file():
                shutil.copy2(entry_path, build_dir / entry_path.name)
            elif entry_path.is_dir():
                shutil.copytree(
                    entry_path,
                    build_dir / entry_path.name,
                    dirs_exist_ok=True,
                )
            else:
                shutil.rmtree(build_dir, ignore_errors=True)
                raise FileNotFoundError(f"Declared absolute context path not found: {entry}")
        else:
            # Relative path: resolve against repo_root (original behavior).
            src = repo_root / entry
            dst = build_dir / entry
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            elif src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            else:
                shutil.rmtree(build_dir, ignore_errors=True)
                raise FileNotFoundError(
                    f"Declared context path not found: {entry} (resolved to {src})"
                )

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
