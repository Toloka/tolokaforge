"""Mount module for Docker Foundation Layer.

Provides abstractions for different types of volume mounts: named volumes,
bind mounts, MCP config mounts, and per-trial workspace volumes.
Uses Pydantic BaseModel for validation and serialization.
"""

from __future__ import annotations

import json
import re
import tempfile
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class MountType(str, Enum):
    """Types of Docker mounts.

    This enum provides type-safe mount type names for use with Docker SDK.
    Values are lowercase strings matching Docker mount types.
    """

    VOLUME = "volume"
    BIND = "bind"
    TMPFS = "tmpfs"


# Regex pattern for valid trial IDs: alphanumeric, underscores, hyphens
# Must start with alphanumeric, 1-128 characters
TRIAL_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")


class Mount(BaseModel):
    """Base mount specification for Docker containers.

    This model defines a mount configuration that can be applied when creating
    a Docker container. Supports named volumes, bind mounts, and tmpfs mounts.

    Attributes:
        source: Host path or volume name (empty for tmpfs).
        target: Container path where the mount will be attached.
        read_only: Whether the mount is read-only.
        mount_type: Type of mount (volume, bind, tmpfs).

    Example:
        >>> mount = Mount.volume("my-data", "/data")
        >>> mount.target
        '/data'
        >>> mount.mount_type
        <MountType.VOLUME: 'volume'>
    """

    source: str = Field(
        description="Host path or volume name (empty for tmpfs)",
    )
    target: str = Field(
        description="Container path where the mount will be attached",
    )
    read_only: bool = Field(
        default=False,
        description="Whether the mount is read-only",
    )
    mount_type: MountType = Field(
        description="Type of mount (volume, bind, tmpfs)",
    )

    model_config = {
        "frozen": True,
        "extra": "forbid",
    }

    @field_validator("target")
    @classmethod
    def validate_target(cls, v: str) -> str:
        """Validate that target path is not empty and is absolute."""
        v = v.strip()
        if not v:
            raise ValueError("Container path (target) cannot be empty")
        if not v.startswith("/"):
            raise ValueError(f"Container path must be absolute, got: {v!r}")
        return v

    @model_validator(mode="after")
    def validate_source_for_type(self) -> Mount:
        """Validate source based on mount type."""
        if self.mount_type == MountType.BIND:
            if not self.source:
                raise ValueError("Bind mount requires a non-empty host path (source)")
            # Bind mounts should have absolute paths
            if not self.source.startswith("/"):
                raise ValueError(f"Bind mount source must be absolute path, got: {self.source!r}")
        elif self.mount_type == MountType.VOLUME:
            if not self.source:
                raise ValueError("Volume mount requires a non-empty volume name (source)")
            # Volume names should not contain path separators
            if "/" in self.source:
                raise ValueError(f"Volume name cannot contain '/', got: {self.source!r}")
        # tmpfs doesn't require source validation
        return self

    def to_docker_mount(self) -> dict[str, Any]:
        """Convert mount to Docker SDK mount specification.

        Returns a dictionary suitable for passing to docker-py's
        create_container() or run() methods as a mount specification.

        Returns:
            Dictionary with Docker mount configuration.

        Example:
            >>> mount = Mount.volume("my-data", "/data", read_only=True)
            >>> config = mount.to_docker_mount()
            >>> config["Type"]
            'volume'
            >>> config["ReadOnly"]
            True
        """
        config: dict[str, Any] = {
            "Type": self.mount_type.value,
            "Target": self.target,
            "ReadOnly": self.read_only,
        }

        if self.mount_type == MountType.VOLUME or self.mount_type == MountType.BIND:
            config["Source"] = self.source
        # tmpfs doesn't need Source

        return config

    @classmethod
    def volume(
        cls,
        name: str,
        container_path: str,
        read_only: bool = False,
    ) -> Mount:
        """Create a named Docker volume mount.

        Named volumes are managed by Docker and persist across container restarts.
        They are the preferred way to persist data.

        Args:
            name: Name of the Docker volume.
            container_path: Path inside the container where the volume is mounted.
            read_only: Whether the mount is read-only.

        Returns:
            Mount configured as a named volume.

        Example:
            >>> mount = Mount.volume("app-data", "/data")
            >>> mount.source
            'app-data'
            >>> mount.mount_type
            <MountType.VOLUME: 'volume'>
        """
        return cls(
            source=name,
            target=container_path,
            read_only=read_only,
            mount_type=MountType.VOLUME,
        )

    @classmethod
    def bind(
        cls,
        host_path: str,
        container_path: str,
        read_only: bool = False,
    ) -> Mount:
        """Create a bind mount from host filesystem.

        Bind mounts map a host directory or file directly into the container.
        Changes are immediately visible on both sides.

        Args:
            host_path: Absolute path on the host filesystem.
            container_path: Path inside the container where the host path is mounted.
            read_only: Whether the mount is read-only.

        Returns:
            Mount configured as a bind mount.

        Example:
            >>> mount = Mount.bind("/host/data", "/container/data", read_only=True)
            >>> mount.source
            '/host/data'
            >>> mount.mount_type
            <MountType.BIND: 'bind'>
        """
        return cls(
            source=host_path,
            target=container_path,
            read_only=read_only,
            mount_type=MountType.BIND,
        )

    @classmethod
    def mcp(
        cls,
        config: dict[str, Any],
        container_path: str = "/app/mcp/config.json",
    ) -> Mount:
        """Create an MCP configuration mount.

        Serializes the MCP config dict to a temporary file on the host
        and bind-mounts it into the container as read-only.

        Note: The temporary file is created in the system temp directory.
        The caller is responsible for cleanup if needed, though the file
        will be cleaned up when the system cleans temp files.

        Args:
            config: MCP configuration dictionary to serialize.
            container_path: Path inside the container for the config file.

        Returns:
            Mount configured as a read-only bind mount of the serialized config.

        Raises:
            ValueError: If config is empty or cannot be serialized.

        Example:
            >>> config = {"mcpServers": {"server1": {"command": "node"}}}
            >>> mount = Mount.mcp(config)
            >>> mount.target
            '/app/mcp/config.json'
            >>> mount.read_only
            True
        """
        if not config:
            raise ValueError("MCP config cannot be empty")

        # Serialize config to a temporary file
        # Using delete=False so the file persists for the container to read
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".json",
                prefix="mcp_config_",
                delete=False,
            ) as f:
                json.dump(config, f, indent=2)
                temp_path = f.name
        except (TypeError, ValueError) as e:
            raise ValueError(f"Failed to serialize MCP config: {e}") from e

        return cls(
            source=temp_path,
            target=container_path,
            read_only=True,
            mount_type=MountType.BIND,
        )

    @classmethod
    def workspace(
        cls,
        trial_id: str,
        container_path: str = "/work",
    ) -> Mount:
        """Create a per-trial workspace volume mount.

        Creates a named volume scoped to the trial ID. This provides isolated
        workspace storage for each trial. Cleanup is the caller's responsibility.

        Args:
            trial_id: Unique identifier for the trial. Must be alphanumeric with
                underscores and hyphens allowed, 1-128 characters.
            container_path: Path inside the container for the workspace.

        Returns:
            Mount configured as a named volume for the trial workspace.

        Raises:
            ValueError: If trial_id is invalid.

        Example:
            >>> mount = Mount.workspace("trial_001")
            >>> mount.source
            'tolokaforge-workspace-trial_001'
            >>> mount.target
            '/work'
        """
        trial_id = trial_id.strip()
        if not trial_id:
            raise ValueError("trial_id cannot be empty")
        if not TRIAL_ID_PATTERN.match(trial_id):
            raise ValueError(
                f"Invalid trial_id: {trial_id!r}. Must be alphanumeric with underscores "
                "and hyphens allowed, starting with alphanumeric, 1-128 characters."
            )

        volume_name = f"tolokaforge-workspace-{trial_id}"
        return cls(
            source=volume_name,
            target=container_path,
            read_only=False,
            mount_type=MountType.VOLUME,
        )

    @classmethod
    def tmpfs(
        cls,
        container_path: str,
    ) -> Mount:
        """Create a tmpfs mount (in-memory filesystem).

        Tmpfs mounts are stored in memory and are lost when the container stops.
        Useful for sensitive data that shouldn't be persisted.

        Args:
            container_path: Path inside the container for the tmpfs mount.

        Returns:
            Mount configured as a tmpfs mount.

        Example:
            >>> mount = Mount.tmpfs("/tmp")
            >>> mount.mount_type
            <MountType.TMPFS: 'tmpfs'>
        """
        return cls(
            source="",
            target=container_path,
            read_only=False,
            mount_type=MountType.TMPFS,
        )

    def with_read_only(self, read_only: bool = True) -> Mount:
        """Return a new mount with the specified read_only setting.

        Args:
            read_only: Whether the mount should be read-only.

        Returns:
            New Mount with the read_only setting updated.

        Example:
            >>> mount = Mount.volume("data", "/data")
            >>> ro_mount = mount.with_read_only(True)
            >>> ro_mount.read_only
            True
        """
        return self.model_copy(update={"read_only": read_only})

    def get_temp_file_path(self) -> Path | None:
        """Get the temporary file path if this is an MCP mount.

        Returns the path to the temporary config file for MCP mounts,
        or None for other mount types.

        Returns:
            Path to temp file for MCP mounts, None otherwise.
        """
        if self.mount_type == MountType.BIND and self.source.startswith(tempfile.gettempdir()):
            return Path(self.source)
        return None

    def cleanup_temp_file(self) -> bool:
        """Clean up the temporary file if this is an MCP mount.

        Removes the temporary config file created by Mount.mcp().
        Safe to call on any mount type - returns False if not applicable.

        Returns:
            True if a temp file was removed, False otherwise.
        """
        temp_path = self.get_temp_file_path()
        if temp_path and temp_path.exists():
            temp_path.unlink()
            return True
        return False
