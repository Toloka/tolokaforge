"""Health Probe module for Docker Foundation Layer.

Provides configurable health check probes for containers: HTTP, TCP, gRPC, and command.
Uses Pydantic BaseModel for validation and tenacity for retry logic.

Async Support:
    The async_wait() method uses tenacity's AsyncRetrying for async retry logic,
    making it safe to use in async contexts without blocking the event loop.
"""

from __future__ import annotations

import logging
import socket
import subprocess
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from enum import Enum

import anyio
import grpc
from grpc_health.v1 import health_pb2, health_pb2_grpc
from pydantic import BaseModel, Field, field_validator
from tenacity import (
    AsyncRetrying,
    RetryError,
    Retrying,
    retry_if_exception_type,
    stop_after_delay,
    wait_fixed,
)

logger = logging.getLogger(__name__)


class ProbeType(str, Enum):
    """Types of health probes.

    This enum provides type-safe probe type names.
    Values are lowercase strings matching probe types.
    """

    HTTP = "http"
    TCP = "tcp"
    GRPC = "grpc"
    COMMAND = "command"


class ProbeResult(BaseModel):
    """Result of a health probe check.

    Attributes:
        healthy: Whether the probe succeeded.
        message: Human-readable status message.
        attempts: Number of attempts made.
        elapsed_s: Total time elapsed in seconds.
    """

    healthy: bool
    message: str
    attempts: int = 1
    elapsed_s: float = 0.0

    model_config = {
        "frozen": True,
        "extra": "forbid",
    }


class HealthProbeError(Exception):
    """Raised when a health probe fails after all retries."""

    def __init__(self, probe_type: ProbeType, target: str, message: str, attempts: int = 0):
        self.probe_type = probe_type
        self.target = target
        self.attempts = attempts
        super().__init__(f"{probe_type.value} probe failed for {target}: {message}")


