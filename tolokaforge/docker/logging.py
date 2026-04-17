"""LogRouter module for real-time container log streaming.

Provides LogRouter class that attaches to Docker containers and streams logs
to Python's logging system with structured context (container name, trial ID).
Supports rich console output formatting and optional file tee.

Features:
    - Attaches to container at start time
    - Streams logs to Python's logging system with structured context
    - Supports rich console output formatting
    - Optionally tees to log files
    - Both sync and async interfaces
    - Handles container crashes gracefully (reconnect or clean exit)

Example:
    >>> from tolokaforge.docker import Container, LogRouter
    >>>
    >>> # Create and start container
    >>> container = Container.create(image=image, name="my-app")
    >>> container.start()
    >>>
    >>> # Create and start log router
    >>> log_router = LogRouter(
    ...     container=container,
    ...     trial_id="trial-001",
    ...     log_file="/tmp/container.log",
    ... )
    >>> log_router.start()
    >>>
    >>> # ... do work ...
    >>>
    >>> # Stop log router and container
    >>> log_router.stop()
    >>> container.stop()
"""

from __future__ import annotations

import logging
import threading
from contextlib import suppress
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

import anyio
from docker.errors import APIError, NotFound
from pydantic import BaseModel, Field, PrivateAttr

if TYPE_CHECKING:
    from tolokaforge.docker.container import Container

logger = logging.getLogger(__name__)


class LogRouterState(str, Enum):
    """State of the LogRouter."""

    IDLE = "idle"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class LogRouterError(Exception):
    """Raised when a LogRouter operation fails."""

    def __init__(self, operation: str, container_name: str, message: str):
        self.operation = operation
        self.container_name = container_name
        super().__init__(f"LogRouter {operation} failed for '{container_name}': {message}")


