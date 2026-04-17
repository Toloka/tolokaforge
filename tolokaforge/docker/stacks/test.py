"""Test service stack: Core with auto-allocated ports.

Designed for CI and integration testing where port conflicts must be avoided.
All host ports are set to "auto" for automatic allocation.

Example:
    >>> from tolokaforge.docker.stacks.test import test_stack
    >>> with test_stack() as stack:
    ...     db_url = stack.get_service_url("db-service", 8000)
    ...     # Run tests against db_url
"""

from __future__ import annotations

from tolokaforge.docker.config import DockerConfig
from tolokaforge.docker.policy import Capability, ResourcePolicy
from tolokaforge.docker.ports import PortConfig
from tolokaforge.docker.stack import ServiceDefinition, ServiceStack


def test_stack(
    config: DockerConfig | None = None,
) -> ServiceStack:
    """Create a test service stack with auto-allocated ports.

    All host ports are "auto" to avoid conflicts in CI environments.

    Args:
        config: Optional DockerConfig. Uses defaults if None.

    Returns:
        ServiceStack configured for testing.
    """
    stack = ServiceStack(
        config=config or DockerConfig(),
        prefix="tolokaforge-test",
    )

    # DB Service with auto port
    db_service = ServiceDefinition(
        name="db-service",
        image_name="tolokaforge-db-service",
        dockerfile="docker/db_service.Dockerfile",
        context=".",
        ports=[PortConfig(container_port=8000, host_port="auto")],
        environment={"PYTHONUNBUFFERED": "1"},
        networks=["test-net"],
    )

    # Runner with auto port
    runner = ServiceDefinition(
        name="runner",
        image_name="tolokaforge-runner",
        dockerfile="docker/runner.Dockerfile",
        context=".",
        ports=[PortConfig(container_port=50051, host_port="auto")],
        environment={
            "PYTHONUNBUFFERED": "1",
            "DB_SERVICE_URL": "http://tolokaforge-test-db-service:8000",
        },
        depends_on=["db-service"],
        resources=ResourcePolicy(
            cap_drop=[Capability.ALL],
            cap_add=[Capability.NET_BIND_SERVICE],
        ),
        networks=["test-net"],
    )

    stack.add_services([db_service, runner])
    return stack