class HealthProbe(BaseModel, ABC):
    """Base health probe configuration.

    This abstract model defines the common configuration for all health probes.
    Subclasses implement specific probe types (HTTP, TCP, gRPC, command).

    Attributes:
        interval_s: Time between retries in seconds.
        timeout_s: Total timeout before giving up in seconds.
        probe_type: Type of probe (set by subclasses).

    Example:
        >>> probe = HealthProbe.http("http://localhost:8000/health")
        >>> probe.wait()  # Blocks until healthy or timeout
    """

    interval_s: float = Field(
        default=1.0,
        gt=0.0,
        description="Time between retries in seconds",
    )
    timeout_s: float = Field(
        default=30.0,
        gt=0.0,
        description="Total timeout before giving up in seconds",
    )
    probe_type: ProbeType = Field(
        description="Type of health probe",
    )

    model_config = {
        "frozen": True,
        "extra": "forbid",
    }

    @abstractmethod
    def _check(self) -> bool:
        """Perform a single health check.

        Returns:
            True if healthy, False otherwise.

        Raises:
            Exception: If the check fails with an error.
        """
        ...

    @abstractmethod
    def _get_target(self) -> str:
        """Get a human-readable target description for logging."""
        ...

    def wait(self) -> ProbeResult:
        """Block until healthy or timeout.

        Performs health checks with retries using tenacity.
        Logs progress using the logging module.

        Returns:
            ProbeResult with the outcome.

        Raises:
            HealthProbeError: If the probe fails after all retries.
        """
        import time

        target = self._get_target()
        start_time = time.monotonic()
        attempts = 0

        logger.info(
            "Starting %s health probe for %s (timeout=%ss, interval=%ss)",
            self.probe_type.value,
            target,
            self.timeout_s,
            self.interval_s,
        )

        try:
            for attempt in Retrying(
                stop=stop_after_delay(self.timeout_s),
                wait=wait_fixed(self.interval_s),
                retry=retry_if_exception_type((Exception,)),
                reraise=True,
            ):
                with attempt:
                    attempts += 1
                    if self._check():
                        elapsed = time.monotonic() - start_time
                        logger.info(
                            "%s probe succeeded for %s after %d attempts (%.2fs)",
                            self.probe_type.value,
                            target,
                            attempts,
                            elapsed,
                        )
                        return ProbeResult(
                            healthy=True,
                            message=f"Healthy after {attempts} attempts",
                            attempts=attempts,
                            elapsed_s=elapsed,
                        )
                    else:
                        raise HealthProbeError(
                            self.probe_type,
                            target,
                            "Check returned False",
                            attempts,
                        )
        except RetryError as e:
            elapsed = time.monotonic() - start_time
            logger.error(
                "%s probe failed for %s after %d attempts (%.2fs): %s",
                self.probe_type.value,
                target,
                attempts,
                elapsed,
                str(e.last_attempt.exception()) if e.last_attempt.exception() else "timeout",
            )
            raise HealthProbeError(
                self.probe_type,
                target,
                f"Timed out after {self.timeout_s}s ({attempts} attempts)",
                attempts,
            ) from e

        # Should not reach here, but satisfy type checker
        elapsed = time.monotonic() - start_time
        return ProbeResult(
            healthy=False,
            message="Unexpected exit from retry loop",
            attempts=attempts,
            elapsed_s=elapsed,
        )

    async def async_wait(self) -> ProbeResult:
        """Asynchronously wait until healthy or timeout.

        This is the async version of wait() that uses tenacity's AsyncRetrying
        for async retry logic. The actual health check (_check) is run in a
        thread pool using anyio.to_thread.run_sync() to avoid blocking the
        event loop.

        Returns:
            ProbeResult with the outcome.

        Raises:
            HealthProbeError: If the probe fails after all retries.

        Example:
            >>> probe = HealthProbe.http("http://localhost:8000/health")
            >>> result = await probe.async_wait()
            >>> if result.healthy:
            ...     print("Service is ready!")
        """
        import time

        target = self._get_target()
        start_time = time.monotonic()
        attempts = 0

        logger.info(
            "Starting async %s health probe for %s (timeout=%ss, interval=%ss)",
            self.probe_type.value,
            target,
            self.timeout_s,
            self.interval_s,
        )

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_delay(self.timeout_s),
                wait=wait_fixed(self.interval_s),
                retry=retry_if_exception_type((Exception,)),
                reraise=True,
            ):
                with attempt:
                    attempts += 1
                    # Run the blocking _check() in a thread pool
                    check_result = await anyio.to_thread.run_sync(self._check)
                    if check_result:
                        elapsed = time.monotonic() - start_time
                        logger.info(
                            "Async %s probe succeeded for %s after %d attempts (%.2fs)",
                            self.probe_type.value,
                            target,
                            attempts,
                            elapsed,
                        )
                        return ProbeResult(
                            healthy=True,
                            message=f"Healthy after {attempts} attempts",
                            attempts=attempts,
                            elapsed_s=elapsed,
                        )
                    else:
                        raise HealthProbeError(
                            self.probe_type,
                            target,
                            "Check returned False",
                            attempts,
                        )
        except RetryError as e:
            elapsed = time.monotonic() - start_time
            logger.error(
                "Async %s probe failed for %s after %d attempts (%.2fs): %s",
                self.probe_type.value,
                target,
                attempts,
                elapsed,
                str(e.last_attempt.exception()) if e.last_attempt.exception() else "timeout",
            )
            raise HealthProbeError(
                self.probe_type,
                target,
                f"Timed out after {self.timeout_s}s ({attempts} attempts)",
                attempts,
            ) from e

        # Should not reach here, but satisfy type checker
        elapsed = time.monotonic() - start_time
        return ProbeResult(
            healthy=False,
            message="Unexpected exit from retry loop",
            attempts=attempts,
            elapsed_s=elapsed,
        )

    @staticmethod
    def http(
        url: str,
        expected_status: int = 200,
        interval_s: float = 1.0,
        timeout_s: float = 30.0,
    ) -> HttpHealthProbe:
        """Create an HTTP GET probe.

        Checks for expected status code from an HTTP GET request.

        Args:
            url: URL to check (must include protocol).
            expected_status: Expected HTTP status code (default 200).
            interval_s: Time between retries in seconds.
            timeout_s: Total timeout before giving up in seconds.

        Returns:
            HttpHealthProbe configured for the URL.

        Example:
            >>> probe = HealthProbe.http("http://localhost:8000/health")
            >>> probe.wait()
        """
        return HttpHealthProbe(
            url=url,
            expected_status=expected_status,
            interval_s=interval_s,
            timeout_s=timeout_s,
        )

    @staticmethod
    def tcp(
        host: str,
        port: int,
        interval_s: float = 1.0,
        timeout_s: float = 30.0,
    ) -> TcpHealthProbe:
        """Create a TCP connection probe.

        Checks that the port is accepting connections.

        Args:
            host: Hostname or IP address.
            port: Port number.
            interval_s: Time between retries in seconds.
            timeout_s: Total timeout before giving up in seconds.

        Returns:
            TcpHealthProbe configured for the host:port.

        Example:
            >>> probe = HealthProbe.tcp("localhost", 5432)
            >>> probe.wait()
        """
        return TcpHealthProbe(
            host=host,
            port=port,
            interval_s=interval_s,
            timeout_s=timeout_s,
        )

    @staticmethod
    def grpc(
        host: str,
        port: int,
        interval_s: float = 1.0,
        timeout_s: float = 30.0,
    ) -> GrpcHealthProbe:
        """Create a gRPC health check probe.

        Uses the standard gRPC health checking protocol.

        Args:
            host: Hostname or IP address.
            port: Port number.
            interval_s: Time between retries in seconds.
            timeout_s: Total timeout before giving up in seconds.

        Returns:
            GrpcHealthProbe configured for the host:port.

        Example:
            >>> probe = HealthProbe.grpc("localhost", 50051)
            >>> probe.wait()
        """
        return GrpcHealthProbe(
            host=host,
            port=port,
            interval_s=interval_s,
            timeout_s=timeout_s,
        )

    @staticmethod
    def command(
        command: str | list[str],
        expected_exit_code: int = 0,
        interval_s: float = 1.0,
        timeout_s: float = 30.0,
    ) -> CommandHealthProbe:
        """Create a command execution probe.

        Runs a command and checks the exit code.

        Args:
            command: Command to run (string or list of args).
            expected_exit_code: Expected exit code (default 0).
            interval_s: Time between retries in seconds.
            timeout_s: Total timeout before giving up in seconds.

        Returns:
            CommandHealthProbe configured for the command.

        Example:
            >>> probe = HealthProbe.command(["pg_isready", "-h", "localhost"])
            >>> probe.wait()
        """
        return CommandHealthProbe(
            command=command if isinstance(command, list) else [command],
            expected_exit_code=expected_exit_code,
            interval_s=interval_s,
            timeout_s=timeout_s,
        )


