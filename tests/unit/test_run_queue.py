"""Unit tests for durable SQLite run queue."""

from __future__ import annotations

from pathlib import Path

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
