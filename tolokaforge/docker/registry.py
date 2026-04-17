"""Image Registry module for Docker Foundation Layer.

Abstraction layer for image sources. Tracks which images are available and
manages build-vs-pull decisions. This is the primary entry point for the upper
layer (not Image.build directly).

Uses Pydantic BaseModel for validation and serialization.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, PrivateAttr

from tolokaforge.docker.image import Image, ImageError

if TYPE_CHECKING:
    from docker import DockerClient

logger = logging.getLogger(__name__)


class RegistryError(Exception):
    """Raised when a registry operation fails."""

    def __init__(self, operation: str, message: str):
        self.operation = operation
        super().__init__(f"Registry {operation} failed: {message}")


class TrackedImage(BaseModel):
    """Represents a tracked image in the registry with metadata.

    Attributes:
        image: The underlying Image object.
        created_at: When the image was first tracked.
        last_used_at: When the image was last accessed via get_or_build.
    """

    image: Image = Field(description="The underlying Image object")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When the image was first tracked",
    )
    last_used_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When the image was last accessed",
    )

    model_config = {
        "extra": "forbid",
    }


class ImageRegistry(BaseModel):
    """Tracks available images and manages build-vs-pull decisions.

    This is the primary entry point for the upper layer. It maintains an
    internal registry of all images it has built or found, and delegates
    to Image.build for actual building (which handles hash-based skip).

    Attributes:
        images: Dictionary mapping full_tag to TrackedImage.

    Example:
        >>> registry = ImageRegistry()
        >>> image = registry.get_or_build(
        ...     name="tolokaforge-executor",
        ...     dockerfile="docker/executor.Dockerfile",
        ...     context=".",
        ... )
        >>> registry.list_images()
        [Image(name='tolokaforge-executor', tag='a3b8f2c1', ...)]
        >>> registry.prune(keep_latest=3)
    """

    images: dict[str, TrackedImage] = Field(
        default_factory=dict,
        description="Dictionary mapping full_tag to TrackedImage",
    )

    # Private attribute for Docker client (not serialized)
    _client: DockerClient | None = PrivateAttr(default=None)

    model_config = {
        "extra": "forbid",
    }

    def get_or_build(
        self,
        name: str,
        dockerfile: str,
        context: str,
        build_args: dict[str, str] | None = None,
    ) -> Image:
        """Return a cached image if the content hash matches, otherwise build a new one.

        This is the primary entry point for the upper layer. It delegates to
        Image.build which already handles hash-based skip logic.

        Args:
            name: Base name for the image (e.g., "tolokaforge-executor").
            dockerfile: Path to the Dockerfile.
            context: Path to the build context directory.
            build_args: Optional build arguments.

        Returns:
            Image instance (either from cache or newly built).

        Raises:
            ImageError: If build fails or paths are invalid.

        Example:
            >>> registry = ImageRegistry()
            >>> image = registry.get_or_build(
            ...     name="tolokaforge-executor",
            ...     dockerfile="docker/executor.Dockerfile",
            ...     context=".",
            ... )
            >>> image.full_tag
            'tolokaforge-executor:a3b8f2c1'
        """
        logger.info(
            "get_or_build called for '%s' (dockerfile=%s, context=%s)",
            name,
            dockerfile,
            context,
        )

        # Delegate to Image.build which handles hash-based skip
        image = Image.build(
            dockerfile=dockerfile,
            context=context,
            build_args=build_args,
            name=name,
            client=self._client,
        )

        # Track the image in our registry
        now = datetime.now(timezone.utc)
        if image.full_tag in self.images:
            # Update last_used_at for existing image
            tracked = self.images[image.full_tag]
            self.images[image.full_tag] = TrackedImage(
                image=tracked.image,
                created_at=tracked.created_at,
                last_used_at=now,
            )
            logger.debug("Updated last_used_at for '%s'", image.full_tag)
        else:
            # Add new image to registry
            self.images[image.full_tag] = TrackedImage(
                image=image,
                created_at=now,
                last_used_at=now,
            )
            logger.debug("Added new image '%s' to registry", image.full_tag)

        return image

    def list_images(self) -> list[Image]:
        """List all tracked images.

        Returns:
            List of all Image objects tracked by this registry.

        Example:
            >>> registry = ImageRegistry()
            >>> registry.get_or_build(...)
            >>> images = registry.list_images()
            >>> len(images)
            1
        """
        return [tracked.image for tracked in self.images.values()]

    def prune(self, keep_latest: int = 3) -> list[Image]:
        """Remove old image versions, keeping the N most recent per name.

        Groups images by their base name, sorts by last_used_at, and removes
        all but the most recent N versions. Also removes the images from Docker.

        Args:
            keep_latest: Number of most recent versions to keep per image name.
                        Must be >= 1.

        Returns:
            List of Image objects that were removed.

        Raises:
            RegistryError: If keep_latest is less than 1.

        Example:
            >>> registry = ImageRegistry()
            >>> # Build multiple versions of the same image
            >>> registry.get_or_build(name="myimage", ...)
            >>> # ... modify dockerfile ...
            >>> registry.get_or_build(name="myimage", ...)
            >>> removed = registry.prune(keep_latest=1)
            >>> len(removed)
            1
        """
        if keep_latest < 1:
            raise RegistryError("prune", f"keep_latest must be >= 1, got {keep_latest}")

        logger.info("Pruning images, keeping latest %d per name", keep_latest)

        # Group images by base name
        images_by_name: dict[str, list[TrackedImage]] = {}
        for tracked in self.images.values():
            name = tracked.image.name
            if name not in images_by_name:
                images_by_name[name] = []
            images_by_name[name].append(tracked)

        removed_images: list[Image] = []

        for _name, tracked_list in images_by_name.items():
            # Sort by last_used_at descending (most recent first)
            tracked_list.sort(key=lambda t: t.last_used_at, reverse=True)

            # Keep the first N, remove the rest
            to_remove = tracked_list[keep_latest:]

            for tracked in to_remove:
                image = tracked.image
                logger.info(
                    "Pruning image '%s' (last used: %s)",
                    image.full_tag,
                    tracked.last_used_at.isoformat(),
                )

                # Remove from Docker
                try:
                    # Use the client if available, otherwise let image create its own
                    if self._client is not None:
                        image = image.with_client(self._client)
                    image.remove()
                except ImageError as e:
                    logger.warning("Failed to remove image '%s' from Docker: %s", image.full_tag, e)

                # Remove from registry
                if image.full_tag in self.images:
                    del self.images[image.full_tag]

                removed_images.append(image)

        logger.info("Pruned %d images", len(removed_images))
        return removed_images

    def get_image(self, full_tag: str) -> Image | None:
        """Get a tracked image by its full tag.

        Args:
            full_tag: Full image tag (e.g., "tolokaforge-executor:a3b8f2c1").

        Returns:
            Image if found, None otherwise.

        Example:
            >>> registry = ImageRegistry()
            >>> image = registry.get_or_build(...)
            >>> found = registry.get_image(image.full_tag)
            >>> found is not None
            True
        """
        tracked = self.images.get(full_tag)
        return tracked.image if tracked else None

    def get_images_by_name(self, name: str) -> list[Image]:
        """Get all tracked images with a given base name.

        Args:
            name: Base name of the image (e.g., "tolokaforge-executor").

        Returns:
            List of Image objects with the given name, sorted by last_used_at
            (most recent first).

        Example:
            >>> registry = ImageRegistry()
            >>> images = registry.get_images_by_name("tolokaforge-executor")
        """
        result = []
        for tracked in self.images.values():
            if tracked.image.name == name:
                result.append((tracked.last_used_at, tracked.image))

        # Sort by last_used_at descending
        result.sort(key=lambda x: x[0], reverse=True)
        return [img for _, img in result]

    def clear(self) -> None:
        """Clear all tracked images from the registry.

        This does NOT remove images from Docker, only from the registry's
        internal tracking. Use prune() to also remove from Docker.

        Example:
            >>> registry = ImageRegistry()
            >>> registry.get_or_build(...)
            >>> registry.clear()
            >>> len(registry.list_images())
            0
        """
        logger.info("Clearing registry (removing %d tracked images)", len(self.images))
        self.images.clear()

    def with_client(self, client: DockerClient) -> ImageRegistry:
        """Return a new ImageRegistry with the specified Docker client.

        Useful for testing with mock clients.

        Args:
            client: Docker client to use.

        Returns:
            New ImageRegistry instance with the client set.

        Example:
            >>> mock_client = MagicMock()
            >>> registry = ImageRegistry().with_client(mock_client)
        """
        new_registry = self.model_copy(deep=True)
        object.__setattr__(new_registry, "_client", client)
        return new_registry

    def __len__(self) -> int:
        """Return the number of tracked images.

        Returns:
            Number of images in the registry.
        """
        return len(self.images)

    def __contains__(self, full_tag: str) -> bool:
        """Check if an image is tracked by full tag.

        Args:
            full_tag: Full image tag to check.

        Returns:
            True if the image is tracked, False otherwise.
        """
        return full_tag in self.images