class HttpHealthProbe(HealthProbe):
    """HTTP GET health probe.

    Performs HTTP GET requests and checks for expected status code.

    Attributes:
        url: URL to check (must include protocol).
        expected_status: Expected HTTP status code.
        request_timeout_s: Timeout for individual HTTP requests.
    """

    url: str = Field(
        description="URL to check (must include protocol)",
    )
    expected_status: int = Field(
        default=200,
        ge=100,
        le=599,
        description="Expected HTTP status code",
    )
    request_timeout_s: float = Field(
        default=5.0,
        gt=0.0,
        description="Timeout for individual HTTP requests",
    )
    probe_type: ProbeType = Field(
        default=ProbeType.HTTP,
        description="Type of health probe",
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        """Validate that URL has a protocol."""
        v = v.strip()
        if not v:
            raise ValueError("URL cannot be empty")
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"URL must include protocol (http:// or https://), got: {v!r}")
        return v

    def _get_target(self) -> str:
        """Get target description for logging."""
        return self.url

    def _check(self) -> bool:
        """Perform HTTP GET and check status code."""
        return self._do_http_check()

    def _do_http_check(self) -> bool:
        """Perform the actual HTTP check (mockable in tests).

        Returns:
            True if status matches expected_status, False otherwise.

        Raises:
            urllib.error.URLError: If connection fails.
        """
        try:
            with urllib.request.urlopen(self.url, timeout=self.request_timeout_s) as response:
                status = response.status
                if status == self.expected_status:
                    logger.debug(
                        "HTTP %s returned %d (expected %d)", self.url, status, self.expected_status
                    )
                    return True
                else:
                    logger.debug(
                        "HTTP %s returned %d, expected %d",
                        self.url,
                        status,
                        self.expected_status,
                    )
                    raise HealthProbeError(
                        ProbeType.HTTP,
                        self.url,
                        f"Got status {status}, expected {self.expected_status}",
                    )
        except urllib.error.HTTPError as e:
            # HTTPError has a status code
            if e.code == self.expected_status:
                return True
            logger.debug("HTTP %s returned error %d", self.url, e.code)
            raise HealthProbeError(
                ProbeType.HTTP,
                self.url,
                f"Got status {e.code}, expected {self.expected_status}",
            ) from e
        except urllib.error.URLError as e:
            logger.debug("HTTP %s connection failed: %s", self.url, e.reason)
            raise HealthProbeError(
                ProbeType.HTTP,
                self.url,
                f"Connection failed: {e.reason}",
            ) from e


