"""Service Stack module for Docker Foundation Layer.

Composes foundation-layer primitives (Container, Image, Network, HealthProbe,
PortConfig, Mount, ResourcePolicy) into a high-level service stack that manages
a collection of services with dependency ordering, health checking, and lifecycle
management.

This is the primary orchestration layer that replaces docker-compose.yaml and
bash Docker management scripts.

Example:
    >>> from tolokaforge.docker.stack import ServiceDefinition, ServiceStack
    >>> from tolokaforge.docker import HealthProbe, PortConfig, Mount
    >>>
    >>> svc = ServiceDefinition(
    ...     name="db-service",
    ...     image_name="tolokaforge-db-service",
    ...     dockerfile="docker/db_service.Dockerfile",
    ...     ports=[PortConfig(container_port=8000, host_port=8000)],
    ...     health_probe=HealthProbe.http("http://localhost:8000/health"),
    ... )
    >>> stack = ServiceStack()
    >>> stack.add_service(svc)
    >>> stack.start_all(wait=True)
    >>> stack.stop_all()
    >>> stack.destroy()
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr

from tolokaforge.docker.config import DockerConfig
from tolokaforge.docker.container import Container, ContainerStatus
from tolokaforge.docker.health import HealthProbe, HealthProbeError
from tolokaforge.docker.image import Image
from tolokaforge.docker.mount import Mount
from tolokaforge.docker.network import Network
from tolokaforge.docker.policy import ResourcePolicy
from tolokaforge.docker.ports import PortConfig, resolve_ports
from tolokaforge.docker.registry import ImageRegistry

logger = logging.getLogger(__name__)


class ServiceDefinition(BaseModel):
    """Frozen Pydantic model describing a single service in a stack.

    A ServiceDefinition captures all the configuration needed to build
    an image, create a container, and start a service. It is declarative
    and immutable — the ServiceStack uses it to orchestrate lifecycle.

    Attributes:
        name: Unique service name (used as container name prefix).
        image_name: Base name for the Docker image.
        dockerfile: Path to Dockerfile (relative to context).
        context: Build context directory.
        build_args: Docker build arguments.
        ports: Port mappings for the container.
        mounts: Volume/bind mounts for the container.
        environment: Environment variables for the container.
        health_probe: Optional health probe configuration.
        resources: Optional resource policy (CPU/memory limits).
        depends_on: Names of services this service depends on.
        networks: Network names to attach to.
        command: Container command override.
        profiles: Profile tags for selective startup.
        use_prebuilt_image: If True, skip building and use existing image.
        prebuilt_tag: Tag for prebuilt image (default: "latest").

    Example:
        >>> svc = ServiceDefinition(
        ...     name="db-service",
        ...     image_name="tolokaforge-db-service",
        ...     dockerfile="docker/db_service.Dockerfile",
        ...     ports=[PortConfig(container_port=8000, host_port=8000)],
        ...     environment={"PYTHONUNBUFFERED": "1"},
        ... )
    """

    name: str = Field(description="Unique service name")
    image_name: str = Field(description="Base name for the Docker image")
    dockerfile: str = Field(default="", description="Path to Dockerfile")
    context: str = Field(default=".", description="Build context directory")
    build_args: dict[str, str] = Field(
        default_factory=dict,
        description="Docker build arguments",
    )
    ports: list[PortConfig] = Field(
        default_factory=list,
        description="Port mappings for the container",
    )
    mounts: list[Mount] = Field(
        default_factory=list,
        description="Volume/bind mounts for the container",
    )
    environment: dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables for the container",
    )
    health_probe: HealthProbe | None = Field(
        default=None,
        description="Health probe configuration",
    )
    resources: ResourcePolicy | None = Field(
        default=None,
        description="Resource policy (CPU/memory limits)",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="Names of services this service depends on",
    )
    networks: list[str] = Field(
        default_factory=list,
        description="Network names to attach to",
    )
    command: str | list[str] | None = Field(
        default=None,
        description="Container command override",
    )
    profiles: list[str] = Field(
        default_factory=list,
        description="Profile tags for selective startup",
    )
    use_prebuilt_image: bool = Field(
        default=False,
        description="If True, skip building and use existing image",
    )
    prebuilt_tag: str = Field(
        default="latest",
        description="Tag for prebuilt image",
    )
    context_files: list[str] = Field(
        default_factory=list,
        description="Explicit list of files/dirs to include in build context. "
        "When set, an isolated temp build directory is created instead of using context.",
    )
    privileged: bool = Field(
        default=False,
        description="Run in privileged mode (required for Docker-in-Docker).",
    )
    secret_keys: list[str] = Field(
        default_factory=list,
        description="Secret env var names to resolve via SecretManager and inject into the container.",
    )

    model_config = {
        "frozen": True,
        "extra": "forbid",
    }


class ServiceStatus(BaseModel):
    """Runtime status of a service in the stack.

    Attributes:
        name: Service name.
        container_id: Docker container ID (None if not created).
        status: Current container status string.
        health: Latest health probe result (None if no probe configured).
        ports: Resolved port mappings.
    """

    name: str = Field(description="Service name")
    container_id: str | None = Field(default=None, description="Docker container ID")
    status: str = Field(default="unknown", description="Current container status")
    health: str = Field(default="unknown", description="Health status")
    ports: dict[int, int] = Field(
        default_factory=dict,
        description="Mapping of container_port -> host_port",
    )

    model_config = {
        "extra": "forbid",
    }


class ServiceStack(BaseModel):
    """Manages a collection of ServiceDefinitions with lifecycle orchestration.

    ServiceStack is the primary replacement for docker-compose.yaml. It handles:
    - Image building via ImageRegistry (with content-hash caching)
    - Network creation (idempotent)
    - Topological sort of services by depends_on
    - Container creation, start, health wait
    - Ordered stop and destroy

    Supports context manager protocol for automatic cleanup.

    Attributes:
        config: Docker configuration.
        services: Dictionary of service name to ServiceDefinition.
        prefix: Prefix for container and network names.

    Example:
        >>> stack = ServiceStack()
        >>> stack.add_service(db_service_def)
        >>> stack.add_service(runner_def)
        >>> with stack:
        ...     # Services are running
        ...     url = stack.get_service_url("db-service", 8000)
    """

    config: DockerConfig = Field(
        default_factory=DockerConfig,
        description="Docker configuration",
    )
    services: dict[str, ServiceDefinition] = Field(
        default_factory=dict,
        description="Service definitions by name",
    )
    prefix: str = Field(
        default="tolokaforge",
        description="Prefix for container/network names",
    )

    # Private runtime state (not serialized)
    _registry: ImageRegistry = PrivateAttr(default_factory=ImageRegistry)
    _images: dict[str, Image] = PrivateAttr(default_factory=dict)
    _containers: dict[str, Container] = PrivateAttr(default_factory=dict)
    _networks: dict[str, Network] = PrivateAttr(default_factory=dict)
    _resolved_ports: dict[str, dict[int, int]] = PrivateAttr(default_factory=dict)

    model_config = {
        "extra": "forbid",
    }

    # ── Service Management ──────────────────────────────────────────────

    def add_service(self, service: ServiceDefinition) -> None:
        """Add a service definition to the stack.

        Args:
            service: ServiceDefinition to add.

        Raises:
            ValueError: If a service with the same name already exists.
        """
        if service.name in self.services:
            raise ValueError(f"Service '{service.name}' already exists in the stack")
        self.services[service.name] = service
        logger.debug("Added service '%s' to stack", service.name)

    def add_services(self, services: list[ServiceDefinition]) -> None:
        """Add multiple service definitions to the stack.

        Args:
            services: List of ServiceDefinition objects to add.
        """
        for service in services:
            self.add_service(service)

    # ── Image Building ──────────────────────────────────────────────────

    def build_images(self, force: bool = False) -> dict[str, Image]:
        """Build images for all services that need building.

        Uses ImageRegistry.get_or_build() for content-hash caching.
        Services with use_prebuilt_image=True are skipped.

        When a service declares ``context_files``, an isolated temp build
        directory is assembled (via :func:`assemble_build_context`) so that
        only the declared files contribute to the content hash and build.

        Args:
            force: If True, force rebuild even if cached.

        Returns:
            Dictionary mapping service name to built Image.
        """
        logger.info("Building images for %d services", len(self.services))
        images: dict[str, Image] = {}
        temp_dirs_to_clean: list[Path] = []

        try:
            for name, svc in self.services.items():
                if svc.use_prebuilt_image:
                    # Use prebuilt image — create Image object without building
                    logger.info(
                        "Using prebuilt image '%s:%s' for service '%s'",
                        svc.image_name,
                        svc.prebuilt_tag,
                        name,
                    )
                    image = Image(
                        name=svc.image_name,
                        tag=svc.prebuilt_tag,
                        dockerfile=svc.dockerfile or "prebuilt",
                        context=svc.context,
                        context_hash="prebuilt",
                    )
                    images[name] = image
                elif svc.dockerfile:
                    logger.info("Building image for service '%s'", name)

                    # Determine build context and dockerfile path
                    if svc.context_files:
                        from tolokaforge.docker.builder import assemble_build_context

                        repo_root = Path(svc.context).resolve()
                        build_context = assemble_build_context(
                            repo_root=repo_root,
                            dockerfile=svc.dockerfile,
                            context_files=svc.context_files,
                        )
                        temp_dirs_to_clean.append(build_context)
                        build_dockerfile = str(build_context / svc.dockerfile)
                        build_context_str = str(build_context)
                    else:
                        build_dockerfile = svc.dockerfile
                        build_context_str = svc.context

                    if force:
                        # Force rebuild by building directly
                        image = Image.build(
                            dockerfile=build_dockerfile,
                            context=build_context_str,
                            build_args=svc.build_args,
                            name=svc.image_name,
                        )
                    else:
                        image = self._registry.get_or_build(
                            name=svc.image_name,
                            dockerfile=build_dockerfile,
                            context=build_context_str,
                            build_args=svc.build_args,
                        )
                    images[name] = image
                else:
                    logger.warning(
                        "Service '%s' has no dockerfile and use_prebuilt_image=False; skipping",
                        name,
                    )
        finally:
            for d in temp_dirs_to_clean:
                shutil.rmtree(d, ignore_errors=True)

        self._images = images
        logger.info("Built %d images", len(images))
        return images

    # ── Network Management ──────────────────────────────────────────────

    def create_networks(self) -> dict[str, Network]:
        """Create all networks referenced by services (idempotent).

        Returns:
            Dictionary mapping network name to Network object.
        """
        network_names: set[str] = set()
        for svc in self.services.values():
            for net_name in svc.networks:
                network_names.add(net_name)

        if not network_names:
            # Create a default network for the stack
            default_name = f"{self.prefix}-net"
            network_names.add(default_name)

        networks: dict[str, Network] = {}
        for net_name in network_names:
            logger.info("Creating network '%s'", net_name)
            network = Network.create(name=net_name)
            networks[net_name] = network

        self._networks = networks
        return networks

    # ── Dependency Ordering ─────────────────────────────────────────────

    @staticmethod
    def _topological_sort(services: dict[str, ServiceDefinition]) -> list[str]:
        """Sort services in dependency order using Kahn's algorithm.

        Args:
            services: Dictionary of service name to ServiceDefinition.

        Returns:
            List of service names in dependency order (dependencies first).

        Raises:
            ValueError: If circular dependencies are detected.
        """
        # Build adjacency and in-degree maps
        in_degree: dict[str, int] = dict.fromkeys(services, 0)
        dependents: dict[str, list[str]] = {name: [] for name in services}

        for name, svc in services.items():
            for dep in svc.depends_on:
                if dep not in services:
                    raise ValueError(
                        f"Service '{name}' depends on '{dep}', which is not in the stack"
                    )
                in_degree[name] += 1
                dependents[dep].append(name)

        # Start with services that have no dependencies
        queue = [name for name, deg in in_degree.items() if deg == 0]
        result: list[str] = []

        while queue:
            # Sort for deterministic ordering
            queue.sort()
            node = queue.pop(0)
            result.append(node)

            for dependent in dependents[node]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        if len(result) != len(services):
            remaining = set(services.keys()) - set(result)
            raise ValueError(f"Circular dependency detected among services: {remaining}")

        return result

    # ── Lifecycle ───────────────────────────────────────────────────────

    def start_all(
        self,
        profiles: list[str] | None = None,
        wait: bool = True,
        build: bool = True,
    ) -> None:
        """Start all services in dependency order.

        Args:
            profiles: If provided, only start services matching these profiles.
                Services with no profiles are always started.
            wait: If True, wait for health probes after starting each service.
            build: If True, build images before starting.

        Raises:
            ValueError: If circular dependencies are detected.
            HealthProbeError: If a required health check fails.
        """
        # Filter services by profile
        active_services = self._filter_by_profiles(profiles)

        if not active_services:
            logger.warning("No services to start (profile filter may have excluded all)")
            return

        logger.info(
            "Starting %d services (profiles=%s, wait=%s)",
            len(active_services),
            profiles,
            wait,
        )

        # Build images if needed
        if build:
            # Temporarily set services to only active ones for building
            self.build_images()

        # Create networks
        self.create_networks()

        # Topological sort for start order
        order = self._topological_sort(active_services)
        logger.info("Start order: %s", order)

        for name in order:
            svc = active_services[name]
            self._start_service(svc, wait=wait)

    def _filter_by_profiles(self, profiles: list[str] | None) -> dict[str, ServiceDefinition]:
        """Filter services by profile tags.

        Services with no profiles are always included.
        Services with profiles are included only if at least one matches.

        Args:
            profiles: Profile tags to filter by. None means include all.

        Returns:
            Filtered dictionary of services.
        """
        if profiles is None:
            return dict(self.services)

        result: dict[str, ServiceDefinition] = {}
        for name, svc in self.services.items():
            if not svc.profiles:
                # No profiles = always included
                result[name] = svc
            elif any(p in profiles for p in svc.profiles):
                result[name] = svc
            else:
                logger.debug(
                    "Skipping service '%s' (profiles %s not in %s)",
                    name,
                    svc.profiles,
                    profiles,
                )

        return result

    def _try_reuse_existing(
        self,
        container_name: str,
        svc: ServiceDefinition,
    ) -> Container | None:
        """Try to reuse an existing healthy container.

        Returns a :class:`Container` wrapper if one exists and is healthy,
        ``None`` otherwise.  If the container exists but is unhealthy or
        stopped, it is forcibly removed so a fresh one can be created.
        """
        import docker as docker_lib

        try:
            client = docker_lib.from_env()
            existing = client.containers.get(container_name)
        except docker_lib.errors.NotFound:
            return None
        except Exception:
            return None

        status = existing.status  # "running", "exited", "created", etc.

        if status == "running":
            if svc.health_probe:
                # Single-shot health check via the probe's internal _check()
                try:
                    healthy = svc.health_probe._check()  # noqa: SLF001
                    if healthy:
                        container = Container(
                            container_id=existing.id,
                            name=container_name,
                            image_tag=(
                                existing.image.tags[0] if existing.image.tags else "unknown"
                            ),
                            current_status=ContainerStatus.RUNNING,
                        )
                        container._client = client  # noqa: SLF001
                        return container
                except Exception:
                    pass  # Health check failed — fall through to removal
            else:
                # No health probe — optimistically reuse if running
                container = Container(
                    container_id=existing.id,
                    name=container_name,
                    image_tag=(existing.image.tags[0] if existing.image.tags else "unknown"),
                    current_status=ContainerStatus.RUNNING,
                )
                container._client = client  # noqa: SLF001
                return container

        # Container exists but isn't healthy — remove it
        logger.info(
            "Removing unhealthy existing container '%s' (status=%s)",
            container_name,
            status,
        )
        try:
            existing.remove(force=True)
        except Exception as e:
            logger.warning(
                "Failed to remove unhealthy container '%s': %s",
                container_name,
                e,
            )

        return None

    @staticmethod
    def _resolve_health_probe(
        probe: HealthProbe | None,
        port_map: dict[int, int],
    ) -> HealthProbe | None:
        """Return *probe* with auto-port URLs resolved, or build a default one.

        When ``core_stack`` is created with ``db_port="auto"`` the
        :class:`ServiceDefinition` carries ``health_probe=None`` because
        the host port is not yet known.  After ports are resolved we can
        construct a proper HTTP probe for every container port that has
        a ``/health`` endpoint on ``localhost``.

        For existing probes whose URL already contains a concrete port,
        this method returns them unchanged.
        """
        if probe is not None:
            return probe

        # No explicit probe — try to build a default HTTP probe for port 8000
        # (the DB-service convention).  Other ports (e.g. gRPC 50051) do not
        # expose HTTP health endpoints, so we only create a probe when there
        # is a mapping for container-port 8000.
        host_port = port_map.get(8000)
        if host_port is not None:
            return HealthProbe.http(
                url=f"http://localhost:{host_port}/health",
                timeout_s=30.0,
                interval_s=1.0,
            )
        return None

    def _extract_ports_from_container(
        self,
        container_name: str,
        svc: ServiceDefinition,
    ) -> dict[int, int]:
        """Read actual host-port bindings from a running Docker container."""
        import docker as docker_lib

        port_map: dict[int, int] = {}
        try:
            client = docker_lib.from_env()
            existing = client.containers.get(container_name)
            ports_info: dict[str, Any] = existing.attrs.get("NetworkSettings", {}).get("Ports", {})
            # ports_info example:
            #   {"8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "32768"}]}
            for pc in svc.ports:
                key = f"{pc.container_port}/{pc.protocol}"
                bindings = ports_info.get(key)
                if bindings:
                    port_map[pc.container_port] = int(bindings[0]["HostPort"])
        except Exception as e:
            logger.warning(
                "Could not extract ports from container '%s': %s",
                container_name,
                e,
            )
        return port_map

    def _start_service(self, svc: ServiceDefinition, wait: bool = True) -> None:
        """Start a single service.

        If a container with the expected name already exists and is healthy
        it is reused instead of creating a new one.  Unhealthy or stopped
        containers are removed and recreated.

        Args:
            svc: ServiceDefinition to start.
            wait: If True, wait for health probe.
        """
        name = svc.name

        # ── Build / fetch image first (need tag for container naming) ──
        if name not in self._images:
            if svc.use_prebuilt_image:
                image = Image(
                    name=svc.image_name,
                    tag=svc.prebuilt_tag,
                    dockerfile=svc.dockerfile or "prebuilt",
                    context=svc.context,
                    context_hash="prebuilt",
                )
                self._images[name] = image
            elif svc.dockerfile:
                image = self._registry.get_or_build(
                    name=svc.image_name,
                    dockerfile=svc.dockerfile,
                    context=svc.context,
                    build_args=svc.build_args,
                )
                self._images[name] = image
            else:
                raise ValueError(f"Service '{name}' has no image built and no dockerfile specified")

        image = self._images[name]

        # Container name includes image tag to prevent reusing containers
        # from a different image (e.g., with vs without Playwright).
        container_name = f"{self.prefix}-{name}"

        # ── Try to reuse an existing healthy container ──────────────
        existing = self._try_reuse_existing(container_name, svc)
        if existing:
            # Verify the existing container uses the same image
            running_image = existing.image_tag or ""
            expected_image = image.full_tag
            if running_image != expected_image:
                logger.info(
                    "Existing container '%s' uses image '%s' but expected '%s' — recreating",
                    container_name,
                    running_image,
                    expected_image,
                )
                try:
                    existing.remove(force=True)
                except Exception:
                    pass
            else:
                self._containers[name] = existing
                logger.info("Reusing existing healthy container '%s'", container_name)
                if svc.ports:
                    port_map = self._extract_ports_from_container(container_name, svc)
                    self._resolved_ports[name] = port_map
                return

        # ── Determine network ───────────────────────────────────────
        network = None
        if svc.networks and svc.networks[0] in self._networks:
            network = self._networks[svc.networks[0]]
        elif self._networks:
            network = next(iter(self._networks.values()))

        # ── Resolve ports (so we know host ports before creation) ───
        resolved_port_configs: list[PortConfig] = []
        port_map: dict[int, int] = {}
        if svc.ports:
            resolved_port_configs = resolve_ports(svc.ports)
            for pc in resolved_port_configs:
                if isinstance(pc.host_port, int):
                    port_map[pc.container_port] = pc.host_port

        # ── Create container ────────────────────────────────────────
        logger.info(
            "Creating container '%s' from image '%s'",
            container_name,
            image.full_tag,
        )

        container = Container.create(
            image=image,
            name=container_name,
            mounts=svc.mounts if svc.mounts else None,
            network=network,
            resources=svc.resources,
            environment=svc.environment if svc.environment else None,
            command=svc.command,
            ports=resolved_port_configs if resolved_port_configs else None,
            privileged=svc.privileged,
            secret_keys=svc.secret_keys if svc.secret_keys else None,
        )

        # Attach to additional networks
        if network and len(svc.networks) > 1:
            for net_name in svc.networks[1:]:
                if net_name in self._networks:
                    self._networks[net_name].attach(container.container_id)

        # Start container
        container.start()
        self._containers[name] = container

        # Track resolved ports
        self._resolved_ports[name] = port_map

        # ── Wait for health check ──────────────────────────────────
        effective_probe = self._resolve_health_probe(svc.health_probe, port_map)
        if wait and effective_probe:
            logger.info("Waiting for service '%s' health check...", name)
            try:
                result = effective_probe.wait()
                if result.healthy:
                    logger.info("Service '%s' is healthy", name)
                else:
                    logger.warning("Service '%s' health check returned unhealthy", name)
            except HealthProbeError as e:
                logger.error("Service '%s' health check failed: %s", name, e)
                raise

    def stop_all(self) -> None:
        """Stop all containers in reverse dependency order."""
        if not self._containers:
            logger.debug("No containers to stop")
            return

        # Get reverse dependency order
        try:
            order = self._topological_sort(
                {n: s for n, s in self.services.items() if n in self._containers}
            )
            order.reverse()
        except ValueError:
            # If ordering fails, just stop all
            order = list(self._containers.keys())

        logger.info("Stopping %d services in order: %s", len(order), order)

        for name in order:
            if name in self._containers:
                container = self._containers[name]
                try:
                    logger.info("Stopping service '%s'", name)
                    container.stop()
                except Exception as e:
                    logger.warning("Failed to stop service '%s': %s", name, e)

    def destroy(
        self,
        remove_networks: bool = True,
        remove_volumes: bool = False,
    ) -> None:
        """Destroy all containers and optionally networks/volumes.

        Args:
            remove_networks: If True, remove created networks.
            remove_volumes: If True, remove associated volumes.
        """
        # Stop all first
        self.stop_all()

        # Destroy containers
        for name, container in list(self._containers.items()):
            try:
                logger.info("Destroying container for service '%s'", name)
                container.destroy(remove_volumes=remove_volumes)
            except Exception as e:
                logger.warning("Failed to destroy container for '%s': %s", name, e)
        self._containers.clear()

        # Remove networks
        if remove_networks:
            for net_name, network in list(self._networks.items()):
                try:
                    logger.info("Removing network '%s'", net_name)
                    network.destroy()
                except Exception as e:
                    logger.warning("Failed to remove network '%s': %s", net_name, e)
            self._networks.clear()

        self._images.clear()
        self._resolved_ports.clear()

    def health_check_all(self) -> dict[str, ServiceStatus]:
        """Run health checks on all services.

        Returns:
            Dictionary mapping service name to ServiceStatus.
        """
        statuses: dict[str, ServiceStatus] = {}

        for name, svc in self.services.items():
            container = self._containers.get(name)
            health = "no_probe"

            if svc.health_probe and container:
                try:
                    result = svc.health_probe.wait()
                    health = "healthy" if result.healthy else "unhealthy"
                except HealthProbeError:
                    health = "unhealthy"

            statuses[name] = ServiceStatus(
                name=name,
                container_id=container.container_id if container else None,
                status=container.current_status.value if container else "not_created",
                health=health,
                ports=self._resolved_ports.get(name, {}),
            )

        return statuses

    def get_service_url(self, name: str, port: int) -> str:
        """Get the URL for a service's port on localhost.

        Args:
            name: Service name.
            port: Container port number.

        Returns:
            URL string like "http://localhost:{host_port}".

        Raises:
            KeyError: If service or port not found.
        """
        ports = self._resolved_ports.get(name, {})
        host_port = ports.get(port, port)
        return f"http://localhost:{host_port}"

    def get_service_address(self, name: str, port: int) -> str:
        """Get the internal address for a service (name:port).

        Used for inter-container communication within Docker networks.

        Args:
            name: Service name.
            port: Container port number.

        Returns:
            Address string like "{prefix}-{name}:{port}".
        """
        container_name = f"{self.prefix}-{name}"
        return f"{container_name}:{port}"

    def get_status(self) -> dict[str, ServiceStatus]:
        """Get current status of all services.

        Returns:
            Dictionary mapping service name to ServiceStatus.
        """
        statuses: dict[str, ServiceStatus] = {}

        for name in self.services:
            container = self._containers.get(name)
            statuses[name] = ServiceStatus(
                name=name,
                container_id=container.container_id if container else None,
                status=container.current_status.value if container else "not_created",
                health="unknown",
                ports=self._resolved_ports.get(name, {}),
            )

        return statuses

    # ── Context Manager ─────────────────────────────────────────────────

    def __enter__(self) -> ServiceStack:
        """Start all services when entering context."""
        self.start_all()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Destroy all services when exiting context."""
        self.destroy()
