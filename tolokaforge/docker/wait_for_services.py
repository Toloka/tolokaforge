"""Blocking wait for Docker runtime dependencies.

This module provides functionality to wait for Docker services to become healthy
before proceeding with operations. It uses the HealthProbe system from health.py
for consistent health checking across the codebase.

Example:
    >>> from tolokaforge.docker.config import DockerConfig
    >>> from tolokaforge.docker.wait_for_services import wait_for_services, ServiceTarget
    >>>
    >>> config = DockerConfig(wait_timeout_s=60.0, wait_poll_s=0.5)
    >>> targets = [
    ...     ServiceTarget.http("JSON DB", "http://json-db:8000/health"),
    ...     ServiceTarget.http("Mock Web", "http://mock-web:8080/health"),
    ...     ServiceTarget.grpc("Executor", "executor", 50051),
    ... ]
    >>> wait_for_services(config, targets)
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from enum import Enum

from tolokaforge.docker.config import DockerConfig
from tolokaforge.docker.health import HealthProbe, HealthProbeError

logger = logging.getLogger(__name__)


class ServiceType(str, Enum):
    """Type of service health check.

    Values:
        HTTP: HTTP health endpoint check.
        GRPC: gRPC health check protocol.
    """

    HTTP = "http"
    GRPC = "grpc"


@dataclass(frozen=True)
class ServiceTarget:
    """Target service for health checking.

    Attributes:
        name: Human-readable service name for logging.
        service_type: Type of health check (HTTP or gRPC).
        url: URL for HTTP checks (e.g., "http://json-db:8000/health").
        host: Hostname for gRPC checks.
        port: Port number for gRPC checks.

    Example:
        >>> http_target = ServiceTarget.http("JSON DB", "http://json-db:8000/health")
        >>> grpc_target = ServiceTarget.grpc("Executor", "executor", 50051)
    """

    name: str
    service_type: ServiceType
    url: str | None = None
    host: str | None = None
    port: int | None = None

    @classmethod
    def http(cls, name: str, url: str) -> ServiceTarget:
        """Create an HTTP service target.

        Args:
            name: Human-readable service name.
            url: Health check URL (must include protocol).

        Returns:
            ServiceTarget configured for HTTP health checks.

        Example:
            >>> target = ServiceTarget.http("JSON DB", "http://json-db:8000/health")
            >>> target.service_type
            <ServiceType.HTTP: 'http'>
        """
        return cls(name=name, service_type=ServiceType.HTTP, url=url)

    @classmethod
    def grpc(cls, name: str, host: str, port: int) -> ServiceTarget:
        """Create a gRPC service target.

        Args:
            name: Human-readable service name.
            host: Hostname or IP address.
            port: Port number.

        Returns:
            ServiceTarget configured for gRPC health checks.

        Example:
            >>> target = ServiceTarget.grpc("Executor", "executor", 50051)
            >>> target.service_type
            <ServiceType.GRPC: 'grpc'>
        """
        return cls(name=name, service_type=ServiceType.GRPC, host=host, port=port)


class ServiceWaitError(Exception):
    """Raised when waiting for services fails.

    Attributes:
        service_name: Name of the service that failed.
        message: Error message.
    """

    def __init__(self, service_name: str, message: str):
        self.service_name = service_name
        super().__init__(f"Failed waiting for {service_name}: {message}")


def wait_for_services(
    config: DockerConfig,
    targets: list[ServiceTarget],
) -> None:
    """Wait for all services to become healthy.

    Uses HealthProbe from health.py for consistent health checking.
    The config's wait_timeout_s and wait_poll_s are propagated to each probe.

    Args:
        config: Docker configuration with timeout/poll settings.
        targets: List of ServiceTarget objects to check.

    Raises:
        ServiceWaitError: If any service fails to become healthy within timeout.

    Example:
        >>> config = DockerConfig(wait_timeout_s=60.0, wait_poll_s=0.5)
        >>> targets = [
        ...     ServiceTarget.http("JSON DB", "http://json-db:8000/health"),
        ...     ServiceTarget.grpc("Executor", "executor", 50051),
        ... ]
        >>> wait_for_services(config, targets)
    """
    # Configure logging based on config
    config.configure_logging()

    logger.info(
        "Waiting for %d services (timeout=%ss, poll=%ss)",
        len(targets),
        config.wait_timeout_s,
        config.wait_poll_s,
    )

    for target in targets:
        _wait_for_target(config, target)

    logger.info("All %d services healthy", len(targets))


def _wait_for_target(config: DockerConfig, target: ServiceTarget) -> None:
    """Wait for a single service target to become healthy.

    Args:
        config: Docker configuration with timeout/poll settings.
        target: Service target to check.

    Raises:
        ServiceWaitError: If the service fails to become healthy.
    """
    logger.info("Checking %s (%s)", target.name, target.service_type.value)

    try:
        if target.service_type == ServiceType.HTTP:
            if target.url is None:
                raise ServiceWaitError(target.name, "HTTP target requires url")
            probe = HealthProbe.http(
                url=target.url,
                interval_s=config.wait_poll_s,
                timeout_s=config.wait_timeout_s,
            )
        elif target.service_type == ServiceType.GRPC:
            if target.host is None or target.port is None:
                raise ServiceWaitError(target.name, "gRPC target requires host and port")
            probe = HealthProbe.grpc(
                host=target.host,
                port=target.port,
                interval_s=config.wait_poll_s,
                timeout_s=config.wait_timeout_s,
            )
        else:
            raise ServiceWaitError(target.name, f"Unknown service type: {target.service_type}")

        result = probe.wait()
        logger.info(
            "%s healthy after %d attempts (%.2fs)",
            target.name,
            result.attempts,
            result.elapsed_s,
        )

    except HealthProbeError as e:
        logger.error("%s failed: %s", target.name, e)
        raise ServiceWaitError(target.name, str(e)) from e


def main(
    config: DockerConfig,
    targets: list[ServiceTarget],
) -> int:
    """Entry point for wait_for_services.

    This function should be called programmatically by the upper layer
    (e.g., orchestrator) with explicit config and targets. It is not
    intended to be run directly as a script.

    Args:
        config: Docker configuration (required).
        targets: Service targets to check (required).

    Returns:
        0 on success, 1 on failure.

    Example:
        >>> from tolokaforge.docker.config import DockerConfig
        >>> from tolokaforge.docker.wait_for_services import main, ServiceTarget
        >>>
        >>> config = DockerConfig(wait_timeout_s=60.0)
        >>> targets = [ServiceTarget.http("API", "http://api:8000/health")]
        >>> exit_code = main(config, targets)
    """
    try:
        wait_for_services(config, targets)
        return 0
    except ServiceWaitError as e:
        logger.error("Fatal: %s", e)
        return 1


if __name__ == "__main__":
    print(
        "Error: wait_for_services.py should not be run directly.\n"
        "This module must be called programmatically by the upper layer.\n"
        "\n"
        "Example usage:\n"
        "    from tolokaforge.docker.config import DockerConfig\n"
        "    from tolokaforge.docker.wait_for_services import main, ServiceTarget\n"
        "\n"
        "    config = DockerConfig(wait_timeout_s=60.0)\n"
        "    targets = [ServiceTarget.http('API', 'http://api:8000/health')]\n"
        "    exit_code = main(config, targets)",
        file=sys.stderr,
    )
    sys.exit(1)
