"""TypeSense server manager using Docker foundation layer.

This module provides orchestrator-managed TypeSense server lifecycle management.
In local mode, it automatically starts a Docker container before trials run and
stops it after completion.

Uses the tolokaforge.docker ServiceStack and typesense_service() for container
lifecycle, replacing raw Docker SDK calls.
"""

import logging
import secrets
from pathlib import Path

import httpx

# Check if Docker foundation layer is available
try:
    from tolokaforge.docker.stack import ServiceStack  # noqa: F401

    DOCKER_AVAILABLE = True
except ImportError:
    DOCKER_AVAILABLE = False

logger = logging.getLogger(__name__)


def find_free_port(start: int = 8108, end: int = 8200) -> int:
    """Find an available port in the specified range.

    Args:
        start: Start of port range (inclusive)
        end: End of port range (exclusive)

    Returns:
        An available port number

    Raises:
        RuntimeError: If no free port found in range
    """
    import socket

    for port in range(start, end):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    raise RuntimeError(f"No free port found in range {start}-{end}")


def generate_api_key() -> str:
    """Generate a secure random API key for TypeSense.

    Returns:
        A 43-character URL-safe random string
    """
    return secrets.token_urlsafe(32)


class TypeSenseServerManager:
    """Manages TypeSense server lifecycle using Docker foundation layer.

    This class handles starting, stopping, and monitoring a TypeSense Docker
    container for local development and testing. Uses ServiceStack from the
    tolokaforge.docker foundation layer for container management.

    Supports context manager protocol for automatic cleanup.

    Example:
        ```python
        with TypeSenseServerManager(port="auto") as server:
            # Server is running, use server.port and server.api_key
            provider = create_typesense_provider(
                port=server.port,
                api_key=server.api_key
            )
        # Server automatically stopped
        ```

    Attributes:
        host: TypeSense server host
        port: TypeSense server port (resolved from "auto" if needed)
        api_key: TypeSense API key (generated if not provided)
        data_dir: Data directory path for TypeSense storage
        image: Docker image to use
        container_name: Name for the Docker container
        timeout: Connection timeout in seconds
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int | str = "auto",
        api_key: str | None = None,
        data_dir: str = ".cache/typesense",
        image: str = "typesense/typesense:26.0",
        container_name: str = "tolokaforge-typesense",
        timeout: float = 30.0,
        cleanup_on_exit: bool = True,
    ):
        """Initialize TypeSense server manager.

        Args:
            host: Server host (usually 127.0.0.1 for local mode)
            port: Server port or "auto" to find available port
            api_key: API key (auto-generated if None)
            data_dir: Directory for TypeSense data persistence
            image: Docker image (e.g., "typesense/typesense:26.0")
            container_name: Name for the Docker container
            timeout: Connection/health check timeout in seconds
            cleanup_on_exit: Whether to remove container on exit
        """
        self.host = host
        self._requested_port = port
        self.port: int = -1  # Will be set when started
        self.api_key = api_key or generate_api_key()
        self.data_dir = Path(data_dir).resolve()
        self.image = image
        self.container_name = container_name
        self.timeout = timeout
        self.cleanup_on_exit = cleanup_on_exit

        self._stack = None

    def _resolve_port(self) -> int:
        """Resolve port from "auto" or integer."""
        if self._requested_port == "auto":
            return find_free_port()
        return int(self._requested_port)

    def _ensure_data_dir(self) -> None:
        """Ensure data directory exists."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        logger.debug("TypeSense data directory: %s", self.data_dir)

    def start(self) -> bool:
        """Start TypeSense server container.

        Uses ServiceStack and typesense_service() from the foundation layer.

        Returns:
            True if server started successfully, False otherwise
        """
        try:
            from tolokaforge.docker.stack import ServiceStack
            from tolokaforge.docker.stacks.typesense import typesense_service
        except ImportError:
            logger.error(
                "Docker foundation layer not available - cannot start local TypeSense server"
            )
            return False

        try:
            # Resolve port
            self.port = self._resolve_port()

            # Ensure data directory exists
            self._ensure_data_dir()

            # Parse image tag from image string
            if ":" in self.image:
                image_tag = self.image.split(":")[-1]
            else:
                image_tag = "latest"

            # Create service definition using foundation layer
            svc_def = typesense_service(
                port=self.port,
                api_key=self.api_key,
                data_dir=str(self.data_dir),
                image_tag=image_tag,
            )

            # Pull the image so container creation doesn't fail with 404.
            # Retry on transient registry errors (504 Gateway Timeout, etc.)
            try:
                from tenacity import retry, stop_after_attempt, wait_exponential

                import docker as docker_sdk

                @retry(
                    stop=stop_after_attempt(5),
                    wait=wait_exponential(multiplier=10, min=10, max=60),
                    reraise=True,
                    before_sleep=lambda rs: logger.warning(
                        "Image pull attempt %d failed, retrying in %ds...",
                        rs.attempt_number,
                        rs.next_action.sleep,
                    ),
                )
                def _pull_with_retry() -> None:
                    docker_sdk.from_env().images.pull(self.image)

                logger.info("Pulling TypeSense image: %s", self.image)
                _pull_with_retry()
            except Exception as pull_err:
                raise RuntimeError(
                    f"Failed to pull TypeSense image '{self.image}': {pull_err}"
                ) from pull_err

            # Create and start the stack
            self._stack = ServiceStack(prefix=self.container_name)
            self._stack.add_service(svc_def)

            logger.info(
                "Starting TypeSense container: %s (port=%d, data_dir=%s)",
                self.container_name,
                self.port,
                self.data_dir,
            )

            self._stack.start_all(wait=False, build=False)

            # Wait for server to be ready using our custom wait logic
            # (more thorough than just the HTTP health probe)
            if not self.wait_ready():
                logger.error("TypeSense server failed to become ready")
                self.stop()
                return False

            logger.info(
                "TypeSense server ready at http://%s:%d",
                self.host,
                self.port,
            )
            return True

        except Exception as e:
            logger.error("Failed to start TypeSense server: %s", e)
            raise

    def stop(self) -> None:
        """Stop TypeSense server container."""
        if self._stack is None:
            return

        try:
            logger.info("Stopping TypeSense container: %s", self.container_name)
            if self.cleanup_on_exit:
                self._stack.destroy(remove_networks=True, remove_volumes=False)
            else:
                self._stack.stop_all()
            logger.info("TypeSense container stopped: %s", self.container_name)
        except Exception as e:
            logger.warning("Error stopping TypeSense container: %s", e)
        finally:
            self._stack = None

    def is_running(self) -> bool:
        """Check if TypeSense container is running.

        Returns:
            True if container is running, False otherwise
        """
        if self._stack is None:
            return False

        try:
            statuses = self._stack.get_status()
            return any(status.status in ("running", "ready") for status in statuses.values())
        except Exception:
            return False

    def wait_ready(self, timeout: float | None = None) -> bool:
        """Wait for TypeSense to be ready to accept connections.

        Performs two checks:
        1. Health endpoint is responding
        2. Collections API is accessible (ensures server is fully initialized)

        Args:
            timeout: Maximum wait time (uses self.timeout if None)

        Returns:
            True if server is ready, False if timeout reached
        """
        import time

        if timeout is None:
            timeout = self.timeout

        health_url = f"http://{self.host}:{self.port}/health"
        collections_url = f"http://{self.host}:{self.port}/collections"
        headers = {"X-TYPESENSE-API-KEY": self.api_key}
        start_time = time.time()

        # First wait for health endpoint
        while time.time() - start_time < timeout:
            try:
                response = httpx.get(health_url, timeout=2.0)
                if response.status_code == 200:
                    logger.debug("TypeSense health check passed")
                    break
            except Exception:
                pass

            # Check if container is still running
            if not self.is_running():
                logger.error("TypeSense container stopped unexpectedly")
                return False

            time.sleep(0.5)
        else:
            logger.error("TypeSense health check timeout after %ss", timeout)
            return False

        # Now wait for collections API to be ready
        # This ensures the server is fully initialized beyond just health check
        while time.time() - start_time < timeout:
            try:
                response = httpx.get(collections_url, headers=headers, timeout=2.0)
                if response.status_code == 200:
                    logger.debug("TypeSense collections API ready")
                    return True
            except Exception:
                pass

            if not self.is_running():
                logger.error("TypeSense container stopped unexpectedly")
                return False

            time.sleep(0.3)

        logger.error("TypeSense collections API timeout after %ss", timeout)
        return False

    def get_connection_info(self) -> dict:
        """Get connection information for TypeSense client.

        Returns:
            Dictionary with host, port, api_key, and timeout
        """
        return {
            "host": self.host,
            "port": self.port,
            "api_key": self.api_key,
            "timeout": self.timeout,
        }

    def __enter__(self) -> "TypeSenseServerManager":
        """Context manager entry - start server."""
        if not self.start():
            raise RuntimeError("Failed to start TypeSense server")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit - stop server."""
        self.stop()


def create_typesense_server(
    port: int | str = "auto",
    api_key: str | None = None,
    data_dir: str = ".cache/typesense",
    image: str = "typesense/typesense:26.0",
    container_name: str = "tolokaforge-typesense",
    timeout: float = 30.0,
    cleanup_on_exit: bool = True,
) -> TypeSenseServerManager:
    """Factory function to create a TypeSense server manager.

    Args:
        port: Server port or "auto" to find available port
        api_key: API key (auto-generated if None)
        data_dir: Directory for TypeSense data persistence
        image: Docker image
        container_name: Name for the Docker container
        timeout: Connection/health check timeout in seconds
        cleanup_on_exit: Whether to remove container on exit

    Returns:
        TypeSenseServerManager instance
    """
    return TypeSenseServerManager(
        host="127.0.0.1",
        port=port,
        api_key=api_key,
        data_dir=data_dir,
        image=image,
        container_name=container_name,
        timeout=timeout,
        cleanup_on_exit=cleanup_on_exit,
    )
