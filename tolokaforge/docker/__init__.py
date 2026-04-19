"""Docker Foundation Layer for TolokaForge.

This module provides the foundation layer for Docker container management,
including image building, container lifecycle management, health probes,
mounts, networks, resource policies, configuration, port configuration,
secret management, and log routing.

Public API:
    - DockerConfig: Configuration for Docker operations
    - Image, ImageRegistry: Image building and registry management
    - Container, ContainerStatus, ExecResult: Container lifecycle and execution
    - HealthProbe, ProbeResult, HealthProbeError: Health checking
    - Mount, MountType: Volume and bind mounts
    - Network, NetworkError: Network management
    - PortConfig, PortAllocationError: Port mapping configuration
    - ResourcePolicy, Capability: Resource limits and security constraints
    - SecretManager, SecretConfig, SecretProvider: Secret management
    - LogRouter, LogRouterState, LogRouterError: Container log streaming
    - wait_for_services, ServiceTarget, ServiceWaitError: Service health waiting

Example:
    >>> from tolokaforge.docker import (
    ...     DockerConfig, Image, ImageRegistry, Container, ContainerStatus,
    ...     HealthProbe, Mount, Network, PortConfig, ResourcePolicy, Capability,
    ...     SecretManager, SecretConfig, wait_for_services, ServiceTarget,
    ...     LogRouter,
    ... )
    >>>
    >>> # Configure Docker operations
    >>> config = DockerConfig(wait_timeout_s=60.0, wait_poll_s=0.5)
    >>>
    >>> # Build an image
    >>> registry = ImageRegistry()
    >>> image = registry.get_or_build(
    ...     name="my-app",
    ...     dockerfile="docker/app.Dockerfile",
    ...     context=".",
    ... )
    >>>
    >>> # Create and start a container with ports and secrets
    >>> container = Container.create(
    ...     image=image,
    ...     name="my-container",
    ...     mounts=[Mount.volume("data", "/data")],
    ...     ports=[
    ...         PortConfig(container_port=8080, host_port=8080),
    ...         PortConfig(container_port=9090, host_port="auto"),  # Auto-allocate
    ...     ],
    ...     resources=ResourcePolicy.executor_default(),
    ...     secret_keys=["API_KEY", "DATABASE_URL"],  # Secrets resolved automatically
    ... )
    >>> container.start(trial_id="trial-001")  # Auto-creates LogRouter
    >>>
    >>> # Or manually create and attach a LogRouter
    >>> log_router = LogRouter.for_container(
    ...     container=container,
    ...     trial_id="trial-001",
    ...     log_file="/tmp/container.log",
    ... )
    >>> container.start(log_router=log_router)
    >>>
    >>> # Health check
    >>> result = container.health_check(
    ...     HealthProbe.http("http://localhost:8000/health")
    ... )
    >>>
    >>> # Wait for services
    >>> targets = [
    ...     ServiceTarget.http("JSON DB", "http://json-db:8000/health"),
    ...     ServiceTarget.grpc("Executor", "executor", 50051),
    ... ]
    >>> wait_for_services(config, targets)
    >>>
    >>> # Execute commands
    >>> exec_result = container.exec(["echo", "hello"])
    >>> print(exec_result.stdout)
    >>>
    >>> # Cleanup (LogRouter is stopped automatically)
    >>> container.stop()
    >>> container.destroy()
"""

from tolokaforge.docker.config import DockerConfig
from tolokaforge.docker.container import Container, ContainerStatus, ExecResult
from tolokaforge.docker.health import HealthProbe, HealthProbeError, ProbeResult
from tolokaforge.docker.image import Image
from tolokaforge.docker.logging import (
    ContainerLogAdapter,
    ContainerLogFormatter,
    LogRouter,
    LogRouterError,
    LogRouterState,
    setup_container_logging,
)
from tolokaforge.docker.mount import Mount, MountType
from tolokaforge.docker.network import Network, NetworkError
from tolokaforge.docker.policy import Capability, ResourcePolicy
from tolokaforge.docker.ports import PortAllocationError, PortConfig
from tolokaforge.docker.registry import ImageRegistry
from tolokaforge.docker.stack import ServiceDefinition, ServiceStack, ServiceStatus
from tolokaforge.docker.wait_for_services import (
    ServiceTarget,
    ServiceType,
    ServiceWaitError,
    wait_for_services,
)
from tolokaforge.secrets import (
    DotEnvProvider,
    EnvProvider,
    MissingSecretError,
    SecretConfig,
    SecretManager,
    SecretProvider,
)

__all__ = [
    # Configuration
    "DockerConfig",
    # Image management
    "Image",
    "ImageRegistry",
    # Container management
    "Container",
    "ContainerStatus",
    "ExecResult",
    # Health probes
    "HealthProbe",
    "ProbeResult",
    "HealthProbeError",
    # Mounts
    "Mount",
    "MountType",
    # Networks
    "Network",
    "NetworkError",
    # Port configuration
    "PortConfig",
    "PortAllocationError",
    # Resource policies
    "ResourcePolicy",
    "Capability",
    # Secret management
    "SecretManager",
    "SecretConfig",
    "SecretProvider",
    "EnvProvider",
    "DotEnvProvider",
    "MissingSecretError",
    # Log routing
    "LogRouter",
    "LogRouterState",
    "LogRouterError",
    "ContainerLogAdapter",
    "ContainerLogFormatter",
    "setup_container_logging",
    # Service waiting
    "wait_for_services",
    "ServiceTarget",
    "ServiceType",
    "ServiceWaitError",
    # Service stack
    "ServiceDefinition",
    "ServiceStack",
    "ServiceStatus",
]
