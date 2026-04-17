"""Port configuration module for Docker Foundation Layer.

Provides PortConfig model for type-safe port mapping configuration with
support for automatic host port allocation using socket binding.
"""

from __future__ import annotations

import logging
import socket
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)


class PortAllocationError(Exception):
    """Raised when port allocation fails."""

    def __init__(self, port: int, message: str):
        self.port = port
        super().__init__(f"Port allocation failed for port {port}: {message}")


class PortConfig(BaseModel):
    """Configuration for a single port mapping.

    Supports both explicit host port specification and automatic allocation.
    When host_port is "auto", the resolve() method will find an available port
    using socket binding.

    Attributes:
        container_port: Port number inside the container (required).
        host_port: Host port number or "auto" for automatic allocation.
        protocol: Protocol for the port mapping (tcp or udp).

    Example:
        >>> # Explicit port mapping
        >>> port = PortConfig(container_port=8080, host_port=8080)
        >>> port.to_docker_format()
        {'8080/tcp': 8080}

        >>> # Auto-allocated port
        >>> port = PortConfig(container_port=8080, host_port="auto")
        >>> resolved = port.resolve()
        >>> resolved.host_port  # Some available port like 49152
        49152
    """

    container_port: int = Field(
        description="Port number inside the container",
        ge=1,
        le=65535,
    )
    host_port: int | Literal["auto"] = Field(
        default="auto",
        description="Host port number or 'auto' for automatic allocation",
    )
    protocol: str = Field(
        default="tcp",
        description="Protocol for the port mapping (tcp or udp)",
    )

    model_config = {
        "frozen": True,
        "extra": "forbid",
    }

    @field_validator("host_port")
    @classmethod
    def validate_host_port(cls, v: int | Literal["auto"]) -> int | Literal["auto"]:
        """Validate host_port is either 'auto' or a valid port number."""
        if v == "auto":
            return v
        if isinstance(v, int):
            if v < 1 or v > 65535:
                raise ValueError(f"Host port must be between 1 and 65535, got: {v}")
            return v
        raise ValueError(f"Host port must be an integer or 'auto', got: {v!r}")

    @field_validator("protocol")
    @classmethod
    def validate_protocol(cls, v: str) -> str:
        """Validate protocol is tcp or udp."""
        v = v.lower()
        if v not in ("tcp", "udp"):
            raise ValueError(f"Protocol must be 'tcp' or 'udp', got: {v!r}")
        return v

    def resolve(self) -> PortConfig:
        """Resolve auto-allocated port to an actual available port.

        If host_port is already an integer, returns self unchanged.
        If host_port is "auto", finds an available port by binding to port 0
        and returns a new PortConfig with the allocated port.

        Returns:
            PortConfig with resolved host_port (always an integer).

        Raises:
            PortAllocationError: If port allocation fails.

        Example:
            >>> port = PortConfig(container_port=8080, host_port="auto")
            >>> resolved = port.resolve()
            >>> isinstance(resolved.host_port, int)
            True
        """
        if isinstance(self.host_port, int):
            # Verify the explicit port is available
            self._verify_port_available(self.host_port)
            return self

        # Auto-allocate a port
        allocated_port = self._allocate_port()
        logger.debug(
            "Auto-allocated host port %d for container port %d/%s",
            allocated_port,
            self.container_port,
            self.protocol,
        )
        return PortConfig(
            container_port=self.container_port,
            host_port=allocated_port,
            protocol=self.protocol,
        )

    def _allocate_port(self) -> int:
        """Allocate an available port using socket binding.

        Binds to port 0 to let the OS assign an available port,
        then immediately releases it.

        Returns:
            Available port number.

        Raises:
            PortAllocationError: If allocation fails.
        """
        sock_type = socket.SOCK_STREAM if self.protocol == "tcp" else socket.SOCK_DGRAM
        try:
            with socket.socket(socket.AF_INET, sock_type) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("", 0))
                _, port = sock.getsockname()
                return port
        except OSError as e:
            raise PortAllocationError(0, f"Failed to allocate port: {e}") from e

    def _verify_port_available(self, port: int) -> None:
        """Verify that a specific port is available.

        Args:
            port: Port number to check.

        Raises:
            PortAllocationError: If the port is not available.
        """
        sock_type = socket.SOCK_STREAM if self.protocol == "tcp" else socket.SOCK_DGRAM
        try:
            with socket.socket(socket.AF_INET, sock_type) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("", port))
        except OSError as e:
            raise PortAllocationError(port, f"Port {port} is not available: {e}") from e

    def to_docker_format(self) -> dict[str, Any]:
        """Convert to Docker SDK port mapping format.

        The Docker SDK expects port mappings in the format:
        {"container_port/protocol": host_port}

        Returns:
            Dictionary in Docker SDK format.

        Raises:
            ValueError: If host_port is still "auto" (not resolved).

        Example:
            >>> port = PortConfig(container_port=8080, host_port=8080)
            >>> port.to_docker_format()
            {'8080/tcp': 8080}
        """
        if self.host_port == "auto":
            raise ValueError(
                "Cannot convert to Docker format: host_port is 'auto'. "
                "Call resolve() first to allocate a port."
            )
        return {f"{self.container_port}/{self.protocol}": self.host_port}


def resolve_ports(ports: list[PortConfig]) -> list[PortConfig]:
    """Resolve all ports in a list, allocating auto ports.

    Args:
        ports: List of PortConfig instances.

    Returns:
        List of PortConfig instances with all host_ports resolved to integers.

    Raises:
        PortAllocationError: If any port allocation fails.

    Example:
        >>> ports = [
        ...     PortConfig(container_port=8080, host_port="auto"),
        ...     PortConfig(container_port=9090, host_port=9090),
        ... ]
        >>> resolved = resolve_ports(ports)
        >>> all(isinstance(p.host_port, int) for p in resolved)
        True
    """
    return [port.resolve() for port in ports]


def ports_to_docker_format(ports: list[PortConfig]) -> dict[str, int]:
    """Convert a list of PortConfig to Docker SDK format.

    Args:
        ports: List of resolved PortConfig instances.

    Returns:
        Dictionary in Docker SDK format for all ports.

    Raises:
        ValueError: If any port has host_port still set to "auto".

    Example:
        >>> ports = [
        ...     PortConfig(container_port=8080, host_port=8080),
        ...     PortConfig(container_port=9090, host_port=9090, protocol="udp"),
        ... ]
        >>> ports_to_docker_format(ports)
        {'8080/tcp': 8080, '9090/udp': 9090}
    """
    result: dict[str, int] = {}
    for port in ports:
        result.update(port.to_docker_format())
    return result