class LogRouter(BaseModel):
    """Routes container logs to Python logging with structured context.

    LogRouter attaches to a Docker container and streams its logs to Python's
    logging system. Each log line is tagged with the container name and optional
    trial ID for multi-container scenarios.

    Attributes:
        container_name: Name of the container to stream logs from.
        container_id: Docker container ID.
        trial_id: Optional trial identifier for structured logging.
        log_file: Optional path to tee logs to a file.
        log_level: Logging level for container logs (default: INFO).
        use_rich: Whether to use rich formatting for console output.
        reconnect_on_failure: Whether to attempt reconnection on stream failure.
        reconnect_delay_s: Delay between reconnection attempts.
        max_reconnect_attempts: Maximum number of reconnection attempts (0 = unlimited).

    Example:
        >>> log_router = LogRouter(
        ...     container_name="my-container",
        ...     container_id="abc123",
        ...     trial_id="trial-001",
        ...     log_file="/tmp/container.log",
        ... )
        >>> log_router.start()
        >>> # ... container runs ...
        >>> log_router.stop()
    """

    container_name: str = Field(
        description="Name of the container to stream logs from",
    )
    container_id: str = Field(
        description="Docker container ID",
    )
    trial_id: str | None = Field(
        default=None,
        description="Optional trial identifier for structured logging",
    )
    log_file: str | None = Field(
        default=None,
        description="Optional path to tee logs to a file",
    )
    log_level: int = Field(
        default=logging.INFO,
        description="Logging level for container logs",
    )
    use_rich: bool = Field(
        default=True,
        description="Whether to use rich formatting for console output",
    )
    reconnect_on_failure: bool = Field(
        default=True,
        description="Whether to attempt reconnection on stream failure",
    )
    reconnect_delay_s: float = Field(
        default=1.0,
        description="Delay between reconnection attempts",
    )
    max_reconnect_attempts: int = Field(
        default=5,
        description="Maximum number of reconnection attempts (0 = unlimited)",
    )

    # Private attributes (not serialized)
    _state: LogRouterState = PrivateAttr(default=LogRouterState.IDLE)
    _thread: threading.Thread | None = PrivateAttr(default=None)
    _stop_event: threading.Event = PrivateAttr(default_factory=threading.Event)
    _file_handle: TextIO | None = PrivateAttr(default=None)
    _logger: logging.Logger = PrivateAttr(default=None)  # type: ignore[assignment]
    _reconnect_count: int = PrivateAttr(default=0)

    model_config = {
        "extra": "forbid",
        "arbitrary_types_allowed": True,
    }

    def model_post_init(self, __context: Any) -> None:
        """Initialize private attributes after model creation."""
        # Create a child logger for this container
        self._logger = logging.getLogger(f"container.{self.container_name}")

    @classmethod
    def for_container(
        cls,
        container: Container,
        trial_id: str | None = None,
        log_file: str | None = None,
        log_level: int = logging.INFO,
        use_rich: bool = True,
        reconnect_on_failure: bool = True,
        reconnect_delay_s: float = 1.0,
        max_reconnect_attempts: int = 5,
    ) -> LogRouter:
        """Create a LogRouter for a Container instance.

        Args:
            container: Container to stream logs from.
            trial_id: Optional trial identifier for structured logging.
            log_file: Optional path to tee logs to a file.
            log_level: Logging level for container logs.
            use_rich: Whether to use rich formatting for console output.
            reconnect_on_failure: Whether to attempt reconnection on stream failure.
            reconnect_delay_s: Delay between reconnection attempts.
            max_reconnect_attempts: Maximum number of reconnection attempts.

        Returns:
            LogRouter instance configured for the container.

        Raises:
            LogRouterError: If container has no container_id.
        """
        if not container.container_id:
            raise LogRouterError(
                "create",
                container.name,
                "Container has no container_id (not created yet)",
            )

        return cls(
            container_name=container.name,
            container_id=container.container_id,
            trial_id=trial_id,
            log_file=log_file,
            log_level=log_level,
            use_rich=use_rich,
            reconnect_on_failure=reconnect_on_failure,
            reconnect_delay_s=reconnect_delay_s,
            max_reconnect_attempts=max_reconnect_attempts,
        )

    @property
    def state(self) -> LogRouterState:
        """Return current state of the LogRouter."""
        return self._state

    @property
    def is_running(self) -> bool:
        """Return True if the LogRouter is currently running."""
        return self._state == LogRouterState.RUNNING

    def _get_extra_fields(self) -> dict[str, Any]:
        """Get extra fields for structured logging."""
        extra: dict[str, Any] = {
            "container_name": self.container_name,
        }
        if self.trial_id:
            extra["trial_id"] = self.trial_id
        return extra

    def _format_log_line(self, line: str) -> str:
        """Format a log line with container context.

        Args:
            line: Raw log line from container.

        Returns:
            Formatted log line with container prefix.
        """
        line = line.rstrip()
        if self.use_rich:
            # Use rich markup for colored output
            prefix = f"[bold blue][{self.container_name}][/bold blue]"
            if self.trial_id:
                prefix = f"[dim]{self.trial_id}[/dim] {prefix}"
            return f"{prefix} {line}"
        else:
            prefix = f"[{self.container_name}]"
            if self.trial_id:
                prefix = f"{self.trial_id} {prefix}"
            return f"{prefix} {line}"

    def _open_log_file(self) -> None:
        """Open the log file for writing if configured."""
        if self.log_file and self._file_handle is None:
            log_path = Path(self.log_file)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._file_handle = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
            logger.debug(
                "Opened log file '%s' for container '%s'",
                self.log_file,
                self.container_name,
            )

    def _close_log_file(self) -> None:
        """Close the log file if open."""
        if self._file_handle is not None:
            with suppress(Exception):
                self._file_handle.close()
            self._file_handle = None
            logger.debug(
                "Closed log file '%s' for container '%s'",
                self.log_file,
                self.container_name,
            )

    def _write_to_file(self, line: str) -> None:
        """Write a log line to the file if configured.

        Args:
            line: Log line to write.
        """
        if self._file_handle is not None:
            try:
                self._file_handle.write(line + "\n")
                self._file_handle.flush()
            except Exception as e:
                logger.warning(
                    "Failed to write to log file '%s': %s",
                    self.log_file,
                    e,
                )

    def _stream_logs(self) -> None:
        """Stream logs from the container (runs in background thread).

        This method connects to the Docker container's log stream and
        forwards each line to Python's logging system and optionally
        to a log file.
        """
        import docker

        try:
            client = docker.from_env()
        except Exception as e:
            logger.error(
                "Failed to connect to Docker for container '%s': %s",
                self.container_name,
                e,
            )
            self._state = LogRouterState.FAILED
            return

        self._open_log_file()
        extra = self._get_extra_fields()

        while not self._stop_event.is_set():
            try:
                docker_container = client.containers.get(self.container_id)

                # Stream logs with follow=True
                log_stream = docker_container.logs(
                    stream=True,
                    follow=True,
                    timestamps=False,
                )

                for chunk in log_stream:
                    if self._stop_event.is_set():
                        break

                    if isinstance(chunk, bytes):
                        line = chunk.decode("utf-8", errors="replace")
                    else:
                        line = str(chunk)

                    # Process each line
                    for single_line in line.splitlines():
                        if single_line.strip():
                            # Log to Python logging with extra fields
                            self._logger.log(
                                self.log_level,
                                single_line.rstrip(),
                                extra=extra,
                            )

                            # Write to file if configured
                            formatted = self._format_log_line(single_line)
                            self._write_to_file(formatted)

                # Stream ended normally (container stopped)
                logger.debug(
                    "Log stream ended for container '%s'",
                    self.container_name,
                )
                break

            except NotFound:
                logger.info(
                    "Container '%s' not found, stopping log router",
                    self.container_name,
                )
                break

            except APIError as e:
                if self._stop_event.is_set():
                    break

                logger.warning(
                    "Docker API error streaming logs for '%s': %s",
                    self.container_name,
                    e,
                )

                if not self._should_reconnect():
                    self._state = LogRouterState.FAILED
                    break

                self._wait_for_reconnect()

            except Exception as e:
                if self._stop_event.is_set():
                    break

                logger.warning(
                    "Error streaming logs for container '%s': %s",
                    self.container_name,
                    e,
                )

                if not self._should_reconnect():
                    self._state = LogRouterState.FAILED
                    break

                self._wait_for_reconnect()

        self._close_log_file()

        if self._state == LogRouterState.RUNNING:
            self._state = LogRouterState.STOPPED

    def _should_reconnect(self) -> bool:
        """Check if we should attempt to reconnect.

        Returns:
            True if reconnection should be attempted.
        """
        if not self.reconnect_on_failure:
            return False

        if self.max_reconnect_attempts > 0:
            if self._reconnect_count >= self.max_reconnect_attempts:
                logger.error(
                    "Max reconnect attempts (%d) reached for container '%s'",
                    self.max_reconnect_attempts,
                    self.container_name,
                )
                return False

        return True

    def _wait_for_reconnect(self) -> None:
        """Wait before attempting to reconnect."""
        self._reconnect_count += 1
        logger.info(
            "Reconnecting to container '%s' in %.1fs (attempt %d/%s)",
            self.container_name,
            self.reconnect_delay_s,
            self._reconnect_count,
            self.max_reconnect_attempts if self.max_reconnect_attempts > 0 else "∞",
        )
        self._stop_event.wait(self.reconnect_delay_s)

    def start(self) -> None:
        """Start streaming logs from the container.

        Starts a background thread that streams logs from the Docker container
        to Python's logging system.

        Raises:
            LogRouterError: If already running or failed to start.

        Example:
            >>> log_router.start()
        """
        if self._state == LogRouterState.RUNNING:
            logger.warning(
                "LogRouter for container '%s' is already running",
                self.container_name,
            )
            return

        if self._state == LogRouterState.STOPPING:
            raise LogRouterError(
                "start",
                self.container_name,
                "LogRouter is currently stopping",
            )

        logger.info(
            "Starting log router for container '%s'%s",
            self.container_name,
            f" (trial: {self.trial_id})" if self.trial_id else "",
        )

        self._stop_event.clear()
        self._reconnect_count = 0
        self._state = LogRouterState.RUNNING

        self._thread = threading.Thread(
            target=self._stream_logs,
            name=f"LogRouter-{self.container_name}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout_s: float = 5.0) -> None:
        """Stop streaming logs.

        Signals the background thread to stop and waits for it to finish.

        Args:
            timeout_s: Maximum time to wait for the thread to stop.

        Example:
            >>> log_router.stop()
        """
        if self._state not in (LogRouterState.RUNNING, LogRouterState.STOPPING):
            logger.debug(
                "LogRouter for container '%s' is not running (state: %s)",
                self.container_name,
                self._state.value,
            )
            return

        logger.info(
            "Stopping log router for container '%s'",
            self.container_name,
        )

        self._state = LogRouterState.STOPPING
        self._stop_event.set()

        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout_s)
            if self._thread.is_alive():
                logger.warning(
                    "LogRouter thread for container '%s' did not stop within %.1fs",
                    self.container_name,
                    timeout_s,
                )

        self._state = LogRouterState.STOPPED
        self._thread = None

        logger.info(
            "Log router stopped for container '%s'",
            self.container_name,
        )

    async def async_start(self) -> None:
        """Start streaming logs from the container (async version).

        This is the async version of start() that runs the blocking
        thread start in a thread pool.

        Raises:
            LogRouterError: If already running or failed to start.

        Example:
            >>> await log_router.async_start()
        """
        await anyio.to_thread.run_sync(self.start)

    async def async_stop(self, timeout_s: float = 5.0) -> None:
        """Stop streaming logs (async version).

        This is the async version of stop() that runs the blocking
        thread join in a thread pool.

        Args:
            timeout_s: Maximum time to wait for the thread to stop.

        Example:
            >>> await log_router.async_stop()
        """
        await anyio.to_thread.run_sync(lambda: self.stop(timeout_s=timeout_s))

    def __enter__(self) -> LogRouter:
        """Context manager entry - starts the log router."""
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit - stops the log router."""
        self.stop()

    async def __aenter__(self) -> LogRouter:
        """Async context manager entry - starts the log router."""
        await self.async_start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit - stops the log router."""
        await self.async_stop()


