"""Unit tests for DomainState and DomainStateManager.

Tests the thread-safe domain initialization coordination mechanism
used by TypeSense caching.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from tolokaforge.core.search.domain_state import (
    DomainState,
    DomainStateManager,
    DomainStatus,
)

pytestmark = pytest.mark.unit


class TestDomainState:
    """Tests for DomainState dataclass."""

    def test_initial_state(self):
        """Test default state is PENDING."""
        state = DomainState(domain_name="test")
        assert state.domain_name == "test"
        assert state.status == DomainStatus.PENDING
        assert state.error is None
        assert state.document_count == 0

    def test_claim_initialization_success(self):
        """Test claiming initialization from PENDING state."""
        state = DomainState(domain_name="test")

        claimed = state.claim_initialization()

        assert claimed is True
        assert state.status == DomainStatus.INITIALIZING

    def test_claim_initialization_atomic(self):
        """Test that claim_initialization is atomic under concurrent access."""
        state = DomainState(domain_name="test")
        results = []

        def claim():
            claimed = state.claim_initialization()
            results.append(claimed)

        threads = [threading.Thread(target=claim) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly one thread should have claimed
        assert sum(results) == 1
        assert state.status == DomainStatus.INITIALIZING

    def test_set_ready(self):
        """Test marking domain as ready."""
        state = DomainState(domain_name="test")
        state.claim_initialization()

        state.set_ready(document_count=10)

        assert state.status == DomainStatus.READY
        assert state.document_count == 10
        assert state.error is None

    def test_set_failed(self):
        """Test marking domain as failed."""
        state = DomainState(domain_name="test")
        state.claim_initialization()

        state.set_failed("Connection error")

        assert state.status == DomainStatus.FAILED
        assert state.error == "Connection error"

    def test_wait_ready_immediate_ready(self):
        """Test wait_ready returns immediately when already READY."""
        state = DomainState(domain_name="test")
        state.set_ready(document_count=5)

        start = time.time()
        result = state.wait_ready(timeout=10.0)
        elapsed = time.time() - start

        assert result is True
        assert elapsed < 0.1

    def test_wait_ready_timeout(self):
        """Test wait_ready raises TimeoutError when stuck in PENDING."""
        state = DomainState(domain_name="test")

        with pytest.raises(TimeoutError, match="timed out"):
            state.wait_ready(timeout=0.1)

    def test_wait_ready_waits_for_ready(self):
        """Test wait_ready blocks until set_ready is called."""
        state = DomainState(domain_name="test")
        state.claim_initialization()
        result = [None]

        def waiter():
            result[0] = state.wait_ready(timeout=5.0)

        def setter():
            time.sleep(0.1)
            state.set_ready(document_count=3)

        t_waiter = threading.Thread(target=waiter)
        t_setter = threading.Thread(target=setter)

        t_waiter.start()
        t_setter.start()
        t_waiter.join()
        t_setter.join()

        assert result[0] is True
        assert state.status == DomainStatus.READY


class TestDomainStateManager:
    """Tests for DomainStateManager."""

    def test_get_or_create_new(self):
        """Test creating new domain state."""
        manager = DomainStateManager()

        state, is_new = manager.get_or_create("test_domain")

        assert is_new is True
        assert state.domain_name == "test_domain"
        assert state.status == DomainStatus.PENDING

    def test_get_or_create_existing(self):
        """Test getting existing domain state."""
        manager = DomainStateManager()
        state1, is_new1 = manager.get_or_create("test_domain")

        state2, is_new2 = manager.get_or_create("test_domain")

        assert is_new1 is True
        assert is_new2 is False
        assert state1 is state2

    def test_concurrent_get_or_create(self):
        """Test concurrent calls only create one state."""
        manager = DomainStateManager()
        results = []

        def get_domain():
            state, is_new = manager.get_or_create("test_domain")
            results.append((state, is_new))

        threads = [threading.Thread(target=get_domain) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        new_count = sum(1 for _, is_new in results if is_new)
        assert new_count == 1
        states = [s for s, _ in results]
        assert all(s is states[0] for s in states)

    def test_submit_init_concurrent(self):
        """Test submit_init handles concurrent calls correctly."""
        manager = DomainStateManager(max_concurrent_inits=2)
        init_count = [0]
        lock = threading.Lock()

        def init_func():
            with lock:
                init_count[0] += 1
            time.sleep(0.1)
            return 5

        states = []
        for _ in range(5):
            state = manager.submit_init("test_domain", init_func)
            if state:
                states.append(state)

        time.sleep(0.3)

        assert init_count[0] == 1
        assert all(s is states[0] for s in states)

        manager.shutdown()

    def test_shutdown(self):
        """Test shutdown stops the thread pool."""
        manager = DomainStateManager()
        manager.shutdown()

        state = manager.submit_init("test", lambda: 1)
        assert state is None


class TestTypeSenseIntegration:
    """Integration tests simulating TypeSense initialization pattern."""

    def test_coordinated_initialization_pattern(self):
        """Test the full initialization coordination pattern used by TypeSense provider."""
        manager = DomainStateManager(default_timeout=5.0)
        init_count = [0]
        lock = threading.Lock()

        def do_init(state: DomainState):
            with lock:
                init_count[0] += 1
            time.sleep(0.05)
            state.set_ready(document_count=10)

        def ensure_initialized():
            state, _ = manager.get_or_create("test_domain")

            if state.claim_initialization():
                do_init(state)

            state.wait_ready(timeout=5.0)
            return state.status == DomainStatus.READY

        results = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(ensure_initialized) for _ in range(10)]
            results = [f.result() for f in futures]

        assert all(results)
        assert init_count[0] == 1

        state = manager.get("test_domain")
        assert state is not None
        assert state.status == DomainStatus.READY
        assert state.document_count == 10

    def test_initialization_failure_propagation(self):
        """Test that initialization failures propagate to all waiters."""
        manager = DomainStateManager(default_timeout=5.0)

        def do_failing_init(state: DomainState):
            time.sleep(0.05)
            state.set_failed("Connection refused")

        def ensure_initialized():
            state, _ = manager.get_or_create("failing_domain")

            if state.claim_initialization():
                do_failing_init(state)

            try:
                state.wait_ready(timeout=5.0)
                return True
            except RuntimeError:
                return False

        results = []
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(ensure_initialized) for _ in range(5)]
            results = [f.result() for f in futures]

        assert all(r is False for r in results)

        state = manager.get("failing_domain")
        assert state is not None
        assert state.status == DomainStatus.FAILED
        assert "Connection refused" in state.error
