"""Integration tests for Postgres-backed run queue."""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.core.run_queue import PostgresRunQueue, create_run_queue

pytestmark = pytest.mark.integration


def _normalize_dsn(dsn: str) -> str:
    if dsn.startswith("postgresql+psycopg2://"):
        return dsn.replace("postgresql+psycopg2://", "postgresql://", 1)
    return dsn


@pytest.fixture(scope="module")
def postgres_dsn() -> str:
    pytest.importorskip("psycopg")
    postgres_mod = pytest.importorskip("testcontainers.postgres")
    PostgresContainer = postgres_mod.PostgresContainer

    try:
        with PostgresContainer("postgres:16-alpine") as postgres:
            yield _normalize_dsn(postgres.get_connection_url())
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres test container unavailable: {exc}")


@pytest.mark.integration
@pytest.mark.requires_docker
@pytest.mark.requires_postgres
class TestPostgresRunQueue:
    def test_enqueue_lease_complete_counts(self, postgres_dsn: str):
        queue = PostgresRunQueue(postgres_dsn, max_retries=0)
        queue.clear_all()
        queue.enqueue_many([("task_a", 0), ("task_b", 0), ("task_a", 0)])

        lease = queue.lease_next(worker_id="worker-1", lease_seconds=300)
        assert lease is not None
        queue.mark_running(lease.id, "worker-1")
        queue.mark_completed(lease.id, cost_usd=0.12)

        counts = queue.get_counts()
        assert counts["completed"] == 1
        assert counts["pending"] == 1
        assert counts["total"] == 2

    def test_retryable_failure_requeues_then_fails(self, postgres_dsn: str):
        queue = create_run_queue(
            "postgres",
            sqlite_path=Path("/tmp/unused-run-queue.sqlite"),  # unused for postgres backend
            max_retries=1,
            postgres_dsn=postgres_dsn,
        )
        queue.clear_all()
        queue.enqueue("task_retry", 0)

        lease_1 = queue.lease_next(worker_id="worker-2", lease_seconds=300)
        assert lease_1 is not None
        queue.mark_running(lease_1.id, "worker-2")
        will_retry = queue.mark_failed(lease_1.id, error="transient", retryable=True)
        assert will_retry is True

        lease_2 = queue.lease_next(worker_id="worker-2", lease_seconds=300)
        assert lease_2 is not None
        assert lease_2.id == lease_1.id
        assert lease_2.retry_count == 1
        queue.mark_running(lease_2.id, "worker-2")
        will_retry_again = queue.mark_failed(lease_2.id, error="transient_again", retryable=True)
        assert will_retry_again is False

        counts = queue.get_counts()
        assert counts["failed"] == 1
        assert counts["pending"] == 0
