"""Unit tests for durable SQLite run queue."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from tolokaforge.core.run_queue import SqliteRunQueue, create_run_queue

pytestmark = pytest.mark.unit


def test_enqueue_lease_complete_counts(tmp_path: Path):
    queue = SqliteRunQueue(tmp_path / "run_queue.sqlite", max_retries=0)
    queue.enqueue_many([("task_a", 0), ("task_b", 0)])

    lease = queue.lease_next(worker_id="worker-1", lease_seconds=300)
    assert lease is not None
    queue.mark_running(lease.id, "worker-1")
    queue.mark_completed(lease.id, cost_usd=0.15)

    counts = queue.get_counts()
    assert counts["completed"] == 1
    assert counts["pending"] == 1
    assert counts["total"] == 2


def test_retryable_failure_requeues_then_fails(tmp_path: Path):
    queue = SqliteRunQueue(tmp_path / "run_queue.sqlite", max_retries=1)
    queue.enqueue("task_a", 0)

    lease_1 = queue.lease_next(worker_id="worker-1", lease_seconds=300)
    assert lease_1 is not None
    queue.mark_running(lease_1.id, "worker-1")
    will_retry = queue.mark_failed(lease_1.id, error="transient 429", retryable=True)
    assert will_retry is True

    lease_2 = queue.lease_next(worker_id="worker-1", lease_seconds=300)
    assert lease_2 is not None
    assert lease_2.id == lease_1.id
    assert lease_2.retry_count == 1
    queue.mark_running(lease_2.id, "worker-1")
    will_retry_again = queue.mark_failed(lease_2.id, error="transient again", retryable=True)
    assert will_retry_again is False

    counts = queue.get_counts()
    assert counts["failed"] == 1
    assert counts["pending"] == 0


def test_factory_sqlite_backend(tmp_path: Path):
    queue = create_run_queue(
        "sqlite",
        sqlite_path=tmp_path / "queue.sqlite",
        max_retries=0,
    )
    assert isinstance(queue, SqliteRunQueue)


def test_factory_unsupported_backend(tmp_path: Path):
    with pytest.raises(ValueError):
        create_run_queue(
            "unknown",
            sqlite_path=tmp_path / "queue.sqlite",
            max_retries=0,
        )


def test_factory_postgres_requires_dsn(tmp_path: Path):
    with pytest.raises(ValueError):
        create_run_queue(
            "postgres",
            sqlite_path=tmp_path / "queue.sqlite",
            max_retries=0,
            postgres_dsn=None,
        )


def test_clear_all_resets_queue(tmp_path: Path):
    queue = SqliteRunQueue(tmp_path / "run_queue.sqlite", max_retries=0)
    queue.enqueue_many([("task_a", 0), ("task_b", 0)])
    assert queue.get_counts()["total"] == 2
    queue.clear_all()
    assert queue.get_counts()["total"] == 0


# ── Stage 14: Resilience tests ──────────────────────────────────────


def test_thread_local_connection_reuse(tmp_path: Path):
    """Same thread should get the same cached connection object."""
    queue = SqliteRunQueue(tmp_path / "run_queue.sqlite", max_retries=0)
    conn1 = queue._connect()
    conn2 = queue._connect()
    assert conn1 is conn2


def test_thread_local_isolation(tmp_path: Path):
    """Different threads should get different connection objects."""
    queue = SqliteRunQueue(tmp_path / "run_queue.sqlite", max_retries=0)
    conn_main = queue._connect()
    conn_other = [None]

    def worker():
        conn_other[0] = queue._connect()

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert conn_other[0] is not None
    assert conn_other[0] is not conn_main


def test_stale_connection_replaced(tmp_path: Path):
    """A broken cached connection should be replaced with a fresh one."""
    queue = SqliteRunQueue(tmp_path / "run_queue.sqlite", max_retries=0)
    conn1 = queue._connect()
    # Simulate a broken connection by closing it
    conn1.close()
    conn2 = queue._connect()
    assert conn2 is not conn1
    # New connection should work
    conn2.execute("SELECT 1")


def test_connect_retries_on_transient_error(tmp_path: Path):
    """_new_connection should retry on transient sqlite3.DatabaseError."""
    queue = SqliteRunQueue(tmp_path / "run_queue.sqlite", max_retries=0)
    call_count = [0]
    original_connect = sqlite3.connect

    def flaky_connect(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] <= 2:
            raise sqlite3.DatabaseError("file is not a database")
        return original_connect(*args, **kwargs)

    with patch("tolokaforge.core.run_queue.sqlite3.connect", side_effect=flaky_connect):
        # Clear cached connection so _new_connection is called
        queue._local.conn = None
        conn = queue._connect()
        assert conn is not None
        assert call_count[0] == 3  # 2 failures + 1 success


def test_connect_raises_after_max_retries(tmp_path: Path):
    """_new_connection should raise after exhausting retries."""
    queue = SqliteRunQueue(tmp_path / "run_queue.sqlite", max_retries=0)

    def always_fail(*args, **kwargs):
        raise sqlite3.DatabaseError("file is not a database")

    with patch("tolokaforge.core.run_queue.sqlite3.connect", side_effect=always_fail):
        queue._local.conn = None
        with pytest.raises(sqlite3.DatabaseError, match="after 3 attempts"):
            queue._connect()


def test_wal_checkpoint_called_periodically(tmp_path: Path):
    """_maybe_checkpoint should fire PRAGMA wal_checkpoint after interval."""
    queue = SqliteRunQueue(tmp_path / "run_queue.sqlite", max_retries=0)
    queue._checkpoint_interval_s = 0  # Always eligible
    queue._last_checkpoint_at = 0.0
    queue.enqueue("task_a", 0)

    # mark_completed calls _maybe_checkpoint internally
    lease = queue.lease_next(worker_id="w1", lease_seconds=300)
    assert lease is not None
    queue.mark_completed(lease.id, cost_usd=0.1)

    # Verify checkpoint was called (last_checkpoint_at updated)
    assert queue._last_checkpoint_at > 0.0


def test_wal_checkpoint_not_called_too_often(tmp_path: Path):
    """_maybe_checkpoint should skip if called within interval."""
    queue = SqliteRunQueue(tmp_path / "run_queue.sqlite", max_retries=0)
    queue._checkpoint_interval_s = 9999  # Very long interval
    queue._last_checkpoint_at = 1e18  # Far in the future

    conn = queue._connect()
    queue._maybe_checkpoint(conn)
    # last_checkpoint_at should not have changed (checkpoint was skipped)
    assert queue._last_checkpoint_at == 1e18