class ContainerLogAdapter(logging.LoggerAdapter):
    """LoggerAdapter that adds container context to log records.

    This adapter automatically adds container_name and trial_id to all
    log records, making it easy to filter and search logs by container.

    Example:
        >>> adapter = ContainerLogAdapter(
        ...     logger=logging.getLogger("myapp"),
        ...     container_name="my-container",
        ...     trial_id="trial-001",
        ... )
        >>> adapter.info("Processing request")
        # Logs: "Processing request" with extra={'container_name': 'my-container', 'trial_id': 'trial-001'}
    """

    def __init__(
        self,
        logger: logging.Logger,
        container_name: str,
        trial_id: str | None = None,
    ):
        """Initialize the adapter.

        Args:
            logger: Base logger to wrap.
            container_name: Name of the container.
            trial_id: Optional trial identifier.
        """
        extra = {"container_name": container_name}
        if trial_id:
            extra["trial_id"] = trial_id
        super().__init__(logger, extra)

    def process(self, msg: str, kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Process the logging call to add extra context.

        Args:
            msg: Log message.
            kwargs: Keyword arguments for the log call.

        Returns:
            Tuple of (message, kwargs) with extra context added.
        """
        # Merge our extra with any existing extra
        extra = kwargs.get("extra", {})
        extra.update(self.extra)
        kwargs["extra"] = extra
        return msg, kwargs


class ContainerLogFormatter(logging.Formatter):
    """Formatter that includes container context in log output.

    This formatter adds container_name and trial_id to the log output
    when available in the log record's extra fields.

    Example:
        >>> formatter = ContainerLogFormatter(
        ...     fmt="%(asctime)s [%(container_name)s] %(message)s",
        ...     include_trial_id=True,
        ... )
        >>> handler.setFormatter(formatter)
    """

    def __init__(
        self,
        fmt: str | None = None,
        datefmt: str | None = None,
        include_trial_id: bool = True,
        use_rich: bool = False,
    ):
        """Initialize the formatter.

        Args:
            fmt: Log format string. If None, uses a default format.
            datefmt: Date format string.
            include_trial_id: Whether to include trial_id in output.
            use_rich: Whether to use rich markup in output.
        """
        if fmt is None:
            if use_rich:
                fmt = "%(asctime)s [bold blue][%(container_name)s][/bold blue] %(message)s"
            else:
                fmt = "%(asctime)s [%(container_name)s] %(message)s"

        super().__init__(fmt=fmt, datefmt=datefmt)
        self.include_trial_id = include_trial_id
        self.use_rich = use_rich

    def format(self, record: logging.LogRecord) -> str:
        """Format the log record.

        Args:
            record: Log record to format.

        Returns:
            Formatted log string.
        """
        # Add default values for container fields if not present
        if not hasattr(record, "container_name"):
            record.container_name = "unknown"  # type: ignore[attr-defined]
        if not hasattr(record, "trial_id"):
            record.trial_id = None  # type: ignore[attr-defined]

        # Add trial_id prefix if configured and present
        if self.include_trial_id and getattr(record, "trial_id", None):
            if self.use_rich:
                record.msg = f"[dim]{record.trial_id}[/dim] {record.msg}"  # type: ignore[attr-defined]
            else:
                record.msg = f"{record.trial_id} {record.msg}"  # type: ignore[attr-defined]

        return super().format(record)


def setup_container_logging(
    level: int = logging.INFO,
    use_rich: bool = True,
    log_file: str | None = None,
) -> None:
    """Set up logging for container log output.

    Configures the 'container' logger hierarchy with appropriate
    handlers and formatters for container log output.

    Args:
        level: Logging level.
        use_rich: Whether to use rich console output.
        log_file: Optional path to log file.

    Example:
        >>> setup_container_logging(level=logging.DEBUG, use_rich=True)
    """
    container_logger = logging.getLogger("container")
    container_logger.setLevel(level)

    # Remove existing handlers
    container_logger.handlers.clear()

    # Console handler
    if use_rich:
        try:
            from rich.logging import RichHandler

            console_handler = RichHandler(
                show_time=True,
                show_path=False,
                markup=True,
                rich_tracebacks=True,
            )
            console_handler.setFormatter(
                ContainerLogFormatter(
                    fmt="%(message)s",
                    use_rich=True,
                )
            )
        except ImportError:
            # Fall back to standard handler if rich not available
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(ContainerLogFormatter(use_rich=False))
    else:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(ContainerLogFormatter(use_rich=False))

    console_handler.setLevel(level)
    container_logger.addHandler(console_handler)

    # File handler
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(
            ContainerLogFormatter(
                fmt="%(asctime)s [%(container_name)s] %(message)s",
                use_rich=False,
            )
        )
        file_handler.setLevel(level)
        container_logger.addHandler(file_handler)

    # Don't propagate to root logger
    container_logger.propagate = False

    logger.debug(
        "Container logging configured (level=%s, rich=%s, file=%s)",
        logging.getLevelName(level),
        use_rich,
        log_file,
    )
