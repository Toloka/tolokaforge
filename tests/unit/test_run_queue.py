"""Unit tests for durable SQLite run queue."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tolokaforge.core.run_queue import (
    QUEUE_SCHEMA_VERSION,
    QueueSchemaVersionError,
    SqliteRunQueue,
    create_run_queue,
)

pytestmark = pytest.mark.unit


def test_enqueue_lease_complete_counts(tmp_path: Path):
    queue = SqliteRunQueue(tmp_path / "run_queue.sqlite", max_retries=0)
    queue.enqueue_many([("", "task_a", 0), ("", "task_b", 0)])

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
    queue.enqueue("", "task_a", 0)

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
    queue.enqueue_many([("", "task_a", 0), ("", "task_b", 0)])
    assert queue.get_counts()["total"] == 2
    queue.clear_all()
    assert queue.get_counts()["total"] == 0


def test_same_task_under_two_entries_is_two_leasable_rows(tmp_path: Path):
    """The entry is part of the attempt identity, so the same task_id enqueued
    under two different entries produces two distinct, independently leasable
    rows rather than colliding on the UNIQUE constraint."""
    queue = SqliteRunQueue(tmp_path / "run_queue.sqlite", max_retries=0)
    queue.enqueue_many([("harness_a", "shared_task", 0), ("harness_b", "shared_task", 0)])

    assert queue.get_counts()["pending"] == 2

    lease_1 = queue.lease_next(worker_id="worker-1", lease_seconds=300)
    lease_2 = queue.lease_next(worker_id="worker-1", lease_seconds=300)
    assert lease_1 is not None and lease_2 is not None
    assert lease_1.id != lease_2.id
    assert lease_1.task_id == lease_2.task_id == "shared_task"
    assert {lease_1.entry, lease_2.entry} == {"harness_a", "harness_b"}

    assert queue.lease_next(worker_id="worker-1", lease_seconds=300) is None


def test_empty_entry_row_behaves_as_single_adapter(tmp_path: Path):
    """An ``entry=""`` row reproduces single-adapter behaviour: it leases with
    the empty-string entry and collides on re-enqueue of the same triple."""
    queue = SqliteRunQueue(tmp_path / "run_queue.sqlite", max_retries=0)
    queue.enqueue("", "task_a", 0)
    queue.enqueue("", "task_a", 0)  # idempotent on (entry, task_id, trial_index)

    assert queue.get_counts()["pending"] == 1

    lease = queue.lease_next(worker_id="worker-1", lease_seconds=300)
    assert lease is not None
    assert lease.entry == ""
    assert lease.task_id == "task_a"
    assert lease.trial_index == 0


def test_enqueue_idempotent_on_triple_conflict_key(tmp_path: Path):
    """Re-enqueuing the same (entry, task_id, trial_index) is a no-op, but a
    differing entry on the same task_id/trial_index is a new row."""
    queue = SqliteRunQueue(tmp_path / "run_queue.sqlite", max_retries=0)
    queue.enqueue_many(
        [
            ("harness_a", "task_a", 0),
            ("harness_a", "task_a", 0),  # duplicate triple — ignored
            ("harness_b", "task_a", 0),  # same task, different entry — kept
        ]
    )
    queue.enqueue("harness_a", "task_a", 0)  # duplicate triple — ignored

    assert queue.get_counts()["total"] == 2


def _write_legacy_queue_db(db_path: Path) -> None:
    """Materialize a pre-change queue DB: the old 2-column UNIQUE and the
    absent (0) user_version an earlier build left behind."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                trial_index INTEGER NOT NULL,
                status TEXT NOT NULL,
                retry_count INTEGER NOT NULL DEFAULT 0,
                max_retries INTEGER NOT NULL DEFAULT 0,
                lease_owner TEXT,
                lease_expires_at REAL,
                started_at REAL,
                ended_at REAL,
                last_error TEXT,
                last_cost_usd REAL NOT NULL DEFAULT 0.0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(task_id, trial_index)
            )
            """)
        conn.commit()
    finally:
        conn.close()


def test_version_guard_refuses_legacy_queue_db(tmp_path: Path):
    """Opening a pre-change queue DB (old schema, absent user_version) with the
    composite-key backend fails loud rather than silently resuming it."""
    db_path = tmp_path / "run_queue.sqlite"
    _write_legacy_queue_db(db_path)

    with pytest.raises(QueueSchemaVersionError) as exc_info:
        SqliteRunQueue(db_path, max_retries=0)

    message = str(exc_info.value)
    assert str(db_path) in message
    assert "fresh run dir" in message


def test_version_guard_stamps_fresh_db_and_allows_reopen(tmp_path: Path):
    """A fresh DB is stamped with the current version and reopens cleanly."""
    db_path = tmp_path / "run_queue.sqlite"
    SqliteRunQueue(db_path, max_retries=0)

    conn = sqlite3.connect(db_path)
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()
    assert version == QUEUE_SCHEMA_VERSION

    # Reopen must not raise — the stamp matches.
    reopened = SqliteRunQueue(db_path, max_retries=0)
    assert reopened.get_counts()["total"] == 0
