"""Network module for Docker Foundation Layer.

Manages Docker networks for isolation zones. The foundation creates and manages
networks; the upper layer decides the topology (which containers go on which network).
Uses Pydantic BaseModel for validation and serialization.

Async Support:
    All blocking operations have async counterparts (async_create, async_attach, etc.)
    that use anyio.to_thread.run_sync() to run the blocking Docker SDK calls
    in a thread pool, making them safe to use in async contexts.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

import anyio
from docker.errors import APIError, DockerException, NotFound
from pydantic import BaseModel, Field, PrivateAttr, field_validator

import docker

if TYPE_CHECKING:
    from docker.models.networks import Network as DockerNetwork

    from docker import DockerClient

logger = logging.getLogger(__name__)


class NetworkError(Exception):
    """Raised when a network operation fails."""

    def __init__(self, operation: str, network_name: str, message: str):
        self.operation = operation
        self.network_name = network_name
        super().__init__(f"Network {operation} failed for '{network_name}': {message}")


class Network(BaseModel):
    """Manages a Docker network.

    This model represents a Docker network configuration and provides methods
    for creating, attaching containers to, detaching containers from, and
    destroying networks.

    Attributes:
        name: Name of the Docker network.
        network_id: Docker network ID (set after creation).
        internal: If True, no external internet access (maps to Docker's internal flag).
        driver: Network driver (default: "bridge").

    Example:
        >>> network = Network.create("my-network", internal=True)
        >>> network.attach(container)
        >>> network.detach(container)
        >>> network.destroy()
    """

    name: str = Field(
        description="Name of the Docker network",
    )
    network_id: str | None = Field(
        default=None,
        description="Docker network ID (set after creation)",
    )
    internal: bool = Field(
        default=False,
        description="If True, no external internet access",
    )
    driver: str = Field(
        default="bridge",
        description="Network driver (default: bridge)",
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
        """Validate that network name is not empty."""
        v = v.strip()
        if not v:
            raise ValueError("Network name cannot be empty")
        # Docker network names can contain alphanumeric, underscores, hyphens, and dots
        # but cannot start with a hyphen or dot
        if v.startswith("-") or v.startswith("."):
            raise ValueError(f"Network name cannot start with '-' or '.', got: {v!r}")
        return v

    @field_validator("driver")
    @classmethod
    def validate_driver(cls, v: str) -> str:
        """Validate that driver is not empty."""
        v = v.strip()
        if not v:
            raise ValueError("Network driver cannot be empty")
        return v

    def _get_client(self) -> DockerClient:
        """Get or create Docker client.

        Returns:
            Docker client instance.

        Raises:
            NetworkError: If Docker client cannot be created.
        """
        if self._client is None:
            try:
                # Use object.__setattr__ because model is frozen
                object.__setattr__(self, "_client", docker.from_env())
            except DockerException as e:
                raise NetworkError("connect", self.name, f"Failed to connect to Docker: {e}") from e
        return cast("DockerClient", self._client)

    def _get_docker_network(self) -> DockerNetwork:
        """Get the Docker network object.

        Returns:
            Docker network object.

        Raises:
            NetworkError: If network doesn't exist.
        """
        if not self.network_id:
            raise NetworkError("get", self.name, "Network has not been created (no network_id)")

        client = self._get_client()
        try:
            return client.networks.get(self.network_id)
        except NotFound as e:
            raise NetworkError("get", self.name, f"Network not found: {self.network_id}") from e
        except APIError as e:
            raise NetworkError("get", self.name, f"Docker API error: {e}") from e

    @classmethod
    def create(
        cls,
        name: str,
        internal: bool = False,
        driver: str = "bridge",
        client: DockerClient | None = None,
    ) -> Network:
        """Create a Docker network.

        Creates a new Docker network with the specified configuration.
        If a network with the same name already exists, returns a Network
        instance pointing to the existing network (idempotent).

        Args:
            name: Name of the network.
            internal: If True, blocks external internet access.
            driver: Network driver (default: "bridge").
            client: Optional Docker client (for testing/mocking).

        Returns:
            Network instance with network_id set.

        Raises:
            NetworkError: If network creation fails.

        Example:
            >>> network = Network.create("env-net", internal=True)
            >>> network.network_id
            'abc123...'
        """
        if client is None:
            try:
                client = docker.from_env()
            except DockerException as e:
                raise NetworkError("create", name, f"Failed to connect to Docker: {e}") from e

        logger.info(
            "Creating network '%s' (internal=%s, driver=%s)",
            name,
            internal,
            driver,
        )

        try:
            # Check if network already exists
            existing = cls._find_existing_network(client, name)
            if existing:
                logger.info("Network '%s' already exists with ID %s", name, existing.id)
                network = cls(
                    name=name,
                    network_id=existing.id,
                    internal=internal,
                    driver=driver,
                )
                object.__setattr__(network, "_client", client)
                return network

            # Create new network
            docker_network = client.networks.create(
                name=name,
                driver=driver,
                internal=internal,
                check_duplicate=True,
            )

            logger.info("Created network '%s' with ID %s", name, docker_network.id)

            network = cls(
                name=name,
                network_id=docker_network.id,
                internal=internal,
                driver=driver,
            )
            object.__setattr__(network, "_client", client)
            return network

        except APIError as e:
            # Handle 409 Conflict race condition: another process created the
            # network between our _find_existing_network() check and create().
            if e.status_code == 409:
                logger.info(
                    "Network '%s' created by another process (409 Conflict), reusing",
                    name,
                )
                existing = cls._find_existing_network(client, name)
                if existing:
                    network = cls(
                        name=name,
                        network_id=existing.id,
                        internal=internal,
                        driver=driver,
                    )
                    object.__setattr__(network, "_client", client)
                    return network
            raise NetworkError("create", name, f"Docker API error: {e}") from e

    @classmethod
    def _find_existing_network(cls, client: DockerClient, name: str) -> DockerNetwork | None:
        """Find an existing network by name.

        This is a lookup function where "not found" is a normal response.
        Returns None on both "not found" and API errors to support idempotent
        network creation (caller will create if None returned).

        Args:
            client: Docker client.
            name: Network name to find.

        Returns:
            Docker network object if found, None otherwise.
        """
        try:
            networks = client.networks.list(names=[name])
            for net in networks:
                if net.name == name:
                    return net
            return None
        except APIError:
            # Treat API errors as "not found" - caller will attempt to create
            return None  # noqa: BLE001 - Lookup function, None is valid "not found"

    def attach(self, container: Any) -> None:
        """Attach a container to this network.

        Args:
            container: Container to attach. Can be a container ID (str),
                container name (str), or a Docker container object.

        Raises:
            NetworkError: If attach operation fails.

        Example:
            >>> network.attach("my-container")
            >>> network.attach(container_obj)
        """
        container_id = self._resolve_container_id(container)

        logger.info("Attaching container '%s' to network '%s'", container_id, self.name)

        try:
            docker_network = self._get_docker_network()
            docker_network.connect(container_id)
            logger.info(
                "Successfully attached container '%s' to network '%s'",
                container_id,
                self.name,
            )
        except APIError as e:
            # Check if already connected (idempotent)
            if "already exists" in str(e).lower() or "endpoint with name" in str(e).lower():
                logger.info(
                    "Container '%s' already attached to network '%s'",
                    container_id,
                    self.name,
                )
                return
            raise NetworkError(
                "attach", self.name, f"Failed to attach container '{container_id}': {e}"
            ) from e

    def detach(self, container: Any) -> None:
        """Detach a container from this network.

        Args:
            container: Container to detach. Can be a container ID (str),
                container name (str), or a Docker container object.

        Raises:
            NetworkError: If detach operation fails.

        Example:
            >>> network.detach("my-container")
            >>> network.detach(container_obj)
        """
        container_id = self._resolve_container_id(container)

        logger.info("Detaching container '%s' from network '%s'", container_id, self.name)

        try:
            docker_network = self._get_docker_network()
            docker_network.disconnect(container_id)
            logger.info(
                "Successfully detached container '%s' from network '%s'",
                container_id,
                self.name,
            )
        except APIError as e:
            # Check if not connected (idempotent)
            if "is not connected" in str(e).lower() or "not found" in str(e).lower():
                logger.info(
                    "Container '%s' was not attached to network '%s'",
                    container_id,
                    self.name,
                )
                return
            raise NetworkError(
                "detach", self.name, f"Failed to detach container '{container_id}': {e}"
            ) from e

    def _resolve_container_id(self, container: Any) -> str:
        """Resolve container to its ID.

        Args:
            container: Container ID, name, or Docker container object.

        Returns:
            Container ID string.
        """
        if isinstance(container, str):
            return container
        # Duck typing: if it has an 'id' attribute, use it
        if hasattr(container, "id"):
            return container.id
        # If it has a 'container_id' attribute (our Container class)
        if hasattr(container, "container_id"):
            return container.container_id
        raise NetworkError(
            "resolve",
            self.name,
            f"Cannot resolve container: {type(container).__name__}",
        )

    def destroy(self) -> None:
        """Remove this network.

        Removes the Docker network. If the network doesn't exist,
        this operation is a no-op (idempotent).

        Raises:
            NetworkError: If network removal fails (except for not found).

        Example:
            >>> network.destroy()
        """
        if not self.network_id:
            logger.warning("Cannot destroy network '%s': no network_id", self.name)
            return

        logger.info("Destroying network '%s' (ID: %s)", self.name, self.network_id)

        client = self._get_client()
        try:
            docker_network = client.networks.get(self.network_id)
            docker_network.remove()
            logger.info("Successfully destroyed network '%s'", self.name)
        except NotFound:
            logger.info("Network '%s' already destroyed or doesn't exist", self.name)
        except APIError as e:
            raise NetworkError("destroy", self.name, f"Docker API error: {e}") from e

    def exists(self) -> bool:
        """Check if this network still exists.

        Returns:
            True if the network exists in Docker, False otherwise.

        Example:
            >>> network = Network.create("my-network")
            >>> network.exists()
            True
            >>> network.destroy()
            >>> network.exists()
            False
        """
        if not self.network_id:
            return False

        client = self._get_client()
        try:
            client.networks.get(self.network_id)
            return True
        except NotFound:
            return False
        # Let APIError propagate - callers should know about Docker daemon issues

    def to_docker_network_config(self) -> dict[str, Any]:
        """Convert network to Docker SDK network configuration.

        Returns a dictionary suitable for passing to docker-py's
        networks.create() method.

        Returns:
            Dictionary with Docker network configuration.

        Example:
            >>> network = Network(name="my-net", internal=True)
            >>> config = network.to_docker_network_config()
            >>> config["internal"]
            True
        """
        return {
            "name": self.name,
            "driver": self.driver,
            "internal": self.internal,
            "check_duplicate": True,
        }

    def with_client(self, client: DockerClient) -> Network:
        """Return a new Network with the specified Docker client.

        Useful for testing with mock clients.

        Args:
            client: Docker client to use.

        Returns:
            New Network instance with the client set.
        """
        new_network = self.model_copy()
        object.__setattr__(new_network, "_client", client)
        return new_network

    # =========================================================================
    # Async Methods
    # =========================================================================
    # These methods wrap the synchronous Docker SDK calls using anyio.to_thread.run_sync()
    # to make them safe for use in async contexts without blocking the event loop.

    @classmethod
    async def async_create(
        cls,
        name: str,
        internal: bool = False,
        driver: str = "bridge",
        client: DockerClient | None = None,
    ) -> Network:
        """Create a Docker network asynchronously.

        This is the async version of create() that runs the blocking Docker SDK
        calls in a thread pool using anyio.to_thread.run_sync().

        Args:
            name: Name of the network.
            internal: If True, blocks external internet access.
            driver: Network driver (default: "bridge").
            client: Optional Docker client (for testing/mocking).

        Returns:
            Network instance with network_id set.

        Raises:
            NetworkError: If network creation fails.

        Example:
            >>> network = await Network.async_create("env-net", internal=True)
        """
        return await anyio.to_thread.run_sync(
            lambda: cls.create(name=name, internal=internal, driver=driver, client=client)
        )

    async def async_attach(self, container: Any) -> None:
        """Attach a container to this network asynchronously.

        This is the async version of attach() that runs the blocking Docker SDK
        calls in a thread pool using anyio.to_thread.run_sync().

        Args:
            container: Container to attach. Can be a container ID (str),
                container name (str), or a Docker container object.

        Raises:
            NetworkError: If attach operation fails.

        Example:
            >>> await network.async_attach("my-container")
        """
        await anyio.to_thread.run_sync(lambda: self.attach(container))

    async def async_detach(self, container: Any) -> None:
        """Detach a container from this network asynchronously.

        This is the async version of detach() that runs the blocking Docker SDK
        calls in a thread pool using anyio.to_thread.run_sync().

        Args:
            container: Container to detach. Can be a container ID (str),
                container name (str), or a Docker container object.

        Raises:
            NetworkError: If detach operation fails.

        Example:
            >>> await network.async_detach("my-container")
        """
        await anyio.to_thread.run_sync(lambda: self.detach(container))

    async def async_destroy(self) -> None:
        """Remove this network asynchronously.

        This is the async version of destroy() that runs the blocking Docker SDK
        calls in a thread pool using anyio.to_thread.run_sync().

        Raises:
            NetworkError: If network removal fails (except for not found).

        Example:
            >>> await network.async_destroy()
        """
        await anyio.to_thread.run_sync(self.destroy)
