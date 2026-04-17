"""Container module for Docker Foundation Layer.

Manages the full lifecycle of a single container: create → start → run → stop → destroy.
Provides methods for interacting with a running container (exec commands, read/write files).
Uses Pydantic BaseModel for validation and serialization.

Async Support:
    All blocking operations have async counterparts (async_start, async_stop, etc.)
    that use anyio.to_thread.run_sync() to run the blocking Docker SDK calls
    in a thread pool, making them safe to use in async contexts.
"""

from __future__ import annotations

import io
import logging
import tarfile
import time
from collections.abc import Iterator
from enum import Enum
from typing import TYPE_CHECKING, Any, cast

import anyio
from docker.errors import APIError, DockerException, NotFound
from pydantic import BaseModel, Field, PrivateAttr, field_validator

import docker
from tolokaforge.docker.health import HealthProbe, HealthProbeError, ProbeResult
from tolokaforge.docker.image import Image
from tolokaforge.docker.logging import LogRouter
from tolokaforge.docker.mount import Mount
from tolokaforge.docker.network import Network
from tolokaforge.docker.policy import ResourcePolicy
from tolokaforge.docker.ports import PortConfig, ports_to_docker_format, resolve_ports

if TYPE_CHECKING:
    from docker.models.containers import Container as DockerContainer

    from docker import DockerClient
    from tolokaforge.docker.secrets.manager import SecretManager

logger = logging.getLogger(__name__)


class ContainerStatus(str, Enum):
    """Status of a Docker container.

    This enum provides type-safe container status names.
    Values are lowercase strings matching container states.
    """

    CREATED = "created"
    STARTING = "starting"
    READY = "ready"  # Health check passed
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"
    DESTROYED = "destroyed"


class ContainerError(Exception):
    """Raised when a container operation fails."""

    def __init__(self, operation: str, container_name: str, message: str):
        self.operation = operation
        self.container_name = container_name
        super().__init__(f"Container {operation} failed for '{container_name}': {message}")


class ExecResult(BaseModel):
    """Result of executing a command inside a container.

    Attributes:
        exit_code: Exit code of the command.
        stdout: Standard output from the command.
        stderr: Standard error from the command.
    """

    exit_code: int = Field(
        description="Exit code of the command",
    )
    stdout: str = Field(
        default="",
        description="Standard output from the command",
    )
    stderr: str = Field(
        default="",
        description="Standard error from the command",
    )

    model_config = {
        "frozen": True,
        "extra": "forbid",
    }


