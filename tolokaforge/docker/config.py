"""Configuration module for Docker Foundation Layer.

Provides the DockerConfig Pydantic model for configuring Docker operations.
No environment variables are read - all configuration is passed programmatically.

Example:
    >>> from tolokaforge.docker.config import DockerConfig
    >>> config = DockerConfig(wait_timeout_s=60.0, wait_poll_s=0.5)
    >>> config.wait_timeout_s
    60.0
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)


LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class DockerConfig(BaseModel):
    """Configuration for Docker foundation layer operations.

    This model defines the configuration settings for Docker operations including
    service wait timeouts, polling intervals, default resource limits, and logging.
    All configuration is passed programmatically - no environment variables are read.

    Attributes:
        wait_timeout_s: Total timeout for waiting on services in seconds.
        wait_poll_s: Interval between health check polls in seconds.
        default_memory_limit: Default memory limit for containers (e.g., "256m", "1g").
        default_cpu_limit: Default CPU limit for containers (e.g., 0.5, 2.0).
        log_level: Logging level for docker operations.

    Example:
        >>> config = DockerConfig(
        ...     wait_timeout_s=60.0,
        ...     wait_poll_s=0.5,
        ...     default_memory_limit="512m",
        ...     log_level="DEBUG",
        ... )
        >>> config.wait_timeout_s
        60.0
        >>> config.wait_poll_s
        0.5
    """

    wait_timeout_s: float = Field(
        default=120.0,
        gt=0.0,
        description="Total timeout for waiting on services in seconds",
    )
    wait_poll_s: float = Field(
        default=1.0,
        gt=0.0,
        description="Interval between health check polls in seconds",
    )
    default_memory_limit: str | None = Field(
        default=None,
        description="Default memory limit for containers (e.g., '256m', '1g')",
    )
    default_cpu_limit: float | None = Field(
        default=None,
        gt=0.0,
        description="Default CPU limit for containers (e.g., 0.5, 2.0)",
    )
    log_level: LogLevel = Field(
        default="INFO",
        description="Logging level for docker operations",
    )

    model_config = {
        "frozen": True,
        "extra": "forbid",
    }

    @field_validator("default_memory_limit")
    @classmethod
    def validate_memory_limit(cls, v: str | None) -> str | None:
        """Validate memory limit format.

        Accepts Docker memory format: number with optional suffix (b, k, m, g).
        Examples: '256m', '1g', '512000000', '100k'
        """
        if v is None:
            return v

        v = v.strip().lower()
        if not v:
            return None

        # Check for valid suffixes
        valid_suffixes = ("b", "k", "m", "g")
        if v[-1] in valid_suffixes:
            number_part = v[:-1]
        else:
            number_part = v

        # Validate the numeric part
        try:
            value = float(number_part)
            if value < 0:
                raise ValueError("Memory limit must be non-negative")
        except ValueError as e:
            raise ValueError(
                f"Invalid memory limit format: {v!r}. "
                "Expected format: number with optional suffix (b, k, m, g)"
            ) from e

        return v

    def configure_logging(self) -> None:
        """Configure logging for the docker module based on log_level.

        Sets the logging level for the tolokaforge.docker logger hierarchy.

        Example:
            >>> config = DockerConfig(log_level="DEBUG")
            >>> config.configure_logging()
        """
        docker_logger = logging.getLogger("tolokaforge.docker")
        level = getattr(logging, self.log_level)
        docker_logger.setLevel(level)
        logger.debug("Docker logging configured to level %s", self.log_level)

    def with_timeout(self, wait_timeout_s: float) -> DockerConfig:
        """Return a new config with the specified wait timeout.

        Args:
            wait_timeout_s: New timeout value in seconds.

        Returns:
            New DockerConfig with the timeout set.

        Example:
            >>> config = DockerConfig()
            >>> config_with_timeout = config.with_timeout(60.0)
            >>> config_with_timeout.wait_timeout_s
            60.0
        """
        return self.model_copy(update={"wait_timeout_s": wait_timeout_s})

    def with_poll_interval(self, wait_poll_s: float) -> DockerConfig:
        """Return a new config with the specified poll interval.

        Args:
            wait_poll_s: New poll interval in seconds.

        Returns:
            New DockerConfig with the poll interval set.

        Example:
            >>> config = DockerConfig()
            >>> config_with_poll = config.with_poll_interval(0.5)
            >>> config_with_poll.wait_poll_s
            0.5
        """
        return self.model_copy(update={"wait_poll_s": wait_poll_s})

    def with_memory_limit(self, default_memory_limit: str) -> DockerConfig:
        """Return a new config with the specified default memory limit.

        Args:
            default_memory_limit: Memory limit in Docker format (e.g., "256m", "1g").

        Returns:
            New DockerConfig with the memory limit set.

        Example:
            >>> config = DockerConfig()
            >>> config_with_mem = config.with_memory_limit("512m")
            >>> config_with_mem.default_memory_limit
            '512m'
        """
        return self.model_copy(update={"default_memory_limit": default_memory_limit})

    def with_cpu_limit(self, default_cpu_limit: float) -> DockerConfig:
        """Return a new config with the specified default CPU limit.

        Args:
            default_cpu_limit: CPU cores limit (e.g., 0.5, 2.0).

        Returns:
            New DockerConfig with the CPU limit set.

        Example:
            >>> config = DockerConfig()
            >>> config_with_cpu = config.with_cpu_limit(2.0)
            >>> config_with_cpu.default_cpu_limit
            2.0
        """
        return self.model_copy(update={"default_cpu_limit": default_cpu_limit})