class TcpHealthProbe(HealthProbe):
    """TCP connection health probe.

    Attempts to establish a TCP connection to verify the port is accepting connections.

    Attributes:
        host: Hostname or IP address.
        port: Port number.
        connect_timeout_s: Timeout for individual connection attempts.
    """

    host: str = Field(
        description="Hostname or IP address",
    )
    port: int = Field(
        ge=1,
        le=65535,
        description="Port number",
    )
    connect_timeout_s: float = Field(
        default=5.0,
        gt=0.0,
        description="Timeout for individual connection attempts",
    )
    probe_type: ProbeType = Field(
        default=ProbeType.TCP,
        description="Type of health probe",
    )

    @field_validator("host")
    @classmethod
    def validate_host(cls, v: str) -> str:
        """Validate that host is not empty."""
        v = v.strip()
        if not v:
            raise ValueError("Host cannot be empty")
        return v

    def _get_target(self) -> str:
        """Get target description for logging."""
        return f"{self.host}:{self.port}"

    def _check(self) -> bool:
        """Attempt TCP connection."""
        return self._do_tcp_check()

    def _do_tcp_check(self) -> bool:
        """Perform the actual TCP check (mockable in tests).

        Returns:
            True if connection succeeds, False otherwise.

        Raises:
            socket.error: If connection fails.
        """
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self.connect_timeout_s)
            sock.connect((self.host, self.port))
            logger.debug("TCP connection to %s:%d succeeded", self.host, self.port)
            return True
        except OSError as e:
            logger.debug("TCP connection to %s:%d failed: %s", self.host, self.port, e)
            raise HealthProbeError(
                ProbeType.TCP,
                f"{self.host}:{self.port}",
                f"Connection failed: {e}",
            ) from e
        finally:
            if sock:
                sock.close()


class GrpcHealthProbe(HealthProbe):
    """gRPC health check probe.

    Uses the standard gRPC health checking protocol (grpc.health.v1.Health).

    Attributes:
        host: Hostname or IP address.
        port: Port number.
        service: Service name to check (empty string for overall health).
        rpc_timeout_s: Timeout for individual RPC calls.
    """

    host: str = Field(
        description="Hostname or IP address",
    )
    port: int = Field(
        ge=1,
        le=65535,
        description="Port number",
    )
    service: str = Field(
        default="",
        description="Service name to check (empty for overall health)",
    )
    rpc_timeout_s: float = Field(
        default=5.0,
        gt=0.0,
        description="Timeout for individual RPC calls",
    )
    probe_type: ProbeType = Field(
        default=ProbeType.GRPC,
        description="Type of health probe",
    )

    @field_validator("host")
    @classmethod
    def validate_host(cls, v: str) -> str:
        """Validate that host is not empty."""
        v = v.strip()
        if not v:
            raise ValueError("Host cannot be empty")
        return v

    def _get_target(self) -> str:
        """Get target description for logging."""
        if self.service:
            return f"{self.host}:{self.port}/{self.service}"
        return f"{self.host}:{self.port}"

    def _check(self) -> bool:
        """Perform gRPC health check."""
        return self._do_grpc_check()

    def _do_grpc_check(self) -> bool:
        """Perform the actual gRPC check (mockable in tests).

        Returns:
            True if service is SERVING, False otherwise.

        Raises:
            grpc.RpcError: If RPC fails.
        """
        channel = None
        try:
            address = f"{self.host}:{self.port}"
            channel = grpc.insecure_channel(address)
            stub = health_pb2_grpc.HealthStub(channel)
            request = health_pb2.HealthCheckRequest(service=self.service)
            response = stub.Check(request, timeout=self.rpc_timeout_s)  # type: ignore[attr-defined]

            if response.status == health_pb2.HealthCheckResponse.SERVING:
                logger.debug("gRPC health check for %s succeeded (SERVING)", self._get_target())
                return True
            else:
                status_name = health_pb2.HealthCheckResponse.ServingStatus.Name(response.status)
                logger.debug(
                    "gRPC health check for %s returned %s", self._get_target(), status_name
                )
                raise HealthProbeError(
                    ProbeType.GRPC,
                    self._get_target(),
                    f"Service status: {status_name}",
                )
        except grpc.RpcError as e:
            logger.debug("gRPC health check for %s failed: %s", self._get_target(), e)
            raise HealthProbeError(
                ProbeType.GRPC,
                self._get_target(),
                f"RPC failed: {e}",
            ) from e
        finally:
            if channel:
                channel.close()