class Container(BaseModel):
    """Manages a single Docker container lifecycle.

    This model represents a Docker container configuration and provides methods
    for creating, starting, stopping, destroying containers, and interacting
    with running containers (exec, read/write files, logs).

    Attributes:
        container_id: Docker container ID (set after creation).
        name: Name of the container.
        image_tag: Full image tag used to create the container.
        current_status: Current status of the container.

    Example:
        >>> container = Container.create(
        ...     image=image,
        ...     name="my-container",
        ...     mounts=[Mount.volume("data", "/data")],
        ... )
        >>> container.start()
        >>> container.health_check(HealthProbe.http("http://localhost:8000/health"))
        >>> result = container.exec(["echo", "hello"])
        >>> container.stop()
        >>> container.destroy()
    """

    container_id: str | None = Field(
        default=None,
        description="Docker container ID (set after creation)",
    )
    name: str = Field(
        description="Name of the container",
    )
    image_tag: str = Field(
        description="Full image tag used to create the container",
    )
    current_status: ContainerStatus = Field(
        default=ContainerStatus.CREATED,
        description="Current status of the container",
    )

    # Private attribute for Docker client (not serialized)
    _client: DockerClient | None = PrivateAttr(default=None)
    # Private attribute for LogRouter (not serialized)
    _log_router: LogRouter | None = PrivateAttr(default=None)

    model_config = {
        "extra": "forbid",
    }

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        """Validate that container name is not empty."""
        v = v.strip()
        if not v:
            raise ValueError("Container name cannot be empty")
        # Docker container names can contain alphanumeric, underscores, hyphens, and dots
        # but cannot start with a hyphen or dot
        if v.startswith("-") or v.startswith("."):
            raise ValueError(f"Container name cannot start with '-' or '.', got: {v!r}")
        return v

    def _get_client(self) -> DockerClient:
        """Get or create Docker client.

        Returns:
            Docker client instance.

        Raises:
            ContainerError: If Docker client cannot be created.
        """
        if self._client is None:
            try:
                self._client = docker.from_env()
            except DockerException as e:
                raise ContainerError(
                    "connect", self.name, f"Failed to connect to Docker: {e}"
                ) from e
        return cast("DockerClient", self._client)

    def _get_docker_container(self) -> DockerContainer:
        """Get the Docker container object.

        Returns:
            Docker container object.

        Raises:
            ContainerError: If container doesn't exist.
        """
        if not self.container_id:
            raise ContainerError(
                "get", self.name, "Container has not been created (no container_id)"
            )

        client = self._get_client()
        try:
            return client.containers.get(self.container_id)
        except NotFound as e:
            raise ContainerError(
                "get", self.name, f"Container not found: {self.container_id}"
            ) from e
        except APIError as e:
            raise ContainerError("get", self.name, f"Docker API error: {e}") from e

    @classmethod
    def create(
        cls,
        image: Image,
        name: str | None = None,
        mounts: list[Mount] | None = None,
        network: Network | None = None,
        resources: ResourcePolicy | None = None,
        environment: dict[str, str] | None = None,
        command: str | list[str] | None = None,
        ports: list[PortConfig] | None = None,
        client: DockerClient | None = None,
        secret_keys: list[str] | None = None,
        secret_manager: SecretManager | None = None,
        privileged: bool = False,
    ) -> Container:
        """Create a container (does not start it).

        Args:
            image: Image to use for the container.
            name: Name for the container. If not provided, Docker generates one.
            mounts: List of mounts to attach to the container.
            network: Network to attach the container to.
            resources: Resource policy (limits, security constraints).
            environment: Environment variables to set.
            command: Command to run in the container.
            ports: List of PortConfig for port mappings. Auto-allocated ports
                will be resolved before container creation.
            client: Optional Docker client (for testing/mocking).
            secret_keys: List of secret key names to resolve and pass as env vars.
            secret_manager: SecretManager to use for resolving secrets.
                If secret_keys is provided but secret_manager is None,
                a default manager will be created using SecretConfig.default().

        Returns:
            Container instance with container_id set.

        Raises:
            ContainerError: If container creation fails.
            MissingSecretError: If a required secret is not found.
            PortAllocationError: If a requested port is unavailable or auto-allocation fails.

        Example:
            >>> container = Container.create(
            ...     image=image,
            ...     name="my-container",
            ...     mounts=[Mount.volume("data", "/data")],
            ...     resources=ResourcePolicy(memory_limit="256m"),
            ... )

        Example with ports:
            >>> container = Container.create(
            ...     image=image,
            ...     name="my-container",
            ...     ports=[
            ...         PortConfig(container_port=8080, host_port=8080),
            ...         PortConfig(container_port=9090, host_port="auto"),
            ...     ],
            ... )

        Example with secrets:
            >>> container = Container.create(
            ...     image=image,
            ...     name="my-container",
            ...     secret_keys=["OPENROUTER_API_KEY", "DATABASE_URL"],
            ... )
        """
        if client is None:
            try:
                client = docker.from_env()
            except DockerException as e:
                raise ContainerError(
                    "create", name or "unknown", f"Failed to connect to Docker: {e}"
                ) from e

        # Generate name if not provided
        if name is None:
            name = f"tolokaforge-{image.name}-{int(time.time())}"

        logger.info(
            "Creating container '%s' from image '%s'",
            name,
            image.full_tag,
        )

        # Build container configuration
        create_kwargs: dict[str, Any] = {
            "image": image.full_tag,
            "name": name,
            "detach": True,
        }

        # Add mounts
        if mounts:
            create_kwargs["mounts"] = [m.to_docker_mount() for m in mounts]

        # Add network
        if network and network.network_id:
            create_kwargs["network"] = network.name

        # Add resource policy
        if resources:
            host_config = resources.to_docker_host_config()
            create_kwargs.update(host_config)

        # Add environment variables
        if environment:
            create_kwargs["environment"] = dict(environment)
        else:
            create_kwargs["environment"] = {}

        # Resolve secrets and add to environment
        if secret_keys:
            # Import here to avoid circular imports
            from tolokaforge.docker.secrets.config import SecretConfig
            from tolokaforge.docker.secrets.manager import SecretManager as SM

            if secret_manager is None:
                secret_manager = SM.from_config(SecretConfig.default())

            # Validate that all required secrets are available
            secret_manager.validate_required(secret_keys)

            # Resolve secrets and add to environment
            secrets_env = secret_manager.to_env_dict(secret_keys)
            create_kwargs["environment"].update(secrets_env)

            logger.info(
                "Resolved %d secrets for container '%s': %s",
                len(secrets_env),
                name,
                list(secrets_env.keys()),
            )

        # Add privileged mode (required for Docker-in-Docker)
        if privileged:
            create_kwargs["privileged"] = True

        # Add command
        if command:
            create_kwargs["command"] = command

        # Add port mappings - resolve auto-allocated ports first
        if ports:
            resolved_ports = resolve_ports(ports)
            create_kwargs["ports"] = ports_to_docker_format(resolved_ports)

        try:
            docker_container = client.containers.create(**create_kwargs)
        except APIError as e:
            if e.status_code == 409:
                # Container name conflict — remove stale container and retry
                logger.warning(
                    "Container '%s' already exists (stale from previous run); "
                    "removing and retrying",
                    name,
                )
                try:
                    stale = client.containers.get(name)
                    stale.remove(force=True)
                    logger.info("Removed stale container '%s'", name)
                except Exception as remove_err:
                    raise ContainerError(
                        "create",
                        name,
                        f"Failed to remove stale container: {remove_err}",
                    ) from e
                # Retry create after removal
                try:
                    docker_container = client.containers.create(**create_kwargs)
                except APIError as retry_err:
                    raise ContainerError(
                        "create", name, f"Docker API error on retry: {retry_err}"
                    ) from retry_err
            else:
                raise ContainerError("create", name, f"Docker API error: {e}") from e

        logger.info(
            "Created container '%s' with ID %s",
            name,
            docker_container.id,
        )

        container = cls(
            container_id=docker_container.id,
            name=name,
            image_tag=image.full_tag,
            current_status=ContainerStatus.CREATED,
        )
        container._client = client
        return container

    def start(
        self,
        log_router: LogRouter | None = None,
        trial_id: str | None = None,
        log_file: str | None = None,
    ) -> None:
        """Start the container.

        Args:
            log_router: Optional LogRouter to attach for log streaming.
                If provided, it will be started automatically.
            trial_id: Optional trial ID for auto-creating a LogRouter.
                Only used if log_router is None.
            log_file: Optional log file path for auto-creating a LogRouter.
                Only used if log_router is None.

        Raises:
            ContainerError: If start operation fails.

        Example:
            >>> container.start()

        Example with log router:
            >>> log_router = LogRouter.for_container(container, trial_id="trial-001")
            >>> container.start(log_router=log_router)

        Example with auto-created log router:
            >>> container.start(trial_id="trial-001", log_file="/tmp/container.log")
        """
        logger.info("Starting container '%s'", self.name)

        self.current_status = ContainerStatus.STARTING

        try:
            docker_container = self._get_docker_container()
            docker_container.start()

            # Update status based on container state
            docker_container.reload()
            if docker_container.status == "running":
                self.current_status = ContainerStatus.RUNNING
                logger.info("Container '%s' is now running", self.name)

                # Start log router if provided or auto-create one
                if log_router is not None:
                    self._log_router = log_router
                    self._log_router.start()
                elif trial_id is not None or log_file is not None:
                    self._log_router = LogRouter.for_container(
                        container=self,
                        trial_id=trial_id,
                        log_file=log_file,
                    )
                    self._log_router.start()
            else:
                self.current_status = ContainerStatus.FAILED
                logger.error(
                    "Container '%s' failed to start, status: %s",
                    self.name,
                    docker_container.status,
                )

        except APIError as e:
            self.current_status = ContainerStatus.FAILED
            raise ContainerError("start", self.name, f"Docker API error: {e}") from e

    def stop(self, timeout_s: float = 10.0) -> None:
        """Stop the container gracefully.

        Also stops the LogRouter if one is attached.

        Args:
            timeout_s: Timeout in seconds before force-killing.

        Raises:
            ContainerError: If stop operation fails (except for not found).

        Example:
            >>> container.stop(timeout_s=30.0)
        """
        if not self.container_id:
            logger.warning("Cannot stop container '%s': no container_id", self.name)
            return

        # Stop log router first
        if self._log_router is not None:
            logger.debug("Stopping log router for container '%s'", self.name)
            self._log_router.stop()
            self._log_router = None

        logger.info("Stopping container '%s' (timeout=%ss)", self.name, timeout_s)

        client = self._get_client()
        try:
            docker_container = client.containers.get(self.container_id)
            docker_container.stop(timeout=int(timeout_s))
            self.current_status = ContainerStatus.STOPPED
            logger.info("Container '%s' stopped", self.name)

        except NotFound:
            logger.info("Container '%s' already stopped or doesn't exist", self.name)
            self.current_status = ContainerStatus.STOPPED
        except APIError as e:
            raise ContainerError("stop", self.name, f"Docker API error: {e}") from e

    def destroy(self, *, remove_volumes: bool = False) -> None:
        """Remove the container and associated resources.

        Also stops the LogRouter if one is attached.

        Args:
            remove_volumes: If True, remove associated anonymous volumes.

        Raises:
            ContainerError: If removal fails (except for not found).

        Example:
            >>> container.destroy()
            >>> container.destroy(remove_volumes=True)
        """
        # Stop log router first
        if self._log_router is not None:
            logger.debug("Stopping log router for container '%s'", self.name)
            self._log_router.stop()
            self._log_router = None

        if not self.container_id:
            logger.warning("Cannot destroy container '%s': no container_id", self.name)
            self.current_status = ContainerStatus.DESTROYED
            return

        logger.info("Destroying container '%s' (ID: %s)", self.name, self.container_id)

        client = self._get_client()
        try:
            docker_container = client.containers.get(self.container_id)
            # Force remove to handle running containers
            docker_container.remove(force=True, v=remove_volumes)
            self.current_status = ContainerStatus.DESTROYED
            logger.info("Container '%s' destroyed", self.name)

        except NotFound:
            logger.info("Container '%s' already destroyed or doesn't exist", self.name)
            self.current_status = ContainerStatus.DESTROYED
        except APIError as e:
            raise ContainerError("destroy", self.name, f"Docker API error: {e}") from e

    def health_check(self, probe: HealthProbe) -> ProbeResult:
        """Run a health probe, blocking until ready or timeout.

        Args:
            probe: Health probe to run.

        Returns:
            ProbeResult with the outcome.

        Raises:
            HealthProbeError: If the probe fails after all retries.

        Example:
            >>> result = container.health_check(
            ...     HealthProbe.http("http://localhost:8000/health")
            ... )
            >>> if result.healthy:
            ...     print("Container is ready!")
        """
        logger.info(
            "Running health check for container '%s' with %s probe",
            self.name,
            probe.probe_type.value,
        )

        try:
            result = probe.wait()
            if result.healthy:
                self.current_status = ContainerStatus.READY
                logger.info("Container '%s' is ready", self.name)
            return result
        except HealthProbeError:
            self.current_status = ContainerStatus.FAILED
            raise

    def status(self) -> ContainerStatus:
        """Return current container status.

        Queries Docker for the actual container state and updates
        the internal status accordingly.

        Returns:
            Current ContainerStatus.

        Example:
            >>> status = container.status()
            >>> if status == ContainerStatus.RUNNING:
            ...     print("Container is running")
        """
        if not self.container_id:
            return self.current_status

        client = self._get_client()
        try:
            docker_container = client.containers.get(self.container_id)
            docker_container.reload()

            docker_status = docker_container.status
            if docker_status == "created":
                self.current_status = ContainerStatus.CREATED
            elif docker_status == "running":
                # Keep READY if already set, otherwise RUNNING
                if self.current_status != ContainerStatus.READY:
                    self.current_status = ContainerStatus.RUNNING
            elif docker_status in ("exited", "dead"):
                # If we explicitly stopped the container, keep it as STOPPED
                # regardless of exit code (SIGTERM causes non-zero exit)
                if self.current_status == ContainerStatus.STOPPED:
                    pass  # Keep STOPPED status
                else:
                    # Check exit code to determine if failed or stopped
                    exit_code = docker_container.attrs.get("State", {}).get("ExitCode", 0)
                    if exit_code != 0:
                        self.current_status = ContainerStatus.FAILED
                    else:
                        self.current_status = ContainerStatus.STOPPED
            elif docker_status == "paused":
                self.current_status = ContainerStatus.STOPPED
            elif docker_status == "restarting":
                self.current_status = ContainerStatus.STARTING

            return self.current_status

        except NotFound:
            self.current_status = ContainerStatus.DESTROYED
            return self.current_status
        except APIError as e:
            raise ContainerError("status", self.name, f"Docker API error: {e}") from e

    def exec(self, command: str | list[str]) -> ExecResult:
        """Execute a command inside the running container.

        Args:
            command: Command to run (string or list of args).

        Returns:
            ExecResult with exit_code, stdout, and stderr.

        Raises:
            ContainerError: If exec operation fails.

        Example:
            >>> result = container.exec(["echo", "hello"])
            >>> print(result.stdout)
            'hello'
            >>> print(result.exit_code)
            0
        """
        if isinstance(command, str):
            command = ["sh", "-c", command]

        logger.debug("Executing command in container '%s': %s", self.name, command)

        try:
            docker_container = self._get_docker_container()

            # Create exec instance
            exec_result = docker_container.exec_run(
                cmd=command,
                demux=True,  # Separate stdout and stderr
            )

            exit_code = exec_result.exit_code
            output = exec_result.output

            # Handle demuxed output (tuple of stdout, stderr)
            if isinstance(output, tuple):
                stdout_bytes, stderr_bytes = output
                stdout = (stdout_bytes or b"").decode("utf-8", errors="replace")
                stderr = (stderr_bytes or b"").decode("utf-8", errors="replace")
            else:
                # Non-demuxed output
                stdout = (output or b"").decode("utf-8", errors="replace")
                stderr = ""

            logger.debug(
                "Command in container '%s' exited with code %d",
                self.name,
                exit_code,
            )

            return ExecResult(
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
            )

        except APIError as e:
            raise ContainerError("exec", self.name, f"Docker API error: {e}") from e

    def write_file(self, container_path: str, content: bytes) -> None:
        """Write a file into the running container via tar archive.

        Args:
            container_path: Absolute path inside the container.
            content: File content as bytes.

        Raises:
            ContainerError: If write operation fails.
            ValueError: If container_path is not absolute.

        Example:
            >>> container.write_file("/app/config.json", b'{"key": "value"}')
        """
        if not container_path.startswith("/"):
            raise ValueError(f"Container path must be absolute, got: {container_path!r}")

        logger.debug(
            "Writing %d bytes to '%s' in container '%s'",
            len(content),
            container_path,
            self.name,
        )

        try:
            docker_container = self._get_docker_container()

            # Create tar archive in memory
            tar_buffer = io.BytesIO()
            with tarfile.open(fileobj=tar_buffer, mode="w") as tar:
                # Extract filename from path
                import os

                filename = os.path.basename(container_path)
                dir_path = os.path.dirname(container_path)

                # Create tarinfo for the file
                tarinfo = tarfile.TarInfo(name=filename)
                tarinfo.size = len(content)

                # Add file to tar
                tar.addfile(tarinfo, io.BytesIO(content))

            # Seek to beginning of buffer
            tar_buffer.seek(0)

            # Put archive into container
            docker_container.put_archive(dir_path, tar_buffer.getvalue())

            logger.debug(
                "Successfully wrote file '%s' to container '%s'",
                container_path,
                self.name,
            )

        except APIError as e:
            raise ContainerError(
                "write_file", self.name, f"Failed to write '{container_path}': {e}"
            ) from e

    def read_file(self, container_path: str) -> bytes:
        """Read a file from the running container.

        Args:
            container_path: Absolute path inside the container.

        Returns:
            File content as bytes.

        Raises:
            ContainerError: If read operation fails.
            ValueError: If container_path is not absolute.

        Example:
            >>> content = container.read_file("/app/output.txt")
            >>> print(content.decode())
        """
        if not container_path.startswith("/"):
            raise ValueError(f"Container path must be absolute, got: {container_path!r}")

        logger.debug(
            "Reading file '%s' from container '%s'",
            container_path,
            self.name,
        )

        try:
            docker_container = self._get_docker_container()

            # Get archive from container
            bits, stat = docker_container.get_archive(container_path)

            # Extract file from tar archive
            tar_buffer = io.BytesIO()
            for chunk in bits:
                tar_buffer.write(chunk)
            tar_buffer.seek(0)

            with tarfile.open(fileobj=tar_buffer, mode="r") as tar:
                # Get the first (and only) file in the archive
                members = tar.getmembers()
                if not members:
                    raise ContainerError(
                        "read_file", self.name, f"No file found at '{container_path}'"
                    )

                # Extract file content
                file_obj = tar.extractfile(members[0])
                if file_obj is None:
                    raise ContainerError(
                        "read_file",
                        self.name,
                        f"Cannot read '{container_path}' (is it a directory?)",
                    )

                content = file_obj.read()

            logger.debug(
                "Successfully read %d bytes from '%s' in container '%s'",
                len(content),
                container_path,
                self.name,
            )

            return content

        except NotFound as e:
            raise ContainerError(
                "read_file", self.name, f"File not found: '{container_path}'"
            ) from e
        except APIError as e:
            raise ContainerError(
                "read_file", self.name, f"Failed to read '{container_path}': {e}"
            ) from e

    def logs(self, follow: bool = False, tail: int | None = None) -> Iterator[str]:
        """Stream container logs.

        Args:
            follow: If True, follow log output (blocking).
            tail: Number of lines to show from the end. None for all.

        Yields:
            Log lines as strings.

        Raises:
            ContainerError: If log retrieval fails.

        Example:
            >>> for line in container.logs(tail=100):
            ...     print(line)
        """
        logger.debug(
            "Getting logs from container '%s' (follow=%s, tail=%s)",
            self.name,
            follow,
            tail,
        )

        try:
            docker_container = self._get_docker_container()

            log_kwargs: dict[str, Any] = {
                "stream": True,
                "follow": follow,
            }
            if tail is not None:
                log_kwargs["tail"] = tail

            log_stream = docker_container.logs(**log_kwargs)

            for chunk in log_stream:
                if isinstance(chunk, bytes):
                    yield chunk.decode("utf-8", errors="replace")
                else:
                    yield str(chunk)

        except APIError as e:
            raise ContainerError("logs", self.name, f"Docker API error: {e}") from e

    def with_client(self, client: DockerClient) -> Container:
        """Return a new Container with the specified Docker client.

        Useful for testing with mock clients.

        Args:
            client: Docker client to use.

        Returns:
            New Container instance with the client set.
        """
        new_container = self.model_copy()
        new_container._client = client
        return new_container

    def exists(self) -> bool:
        """Check if this container still exists.

        Returns:
            True if the container exists in Docker, False otherwise.

        Example:
            >>> container = Container.create(...)
            >>> container.exists()
            True
            >>> container.destroy()
            >>> container.exists()
            False
        """
        if not self.container_id:
            return False

        client = self._get_client()
        try:
            client.containers.get(self.container_id)
            return True
        except NotFound:
            return False
        # Let APIError propagate - callers should know about Docker daemon issues

    @property
    def log_router(self) -> LogRouter | None:
        """Return the attached LogRouter, if any.

        Returns:
            The LogRouter instance if one is attached, None otherwise.

        Example:
            >>> container.start(trial_id="trial-001")
            >>> if container.log_router:
            ...     print(f"Logging to: {container.log_router.log_file}")
        """
        return self._log_router

    # =========================================================================
    # Async Methods
    # =========================================================================
    # These methods wrap the synchronous Docker SDK calls using anyio.to_thread.run_sync()
    # to make them safe for use in async contexts without blocking the event loop.

    async def async_start(
        self,
        log_router: LogRouter | None = None,
        trial_id: str | None = None,
        log_file: str | None = None,
    ) -> None:
        """Start the container asynchronously.

        This is the async version of start() that runs the blocking Docker SDK
        calls in a thread pool using anyio.to_thread.run_sync().

        Args:
            log_router: Optional LogRouter to attach for log streaming.
                If provided, it will be started automatically.
            trial_id: Optional trial ID for auto-creating a LogRouter.
                Only used if log_router is None.
            log_file: Optional log file path for auto-creating a LogRouter.
                Only used if log_router is None.

        Raises:
            ContainerError: If start operation fails.

        Example:
            >>> await container.async_start()

        Example with log router:
            >>> await container.async_start(trial_id="trial-001")
        """
        await anyio.to_thread.run_sync(
            lambda: self.start(
                log_router=log_router,
                trial_id=trial_id,
                log_file=log_file,
            )
        )

    async def async_stop(self, timeout_s: float = 10.0) -> None:
        """Stop the container gracefully (async version).

        This is the async version of stop() that runs the blocking Docker SDK
        calls in a thread pool using anyio.to_thread.run_sync().

        Args:
            timeout_s: Timeout in seconds before force-killing.

        Raises:
            ContainerError: If stop operation fails (except for not found).

        Example:
            >>> await container.async_stop(timeout_s=30.0)
        """
        await anyio.to_thread.run_sync(lambda: self.stop(timeout_s=timeout_s))

    async def async_destroy(self) -> None:
        """Remove the container and associated resources (async version).

        This is the async version of destroy() that runs the blocking Docker SDK
        calls in a thread pool using anyio.to_thread.run_sync().

        Raises:
            ContainerError: If removal fails (except for not found).

        Example:
            >>> await container.async_destroy()
        """
        await anyio.to_thread.run_sync(self.destroy)

    async def async_exec(self, command: str | list[str]) -> ExecResult:
        """Execute a command inside the running container (async version).

        This is the async version of exec() that runs the blocking Docker SDK
        calls in a thread pool using anyio.to_thread.run_sync().

        Args:
            command: Command to run (string or list of args).

        Returns:
            ExecResult with exit_code, stdout, and stderr.

        Raises:
            ContainerError: If exec operation fails.

        Example:
            >>> result = await container.async_exec(["echo", "hello"])
            >>> print(result.stdout)
            'hello'
        """
        return await anyio.to_thread.run_sync(lambda: self.exec(command))

    async def async_health_check(self, probe: HealthProbe) -> ProbeResult:
        """Run a health probe asynchronously, blocking until ready or timeout.

        This is the async version of health_check() that runs the blocking
        probe.wait() call in a thread pool using anyio.to_thread.run_sync().

        Args:
            probe: Health probe to run.

        Returns:
            ProbeResult with the outcome.

        Raises:
            HealthProbeError: If the probe fails after all retries.

        Example:
            >>> result = await container.async_health_check(
            ...     HealthProbe.http("http://localhost:8000/health")
            ... )
            >>> if result.healthy:
            ...     print("Container is ready!")
        """
        return await anyio.to_thread.run_sync(lambda: self.health_check(probe))

    async def async_write_file(self, container_path: str, content: bytes) -> None:
        """Write a file into the running container via tar archive (async version).

        This is the async version of write_file() that runs the blocking Docker SDK
        calls in a thread pool using anyio.to_thread.run_sync().

        Args:
            container_path: Absolute path inside the container.
            content: File content as bytes.

        Raises:
            ContainerError: If write operation fails.
            ValueError: If container_path is not absolute.

        Example:
            >>> await container.async_write_file("/app/config.json", b'{"key": "value"}')
        """
        await anyio.to_thread.run_sync(lambda: self.write_file(container_path, content))

    async def async_read_file(self, container_path: str) -> bytes:
        """Read a file from the running container (async version).

        This is the async version of read_file() that runs the blocking Docker SDK
        calls in a thread pool using anyio.to_thread.run_sync().

        Args:
            container_path: Absolute path inside the container.

        Returns:
            File content as bytes.

        Raises:
            ContainerError: If read operation fails.
            ValueError: If container_path is not absolute.

        Example:
            >>> content = await container.async_read_file("/app/output.txt")
            >>> print(content.decode())
        """
        return await anyio.to_thread.run_sync(lambda: self.read_file(container_path))
