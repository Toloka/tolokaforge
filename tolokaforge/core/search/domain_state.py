"""
Domain state management for TypeSense initialization.

This module provides thread-safe coordination for domain initialization,
ensuring that multiple concurrent callers properly coordinate initialization
without race conditions.

Designed to be portable to Docker-based architecture where coordination
might happen via external services (Redis, etcd, etc.) in the future.
"""

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from tolokaforge.core.logging import get_logger

logger = get_logger(__name__)


class DomainStatus(Enum):
    """Status of domain TypeSense initialization."""

    PENDING = "pending"  # Not yet started
    INITIALIZING = "initializing"  # In progress
    READY = "ready"  # Successfully initialized
    FAILED = "failed"  # Initialization failed


@dataclass
class DomainState:
    """
    State tracking for a domain's TypeSense initialization.

    Uses a condition variable for efficient waiting - threads can block
    until the domain reaches READY or FAILED state.
    """

    domain_name: str
    status: DomainStatus = DomainStatus.PENDING
    error: str | None = None
    document_count: int = 0
    condition: threading.Condition = field(default_factory=threading.Condition)

    def claim_initialization(self) -> bool:
        """
        Atomically claim initialization rights.

        Only one thread can successfully claim - the one that transitions
        from PENDING to INITIALIZING. This is the key race condition fix.

        Returns:
            True if this thread should perform initialization, False otherwise
        """
        with self.condition:
            if self.status == DomainStatus.PENDING:
                self.status = DomainStatus.INITIALIZING
                logger.debug(
                    f"Domain '{self.domain_name}' initialization claimed by current thread"
                )
                return True
            logger.debug(
                f"Domain '{self.domain_name}' initialization already claimed, status={self.status.value}"
            )
            return False

    def wait_ready(self, timeout: float = 30.0) -> bool:
        """
        Wait for domain to reach READY or FAILED state.

        Args:
            timeout: Maximum time to wait in seconds

        Returns:
            True if READY, False otherwise

        Raises:
            TimeoutError: If timeout exceeded
            RuntimeError: If initialization failed
        """
        with self.condition:
            # Fast path: already in terminal state
            if self.status == DomainStatus.READY:
                return True
            if self.status == DomainStatus.FAILED:
                raise RuntimeError(
                    f"TypeSense initialization failed for domain '{self.domain_name}': {self.error}"
                )

            # Wait until not PENDING/INITIALIZING
            if not self.condition.wait_for(
                lambda: self.status in (DomainStatus.READY, DomainStatus.FAILED),
                timeout=timeout,
            ):
                raise TimeoutError(
                    f"TypeSense initialization for domain '{self.domain_name}' "
                    f"timed out after {timeout}s"
                )

            if self.status == DomainStatus.FAILED:
                raise RuntimeError(
                    f"TypeSense initialization failed for domain '{self.domain_name}': {self.error}"
                )

            return True

    def set_ready(self, document_count: int) -> None:
        """Mark domain as ready with document count."""
        with self.condition:
            self.status = DomainStatus.READY
            self.document_count = document_count
            self.condition.notify_all()
            logger.debug(
                f"Domain '{self.domain_name}' marked READY with {document_count} documents"
            )

    def set_failed(self, error: str) -> None:
        """Mark domain as failed with error message."""
        with self.condition:
            self.status = DomainStatus.FAILED
            self.error = error
            self.condition.notify_all()
            logger.debug(f"Domain '{self.domain_name}' marked FAILED: {error}")


class DomainStateManager:
    """
    Thread-safe manager for domain initialization states.

    Provides atomic get-or-create operations for domain states and
    optional background initialization via thread pool.

    Designed to be portable to Docker-based architecture where
    coordination might happen via external services.
    """

    def __init__(
        self,
        max_concurrent_inits: int = 2,
        default_timeout: float = 30.0,
    ):
        """
        Initialize domain state manager.

        Args:
            max_concurrent_inits: Maximum concurrent domain initializations
            default_timeout: Default timeout for waiting on initialization
        """
        self._domains: dict[str, DomainState] = {}
        self._lock = threading.Lock()
        self._init_executor: ThreadPoolExecutor | None = None
        self._max_concurrent_inits = max_concurrent_inits
        self.default_timeout = default_timeout
        self._shutdown = False

    def _get_executor(self) -> ThreadPoolExecutor:
        """Lazily create thread pool executor."""
        if self._init_executor is None:
            self._init_executor = ThreadPoolExecutor(
                max_workers=self._max_concurrent_inits,
                thread_name_prefix="typesense-init-",
            )
        return self._init_executor

    def get_or_create(self, domain_name: str) -> tuple[DomainState, bool]:
        """
        Get existing domain state or create new one atomically.

        Args:
            domain_name: Domain identifier

        Returns:
            Tuple of (DomainState, is_new) where is_new indicates if we created it
        """
        with self._lock:
            existing = self._domains.get(domain_name)
            if existing is not None:
                return existing, False

            # Create new state
            state = DomainState(domain_name=domain_name)
            self._domains[domain_name] = state
            logger.debug(f"Created new DomainState for '{domain_name}'")
            return state, True

    def get(self, domain_name: str) -> DomainState | None:
        """Get domain state if exists."""
        with self._lock:
            return self._domains.get(domain_name)

    def submit_init(
        self,
        domain_name: str,
        init_func: Callable[..., int],
        *args: Any,
        **kwargs: Any,
    ) -> DomainState | None:
        """
        Submit domain initialization to background thread pool.

        Args:
            domain_name: Domain to initialize
            init_func: Function to call for initialization, should return document count
            *args, **kwargs: Arguments for init_func

        Returns:
            DomainState for tracking, or None if shutdown
        """
        if self._shutdown:
            logger.warning(f"Cannot submit init for '{domain_name}' - manager is shutdown")
            return None

        state, is_new = self.get_or_create(domain_name)

        if not is_new:
            # Someone else already created/started this domain
            return state

        if not state.claim_initialization():
            # Another thread beat us to it
            return state

        def _do_init() -> None:
            try:
                result = init_func(*args, **kwargs)
                if result is not None:
                    state.set_ready(document_count=result)
                else:
                    state.set_failed("Initialization returned None")
            except Exception as e:
                state.set_failed(str(e))
                logger.error(f"Background initialization failed for domain '{domain_name}': {e}")

        self._get_executor().submit(_do_init)
        return state

    def shutdown(self, wait: bool = True) -> None:
        """Shutdown the initialization thread pool."""
        self._shutdown = True
        if self._init_executor is not None:
            self._init_executor.shutdown(wait=wait)
            logger.debug("DomainStateManager thread pool shut down")

    def clear(self) -> None:
        """Clear all domain states (mainly for testing)."""
        with self._lock:
            self._domains.clear()
            logger.debug("DomainStateManager cleared")
