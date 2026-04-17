"""Image module for Docker Foundation Layer.

Handles building Docker images with content-hash based caching. Instead of relying
on `latest` tags (which are unreliable for cache invalidation), the builder hashes
the Dockerfile + source context files and uses the hash as the image tag. If nothing
changed, the build is skipped entirely.

Uses Pydantic BaseModel for validation and serialization.

Async Support:
    Blocking operations have async counterparts (async_build, async_remove)
    that use anyio.to_thread.run_sync() to run the blocking Docker SDK calls
    in a thread pool, making them safe to use in async contexts.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import anyio
from docker.errors import APIError, BuildError, DockerException, ImageNotFound
from pydantic import BaseModel, Field, PrivateAttr, field_validator

import docker

if TYPE_CHECKING:
    from docker.models.images import Image as DockerImage

    from docker import DockerClient

logger = logging.getLogger(__name__)


class ImageError(Exception):
    """Raised when an image operation fails."""

    def __init__(self, operation: str, image_name: str, message: str):
        self.operation = operation
        self.image_name = image_name
        super().__init__(f"Image {operation} failed for '{image_name}': {message}")


class Image(BaseModel):
    """Represents a built Docker image with content-hash tagging.

    This model represents a Docker image configuration and provides methods
    for building, checking existence, computing content hashes, and removing images.
    Content-hash based tagging ensures that images are only rebuilt when the
    Dockerfile or context files change.

    Attributes:
        name: Base name of the image (e.g., "tolokaforge-executor").
        tag: Hash-based tag (e.g., "a3b8f2c1").
        image_id: Docker image ID (set after build).
        dockerfile: Path to Dockerfile used.
        context: Path to build context directory.
        context_hash: SHA256 of Dockerfile + context files.
        build_args: Build arguments passed to docker build.

    Example:
        >>> image = Image.build(
        ...     dockerfile="docker/executor.Dockerfile",
        ...     context=".",
        ...     name="tolokaforge-executor",
        ... )
        >>> image.full_tag
        'tolokaforge-executor:a3b8f2c1'
        >>> image.exists()
        True
    """

    name: str = Field(
        description="Base name of the image (e.g., 'tolokaforge-executor')",
    )
    tag: str = Field(
        description="Hash-based tag (e.g., 'a3b8f2c1')",
    )
    image_id: str | None = Field(
        default=None,
        description="Docker image ID (set after build)",
    )
    dockerfile: str = Field(
        description="Path to Dockerfile used",
    )
    context: str = Field(
        description="Path to build context directory",
    )
    context_hash: str = Field(
        description="SHA256 of Dockerfile + context files",
    )
    build_args: dict[str, str] = Field(
        default_factory=dict,
        description="Build arguments passed to docker build",
    )

    # Private attribute for Docker client (not serialized)
    _client: DockerClient | None = PrivateAttr(default=None)

    model_config = {
        "frozen": True,
        "extra": "forbid",
    }

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        """Validate that image name is not empty and follows Docker naming rules."""
        v = v.strip()
        if not v:
            raise ValueError("Image name cannot be empty")
        # Docker image names can contain lowercase letters, digits, and separators (., _, -)
        # but cannot start with a separator
        if v.startswith(("-", ".", "_")):
            raise ValueError(f"Image name cannot start with '-', '.', or '_', got: {v!r}")
        return v.lower()

    @field_validator("tag")
    @classmethod
    def validate_tag(cls, v: str) -> str:
        """Validate that tag is not empty."""
        v = v.strip()
        if not v:
            raise ValueError("Image tag cannot be empty")
        return v

    @field_validator("dockerfile")
    @classmethod
    def validate_dockerfile(cls, v: str) -> str:
        """Validate that dockerfile path is not empty."""
        v = v.strip()
        if not v:
            raise ValueError("Dockerfile path cannot be empty")
        return v

    @field_validator("context")
    @classmethod
    def validate_context(cls, v: str) -> str:
        """Validate that context path is not empty."""
        v = v.strip()
        if not v:
            raise ValueError("Context path cannot be empty")
        return v

    @property
    def full_tag(self) -> str:
        """Return the full image tag (name:tag).

        Returns:
            Full image tag string.

        Example:
            >>> image = Image(name="myimage", tag="abc123", ...)
            >>> image.full_tag
            'myimage:abc123'
        """
        return f"{self.name}:{self.tag}"

    def _get_client(self) -> DockerClient:
        """Get or create Docker client.

        Returns:
            Docker client instance.

        Raises:
            ImageError: If Docker client cannot be created.
        """
        if self._client is None:
            try:
                # Use object.__setattr__ because model is frozen
                object.__setattr__(self, "_client", docker.from_env())
            except DockerException as e:
                raise ImageError("connect", self.name, f"Failed to connect to Docker: {e}") from e
        return cast("DockerClient", self._client)

    def _get_docker_image(self) -> DockerImage:
        """Get the Docker image object.

        Returns:
            Docker image object.

        Raises:
            ImageError: If image doesn't exist.
        """
        client = self._get_client()
        try:
            return client.images.get(self.full_tag)
        except ImageNotFound as e:
            raise ImageError("get", self.full_tag, f"Image not found: {self.full_tag}") from e
        except APIError as e:
            raise ImageError("get", self.full_tag, f"Docker API error: {e}") from e

    @classmethod
    def build(
        cls,
        dockerfile: str,
        context: str,
        build_args: dict[str, str] | None = None,
        name: str | None = None,
        client: DockerClient | None = None,
    ) -> Image:
        """Build an image from a Dockerfile.

        Computes content hash first — skips build if a matching image exists.
        The hash is computed from the Dockerfile content and all files in the
        context directory.

        Args:
            dockerfile: Path to the Dockerfile.
            context: Path to the build context directory.
            build_args: Optional build arguments.
            name: Base name for the image. If not provided, derived from Dockerfile name.
            client: Optional Docker client (for testing/mocking).

        Returns:
            Image instance with image_id set.

        Raises:
            ImageError: If build fails or paths are invalid.

        Example:
            >>> image = Image.build(
            ...     dockerfile="docker/executor.Dockerfile",
            ...     context=".",
            ...     name="tolokaforge-executor",
            ... )
            >>> image.exists()
            True
        """
        if client is None:
            try:
                client = docker.from_env()
            except DockerException as e:
                raise ImageError(
                    "build", name or "unknown", f"Failed to connect to Docker: {e}"
                ) from e

        build_args = build_args or {}

        # Resolve paths
        dockerfile_path = Path(dockerfile)
        context_path = Path(context)

        # Validate paths exist
        if not dockerfile_path.exists():
            raise ImageError("build", name or "unknown", f"Dockerfile not found: {dockerfile}")
        if not context_path.exists():
            raise ImageError("build", name or "unknown", f"Context directory not found: {context}")
        if not context_path.is_dir():
            raise ImageError("build", name or "unknown", f"Context must be a directory: {context}")

        # Derive name from Dockerfile if not provided
        if name is None:
            # e.g., "docker/executor.Dockerfile" -> "executor"
            name = dockerfile_path.stem.replace(".Dockerfile", "").replace("Dockerfile", "")
            if not name:
                name = "image"
            name = f"tolokaforge-{name}"

        # Compute content hash
        content_hash = cls._compute_content_hash(dockerfile_path, context_path, build_args)
        tag = content_hash[:8]  # Use first 8 chars of hash as tag

        logger.info(
            "Building image '%s:%s' from %s (context: %s)",
            name,
            tag,
            dockerfile,
            context,
        )

        # Check if image with this hash already exists
        full_tag = f"{name}:{tag}"
        existing_image = cls._find_existing_image(client, full_tag)
        if existing_image:
            logger.info("Image '%s' already exists, skipping build", full_tag)
            image = cls(
                name=name,
                tag=tag,
                image_id=existing_image.id,
                dockerfile=str(dockerfile_path),
                context=str(context_path),
                context_hash=content_hash,
                build_args=build_args,
            )
            object.__setattr__(image, "_client", client)
            return image

        # Build the image with retries for transient registry errors
        # (e.g. 504 Gateway Timeout, connection resets during layer pulls)
        from tenacity import retry, stop_after_attempt, wait_exponential

        @retry(
            stop=stop_after_attempt(5),
            wait=wait_exponential(multiplier=10, min=10, max=60),
            reraise=True,
            before_sleep=lambda rs: logger.warning(
                "Docker build attempt %d failed, retrying in %ds...",
                rs.attempt_number,
                rs.next_action.sleep,
            ),
        )
        def _build_with_retry() -> tuple:
            return client.images.build(
                path=str(context_path.absolute()),
                dockerfile=str(dockerfile_path.absolute()),
                tag=full_tag,
                buildargs=build_args,
                rm=True,
                forcerm=True,
            )

        try:
            logger.info("Building image '%s'...", full_tag)

            docker_image, build_logs = _build_with_retry()

            # Log build output
            for log_entry in build_logs:
                if isinstance(log_entry, dict):
                    if "stream" in log_entry:
                        stream = str(log_entry["stream"]).strip()
                        if stream:
                            logger.debug("Build: %s", stream)
                    elif "error" in log_entry:
                        logger.error("Build error: %s", log_entry["error"])

            logger.info("Successfully built image '%s' (ID: %s)", full_tag, docker_image.id)

            image = cls(
                name=name,
                tag=tag,
                image_id=docker_image.id,
                dockerfile=str(dockerfile_path),
                context=str(context_path),
                context_hash=content_hash,
                build_args=build_args,
            )
            object.__setattr__(image, "_client", client)
            return image

        except BuildError as e:
            # Extract build log for error message
            build_log = ""
            if hasattr(e, "build_log"):
                for log_entry in e.build_log:
                    if "stream" in log_entry:
                        build_log += log_entry["stream"]
                    elif "error" in log_entry:
                        build_log += f"ERROR: {log_entry['error']}\n"
            raise ImageError("build", full_tag, f"Build failed: {e.msg}\n{build_log}") from e
        except APIError as e:
            raise ImageError("build", full_tag, f"Docker API error: {e}") from e

    @classmethod
    def _find_existing_image(cls, client: DockerClient, full_tag: str) -> DockerImage | None:
        """Find an existing image by tag.

        This is a lookup function where "not found" is a normal response.
        Returns None on both ImageNotFound and API errors to support
        content-hash based caching (caller will build if None returned).

        Args:
            client: Docker client.
            full_tag: Full image tag to find.

        Returns:
            Docker image object if found, None otherwise.
        """
        try:
            return client.images.get(full_tag)
        except ImageNotFound:
            return None  # Expected case: image doesn't exist yet
        except APIError:
            # Treat API errors as "not found" - caller will attempt to build
            return None  # noqa: BLE001 - Lookup function, None is valid "not found"

    @classmethod
    def _compute_content_hash(
        cls,
        dockerfile_path: Path,
        context_path: Path,
        build_args: dict[str, str],
    ) -> str:
        """Compute SHA256 hash of Dockerfile + context files + build args.

        The hash is computed from:
        1. Dockerfile content
        2. All files in the context directory (sorted by path)
        3. Build arguments (sorted by key)

        Args:
            dockerfile_path: Path to the Dockerfile.
            context_path: Path to the build context directory.
            build_args: Build arguments.

        Returns:
            SHA256 hash string (64 hex characters).
        """
        hasher = hashlib.sha256()

        # Hash Dockerfile content
        dockerfile_content = dockerfile_path.read_bytes()
        hasher.update(b"DOCKERFILE:")
        hasher.update(dockerfile_content)

        # Hash all files in context directory (sorted for determinism)
        context_files = sorted(context_path.rglob("*"))
        for file_path in context_files:
            if file_path.is_file():
                # Skip common non-source files
                if cls._should_skip_file(file_path):
                    continue

                # Include relative path in hash for structure awareness
                rel_path = file_path.relative_to(context_path)
                hasher.update(f"FILE:{rel_path}:".encode())

                # Hash file content
                try:
                    hasher.update(file_path.read_bytes())
                except OSError as e:
                    logger.warning("Could not read file %s for hashing: %s", file_path, e)

        # Hash build arguments (sorted for determinism)
        if build_args:
            hasher.update(b"BUILDARGS:")
            for key in sorted(build_args.keys()):
                hasher.update(f"{key}={build_args[key]}".encode())

        return hasher.hexdigest()

    @classmethod
    def _should_skip_file(cls, file_path: Path) -> bool:
        """Check if a file should be skipped during hashing.

        Skips common non-source files like .git, __pycache__, etc.

        Args:
            file_path: Path to check.

        Returns:
            True if file should be skipped.
        """
        skip_patterns = {
            ".git",
            ".gitignore",
            "__pycache__",
            ".pyc",
            ".pyo",
            ".egg-info",
            ".eggs",
            "node_modules",
            ".venv",
            "venv",
            ".env",
            ".DS_Store",
            "Thumbs.db",
            ".idea",
            ".vscode",
            "*.log",
            "*.tmp",
            "*.swp",
        }

        # Check if any part of the path matches skip patterns
        for part in file_path.parts:
            if part in skip_patterns:
                return True
            # Check suffix patterns
            for pattern in skip_patterns:
                if pattern.startswith("*") and file_path.suffix == pattern[1:]:
                    return True

        return False

    def exists(self) -> bool:
        """Check if this image exists in the local Docker daemon.

        Returns:
            True if the image exists, False otherwise.

        Example:
            >>> image = Image.build(...)
            >>> image.exists()
            True
            >>> image.remove()
            >>> image.exists()
            False
        """
        client = self._get_client()
        try:
            client.images.get(self.full_tag)
            return True
        except ImageNotFound:
            return False
        # Let APIError propagate - callers should know about Docker daemon issues

    def content_hash(self) -> str:
        """Return the content hash (Dockerfile + context files).

        Returns:
            SHA256 hash string (64 hex characters).

        Example:
            >>> image = Image.build(...)
            >>> len(image.content_hash())
            64
        """
        return self.context_hash

    def remove(self, force: bool = False) -> None:
        """Remove this image from the local Docker daemon.

        Args:
            force: Force removal even if image is in use.

        Raises:
            ImageError: If removal fails (except for not found).

        Example:
            >>> image = Image.build(...)
            >>> image.remove()
            >>> image.exists()
            False
        """
        logger.info("Removing image '%s'", self.full_tag)

        client = self._get_client()
        try:
            client.images.remove(self.full_tag, force=force)
            logger.info("Successfully removed image '%s'", self.full_tag)
        except ImageNotFound:
            logger.info("Image '%s' already removed or doesn't exist", self.full_tag)
        except APIError as e:
            raise ImageError("remove", self.full_tag, f"Docker API error: {e}") from e

    def with_client(self, client: DockerClient) -> Image:
        """Return a new Image with the specified Docker client.

        Useful for testing with mock clients.

        Args:
            client: Docker client to use.

        Returns:
            New Image instance with the client set.
        """
        new_image = self.model_copy()
        object.__setattr__(new_image, "_client", client)
        return new_image

    def to_docker_image_config(self) -> dict[str, Any]:
        """Convert image to Docker SDK image configuration.

        Returns a dictionary with image information suitable for
        container creation.

        Returns:
            Dictionary with Docker image configuration.

        Example:
            >>> image = Image.build(...)
            >>> config = image.to_docker_image_config()
            >>> config["image"]
            'tolokaforge-executor:a3b8f2c1'
        """
        return {
            "image": self.full_tag,
            "name": self.name,
            "tag": self.tag,
            "image_id": self.image_id,
        }

    # =========================================================================
    # Async Methods
    # =========================================================================
    # These methods wrap the synchronous Docker SDK calls using anyio.to_thread.run_sync()
    # to make them safe for use in async contexts without blocking the event loop.

    @classmethod
    async def async_build(
        cls,
        dockerfile: str,
        context: str,
        build_args: dict[str, str] | None = None,
        name: str | None = None,
        client: DockerClient | None = None,
    ) -> Image:
        """Build an image from a Dockerfile asynchronously.

        This is the async version of build() that runs the blocking Docker SDK
        calls in a thread pool using anyio.to_thread.run_sync().

        Computes content hash first — skips build if a matching image exists.
        The hash is computed from the Dockerfile content and all files in the
        context directory.

        Args:
            dockerfile: Path to the Dockerfile.
            context: Path to the build context directory.
            build_args: Optional build arguments.
            name: Base name for the image. If not provided, derived from Dockerfile name.
            client: Optional Docker client (for testing/mocking).

        Returns:
            Image instance with image_id set.

        Raises:
            ImageError: If build fails or paths are invalid.

        Example:
            >>> image = await Image.async_build(
            ...     dockerfile="docker/executor.Dockerfile",
            ...     context=".",
            ...     name="tolokaforge-executor",
            ... )
        """
        return await anyio.to_thread.run_sync(
            lambda: cls.build(
                dockerfile=dockerfile,
                context=context,
                build_args=build_args,
                name=name,
                client=client,
            )
        )

    async def async_remove(self, force: bool = False) -> None:
        """Remove this image from the local Docker daemon asynchronously.

        This is the async version of remove() that runs the blocking Docker SDK
        calls in a thread pool using anyio.to_thread.run_sync().

        Args:
            force: Force removal even if image is in use.

        Raises:
            ImageError: If removal fails (except for not found).

        Example:
            >>> await image.async_remove()
        """
        await anyio.to_thread.run_sync(lambda: self.remove(force=force))
