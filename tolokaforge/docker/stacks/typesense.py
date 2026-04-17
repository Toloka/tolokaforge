"""TypeSense service definition for use with ServiceStack.

Provides a factory function that creates a ServiceDefinition for
a TypeSense search server, replacing raw Docker SDK usage in
typesense_server.py.

Example:
    >>> from tolokaforge.docker.stacks.typesense import typesense_service
    >>> from tolokaforge.docker.stack import ServiceStack
    >>>
    >>> svc = typesense_service(port=8108, api_key="test-key")
    >>> stack = ServiceStack()
    >>> stack.add_service(svc)
    >>> stack.start_all()
"""

from __future__ import annotations

from tolokaforge.docker.health import HealthProbe
from tolokaforge.docker.mount import Mount
from tolokaforge.docker.ports import PortConfig
from tolokaforge.docker.stack import ServiceDefinition


def typesense_service(
    port: int | str = 8108,
    api_key: str = "test-key",
    data_dir: str | None = None,
    image_tag: str = "26.0",
    container_name_suffix: str = "",
) -> ServiceDefinition:
    """Create a TypeSense service definition.

    Args:
        port: Host port for TypeSense (int or "auto").
        api_key: TypeSense API key.
        data_dir: Optional host directory for data persistence.
            If provided, creates a bind mount to /data.
        image_tag: TypeSense image tag (default: "26.0").
        container_name_suffix: Optional suffix for container name
            to avoid conflicts (e.g., "-test").

    Returns:
        ServiceDefinition for TypeSense.
    """
    host_port: int | str = port
    if isinstance(port, str) and port == "auto":
        host_port = "auto"

    mounts: list[Mount] = []
    if data_dir:
        mounts.append(
            Mount.bind(
                host_path=data_dir,
                container_path="/data",
            )
        )

    name = f"typesense{container_name_suffix}"

    # For health probe, use the resolved port if available, otherwise default
    # The health probe URL will use the host port for localhost access
    probe_port = port if isinstance(port, int) else 8108

    return ServiceDefinition(
        name=name,
        image_name="typesense/typesense",
        use_prebuilt_image=True,
        prebuilt_tag=image_tag,
        ports=[PortConfig(container_port=8108, host_port=host_port)],
        mounts=mounts,
        command=[
            "--data-dir=/data",
            f"--api-key={api_key}",
            "--listen-port=8108",
            "--enable-cors",
        ],
        health_probe=HealthProbe.http(
            url=f"http://localhost:{probe_port}/health",
            timeout_s=30.0,
            interval_s=1.0,
        ),
    )
