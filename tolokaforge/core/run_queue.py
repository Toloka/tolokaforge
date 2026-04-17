"""Durable run queue backends (SQLite and optional Postgres)."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class AttemptLease:
    """Leased queue item for execution."""

    id: int
    task_id: str
    trial_index: int
    retry_count: int


class SqliteRunQueue:
    """SQLite-backed durable attempt queue.

    The queue tracks a single logical attempt per (task_id, trial_index) and
    increments `retry_count` when an attempt is requeued.
    """

    def __init__(self, db_path: Path, max_retries: int = 0):
        self.db_path = Path(db_path)
        self.max_retries = max(0, int(max_retries))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS attempts (
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
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS attempt_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    attempt_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(attempt_id) REFERENCES attempts(id)
                )
                """
            )

    def enqueue(self, task_id: str, trial_index: int) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO attempts (
                    task_id, trial_index, status, retry_count, max_retries, created_at, updated_at
                ) VALUES (?, ?, 'pending', 0, ?, ?, ?)
                """,
                (task_id, trial_index, self.max_retries, now, now),
            )

    def enqueue_many(self, items: list[tuple[str, int]]) -> None:
        if not items:
            return
        now = time.time()
        rows = [
            (task_id, trial_index, self.max_retries, now, now) for task_id, trial_index in items
        ]
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO attempts (
                    task_id, trial_index, status, retry_count, max_retries, created_at, updated_at
                ) VALUES (?, ?, 'pending', 0, ?, ?, ?)
                """,
                rows,
            )

    def recover_inflight(self, max_lease_age_s: int = 3600) -> int:
        """Requeue stale leased/running attempts.

        Returns number of rows moved back to pending.
        """
        cutoff = time.time() - max(1, max_lease_age_s)
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE attempts
                SET status='pending',
                    lease_owner=NULL,
                    lease_expires_at=NULL,
                    updated_at=?
                WHERE status IN ('leased', 'running')
                  AND (
                      lease_expires_at IS NULL OR lease_expires_at < ? OR updated_at < ?
                  )
                """,
                (time.time(), time.time(), cutoff),
            )
            return int(cur.rowcount or 0)

    def lease_next(self, worker_id: str, lease_seconds: int) -> AttemptLease | None:
        now = time.time()
        expires = now + max(30, lease_seconds)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT id, task_id, trial_index, retry_count
                FROM attempts
                WHERE status='pending'
                ORDER BY updated_at ASC, id ASC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None

            conn.execute(
                """
                UPDATE attempts
                SET status='leased',
                    lease_owner=?,
                    lease_expires_at=?,
                    updated_at=?
                WHERE id=?
                """,
                (worker_id, expires, now, row["id"]),
            )
            conn.execute("COMMIT")
            return AttemptLease(
                id=int(row["id"]),
                task_id=str(row["task_id"]),
                trial_index=int(row["trial_index"]),
                retry_count=int(row["retry_count"]),
            )

    def mark_running(self, attempt_id: int, worker_id: str) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE attempts
                SET status='running',
                    lease_owner=?,
                    started_at=COALESCE(started_at, ?),
                    updated_at=?
                WHERE id=?
                """,
                (worker_id, now, now, attempt_id),
            )

    def mark_completed(self, attempt_id: int, cost_usd: float) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE attempts
                SET status='completed',
                    ended_at=?,
                    lease_owner=NULL,
                    lease_expires_at=NULL,
                    last_cost_usd=?,
                    updated_at=?
                WHERE id=?
                """,
                (now, max(0.0, float(cost_usd)), now, attempt_id),
            )

    def mark_failed(self, attempt_id: int, error: str, retryable: bool) -> bool:
        """Mark failed. Returns True if requeued for retry."""
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT retry_count, max_retries FROM attempts WHERE id=?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                return False

            retry_count = int(row["retry_count"])
            max_retries = int(row["max_retries"])
            should_retry = bool(retryable and retry_count < max_retries)
            if should_retry:
                conn.execute(
                    """
                    UPDATE attempts
                    SET status='pending',
                        retry_count=retry_count+1,
                        lease_owner=NULL,
                        lease_expires_at=NULL,
                        last_error=?,
                        updated_at=?
                    WHERE id=?
                    """,
                    (error, now, attempt_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE attempts
                    SET status='failed',
                        ended_at=?,
                        lease_owner=NULL,
                        lease_expires_at=NULL,
                        last_error=?,
                        updated_at=?
                    WHERE id=?
                    """,
                    (now, error, now, attempt_id),
                )
            return should_retry

    def get_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM attempts GROUP BY status"
            ).fetchall()
            counts = {
                "pending": 0,
                "leased": 0,
                "running": 0,
                "completed": 0,
                "failed": 0,
                "cancelled": 0,
            }
            for row in rows:
                counts[str(row["status"])] = int(row["n"])
            counts["total"] = sum(v for k, v in counts.items() if k != "total")
            return counts

    def estimate_eta_seconds(self) -> float | None:
        """Estimate ETA from completed attempts; returns None when insufficient data."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS completed_n,
                    MIN(started_at) AS min_started,
                    MAX(ended_at) AS max_ended
                FROM attempts
                WHERE status='completed' AND started_at IS NOT NULL AND ended_at IS NOT NULL
                """
            ).fetchone()
            if row is None:
                return None
            completed_n = int(row["completed_n"] or 0)
            if completed_n < 3:
                return None
            min_started = float(row["min_started"] or 0.0)
            max_ended = float(row["max_ended"] or 0.0)
            elapsed = max_ended - min_started
            if elapsed <= 0:
                return None
            throughput = completed_n / elapsed
            if throughput <= 0:
                return None
            counts = self.get_counts()
            remaining = (
                counts.get("pending", 0) + counts.get("leased", 0) + counts.get("running", 0)
            )
            return remaining / throughput

    def clear_all(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM attempt_events")
            conn.execute("DELETE FROM attempts")


class RunQueue(Protocol):
    """Queue protocol used by orchestrator."""

    def enqueue(self, task_id: str, trial_index: int) -> None: ...
    def enqueue_many(self, items: list[tuple[str, int]]) -> None: ...
    def recover_inflight(self, max_lease_age_s: int = 3600) -> int: ...
    def lease_next(self, worker_id: str, lease_seconds: int) -> AttemptLease | None: ...
    def mark_running(self, attempt_id: int, worker_id: str) -> None: ...
    def mark_completed(self, attempt_id: int, cost_usd: float) -> None: ...
    def mark_failed(self, attempt_id: int, error: str, retryable: bool) -> bool: ...
    def get_counts(self) -> dict[str, int]: ...
    def estimate_eta_seconds(self) -> float | None: ...
    def clear_all(self) -> None: ...


class PostgresRunQueue:
    """Postgres-backed durable queue for distributed workers.

    Requires `psycopg` (v3). The API mirrors `SqliteRunQueue`.
    """

    def __init__(self, dsn: str, max_retries: int = 0):
        if not dsn:
            raise ValueError("Postgres DSN must be provided for postgres queue backend")
        self.dsn = dsn
        self.max_retries = max(0, int(max_retries))
        self._init_db()

    def _connect(self):
        try:
            import psycopg
        except Exception as e:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "psycopg is required for postgres queue backend. Install with `uv pip install psycopg[binary]`."
            ) from e
        return psycopg.connect(self.dsn, autocommit=True)

    def _init_db(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS attempts (
                        id BIGSERIAL PRIMARY KEY,
                        task_id TEXT NOT NULL,
                        trial_index INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        retry_count INTEGER NOT NULL DEFAULT 0,
                        max_retries INTEGER NOT NULL DEFAULT 0,
                        lease_owner TEXT,
                        lease_expires_at DOUBLE PRECISION,
                        started_at DOUBLE PRECISION,
                        ended_at DOUBLE PRECISION,
                        last_error TEXT,
                        last_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                        created_at DOUBLE PRECISION NOT NULL,
                        updated_at DOUBLE PRECISION NOT NULL,
                        UNIQUE(task_id, trial_index)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS attempt_events (
                        id BIGSERIAL PRIMARY KEY,
                        attempt_id BIGINT NOT NULL REFERENCES attempts(id),
                        event_type TEXT NOT NULL,
                        payload TEXT,
                        created_at DOUBLE PRECISION NOT NULL
                    )
                    """
                )

    def enqueue(self, task_id: str, trial_index: int) -> None:
        now = time.time()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO attempts (
                        task_id, trial_index, status, retry_count, max_retries, created_at, updated_at
                    ) VALUES (%s, %s, 'pending', 0, %s, %s, %s)
                    ON CONFLICT (task_id, trial_index) DO NOTHING
                    """,
                    (task_id, trial_index, self.max_retries, now, now),
                )

    def enqueue_many(self, items: list[tuple[str, int]]) -> None:
        if not items:
            return
        now = time.time()
        chunk_size = 500
        with self._connect() as conn:
            with conn.cursor() as cur:
                for idx in range(0, len(items), chunk_size):
                    chunk = items[idx : idx + chunk_size]
                    values_sql = ", ".join(["(%s, %s, 'pending', 0, %s, %s, %s)"] * len(chunk))
                    params: list[object] = []
                    for task_id, trial_index in chunk:
                        params.extend([task_id, trial_index, self.max_retries, now, now])
                    cur.execute(
                        f"""
                        INSERT INTO attempts (
                            task_id, trial_index, status, retry_count, max_retries, created_at, updated_at
                        ) VALUES {values_sql}
                        ON CONFLICT (task_id, trial_index) DO NOTHING
                        """,
                        params,
                    )

    def recover_inflight(self, max_lease_age_s: int = 3600) -> int:
        cutoff = time.time() - max(1, max_lease_age_s)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE attempts
                    SET status='pending',
                        lease_owner=NULL,
                        lease_expires_at=NULL,
                        updated_at=%s
                    WHERE status IN ('leased', 'running')
                      AND (lease_expires_at IS NULL OR lease_expires_at < %s OR updated_at < %s)
                    """,
                    (time.time(), time.time(), cutoff),
                )
                return int(cur.rowcount or 0)

    def lease_next(self, worker_id: str, lease_seconds: int) -> AttemptLease | None:
        now = time.time()
        expires = now + max(30, lease_seconds)
        with self._connect() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        WITH cte AS (
                            SELECT id
                            FROM attempts
                            WHERE status='pending'
                            ORDER BY updated_at ASC, id ASC
                            LIMIT 1
                            FOR UPDATE SKIP LOCKED
                        )
                        UPDATE attempts a
                        SET status='leased',
                            lease_owner=%s,
                            lease_expires_at=%s,
                            updated_at=%s
                        FROM cte
                        WHERE a.id=cte.id
                        RETURNING a.id, a.task_id, a.trial_index, a.retry_count
                        """,
                        (worker_id, expires, now),
                    )
                    row = cur.fetchone()
                    if row is None:
                        return None
                    return AttemptLease(
                        id=int(row[0]),
                        task_id=str(row[1]),
                        trial_index=int(row[2]),
                        retry_count=int(row[3]),
                    )

    def mark_running(self, attempt_id: int, worker_id: str) -> None:
        now = time.time()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE attempts
                    SET status='running',
                        lease_owner=%s,
                        started_at=COALESCE(started_at, %s),
                        updated_at=%s
                    WHERE id=%s
                    """,
                    (worker_id, now, now, attempt_id),
                )

    def mark_completed(self, attempt_id: int, cost_usd: float) -> None:
        now = time.time()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE attempts
                    SET status='completed',
                        ended_at=%s,
                        lease_owner=NULL,
                        lease_expires_at=NULL,
                        last_cost_usd=%s,
                        updated_at=%s
                    WHERE id=%s
                    """,
                    (now, max(0.0, float(cost_usd)), now, attempt_id),
                )

    def mark_failed(self, attempt_id: int, error: str, retryable: bool) -> bool:
        now = time.time()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT retry_count, max_retries FROM attempts WHERE id=%s",
                    (attempt_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return False
                retry_count = int(row[0])
                max_retries = int(row[1])
                should_retry = bool(retryable and retry_count < max_retries)
                if should_retry:
                    cur.execute(
                        """
                        UPDATE attempts
                        SET status='pending',
                            retry_count=retry_count+1,
                            lease_owner=NULL,
                            lease_expires_at=NULL,
                            last_error=%s,
                            updated_at=%s
                        WHERE id=%s
                        """,
                        (error, now, attempt_id),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE attempts
                        SET status='failed',
                            ended_at=%s,
                            lease_owner=NULL,
                            lease_expires_at=NULL,
                            last_error=%s,
                            updated_at=%s
                        WHERE id=%s
                        """,
                        (now, error, now, attempt_id),
                    )
                return should_retry

    def get_counts(self) -> dict[str, int]:
        counts = {
            "pending": 0,
            "leased": 0,
            "running": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
        }
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status, COUNT(*) AS n FROM attempts GROUP BY status")
                rows = cur.fetchall()
                for status, n in rows:
                    counts[str(status)] = int(n)
        counts["total"] = sum(v for k, v in counts.items() if k != "total")
        return counts

    def estimate_eta_seconds(self) -> float | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        COUNT(*) AS completed_n,
                        MIN(started_at) AS min_started,
                        MAX(ended_at) AS max_ended
                    FROM attempts
                    WHERE status='completed' AND started_at IS NOT NULL AND ended_at IS NOT NULL
                    """
                )
                row = cur.fetchone()
                if row is None:
                    return None
                completed_n = int(row[0] or 0)
                if completed_n < 3:
                    return None
                min_started = float(row[1] or 0.0)
                max_ended = float(row[2] or 0.0)
                elapsed = max_ended - min_started
                if elapsed <= 0:
                    return None
                throughput = completed_n / elapsed
                if throughput <= 0:
                    return None
                counts = self.get_counts()
                remaining = (
                    counts.get("pending", 0) + counts.get("leased", 0) + counts.get("running", 0)
                )
                return remaining / throughput

    def clear_all(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM attempt_events")
                cur.execute("DELETE FROM attempts")


def create_run_queue(
    backend: str,
    *,
    sqlite_path: Path,
    max_retries: int,
    postgres_dsn: str | None = None,
) -> RunQueue:
    """Factory for queue backend selection."""
    normalized = (backend or "sqlite").lower()
    if normalized == "sqlite":
        return SqliteRunQueue(sqlite_path, max_retries=max_retries)
    if normalized == "postgres":
        return PostgresRunQueue(postgres_dsn or "", max_retries=max_retries)
    raise ValueError(f"Unsupported queue backend: {backend}")