class CommandHealthProbe(HealthProbe):
    """Command execution health probe.

    Runs a command and checks the exit code.

    Attributes:
        command: Command to run as a list of arguments.
        expected_exit_code: Expected exit code.
        exec_timeout_s: Timeout for command execution.
    """

    command: list[str] = Field(
        description="Command to run as a list of arguments",
    )
    expected_exit_code: int = Field(
        default=0,
        ge=0,
        le=255,
        description="Expected exit code",
    )
    exec_timeout_s: float = Field(
        default=10.0,
        gt=0.0,
        description="Timeout for command execution",
    )
    probe_type: ProbeType = Field(
        default=ProbeType.COMMAND,
        description="Type of health probe",
    )

    @field_validator("command")
    @classmethod
    def validate_command(cls, v: list[str]) -> list[str]:
        """Validate that command is not empty."""
        if not v:
            raise ValueError("Command cannot be empty")
        if not v[0]:
            raise ValueError("Command executable cannot be empty")
        return v

    def _get_target(self) -> str:
        """Get target description for logging."""
        return " ".join(self.command)

    def _check(self) -> bool:
        """Execute command and check exit code."""
        return self._do_command_check()

    def _do_command_check(self) -> bool:
        """Perform the actual command check (mockable in tests).

        Returns:
            True if exit code matches expected, False otherwise.

        Raises:
            subprocess.SubprocessError: If command execution fails.
        """
        try:
            result = subprocess.run(
                self.command,
                capture_output=True,
                timeout=self.exec_timeout_s,
                check=False,
            )
            if result.returncode == self.expected_exit_code:
                logger.debug(
                    "Command '%s' returned %d (expected %d)",
                    self._get_target(),
                    result.returncode,
                    self.expected_exit_code,
                )
                return True
            else:
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                logger.debug(
                    "Command '%s' returned %d, expected %d: %s",
                    self._get_target(),
                    result.returncode,
                    self.expected_exit_code,
                    stderr,
                )
                raise HealthProbeError(
                    ProbeType.COMMAND,
                    self._get_target(),
                    f"Exit code {result.returncode}, expected {self.expected_exit_code}",
                )
        except subprocess.TimeoutExpired as e:
            logger.debug(
                "Command '%s' timed out after %ss", self._get_target(), self.exec_timeout_s
            )
            raise HealthProbeError(
                ProbeType.COMMAND,
                self._get_target(),
                f"Command timed out after {self.exec_timeout_s}s",
            ) from e
        except FileNotFoundError as e:
            logger.debug("Command '%s' not found: %s", self._get_target(), e)
            raise HealthProbeError(
                ProbeType.COMMAND,
                self._get_target(),
                f"Command not found: {self.command[0]}",
            ) from e


# Type alias for any health probe
AnyHealthProbe = HttpHealthProbe | TcpHealthProbe | GrpcHealthProbe | CommandHealthProbe
