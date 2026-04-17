"""Unit tests for `tolokaforge status` command paths."""

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from tolokaforge.cli.main import cli
from tolokaforge.core.run_queue import create_run_queue

pytestmark = pytest.mark.unit


class _FakeQueue:
    """Minimal queue stub for CLI status checks."""

    def get_counts(self) -> dict[str, int]:
        return {
            "pending": 3,
            "leased": 1,
            "running": 1,
            "completed": 2,
            "failed": 0,
            "total": 6,
        }

    def estimate_eta_seconds(self) -> float:
        return 42.0


def _write_run_config(
    path: Path, queue_backend: str, queue_postgres_dsn: str | None = None
) -> None:
    config = {
        "models": {"main": {"provider": "openai", "name": "gpt-4o-mini"}},
        "orchestrator": {
            "workers": 1,
            "repeats": 1,
            "queue_backend": queue_backend,
            "queue_postgres_dsn": queue_postgres_dsn,
        },
        "evaluation": {"output_dir": "results/test_status"},
    }
    path.write_text(yaml.safe_dump(config), encoding="utf-8")


def test_status_reads_sqlite_queue_when_present(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    queue_path = run_dir / "run_queue.sqlite"
    create_run_queue("sqlite", sqlite_path=queue_path, max_retries=0)

    runner = CliRunner()
    result = runner.invoke(cli, ["status", "--run-dir", str(run_dir)])

    assert result.exit_code == 0
    assert "Run: (no run_state.json)" in result.output
    assert "Queue:" in result.output
    assert "total=0" in result.output


def test_status_reads_postgres_queue_from_config_when_sqlite_missing(
    tmp_path: Path, monkeypatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "run.yaml"
    _write_run_config(
        config_path,
        queue_backend="postgres",
        queue_postgres_dsn="postgresql://user:pass@localhost:5432/tolokaforge",
    )

    called: dict[str, str] = {}

    def _fake_create_run_queue(
        backend: str, sqlite_path: Path, max_retries: int, postgres_dsn: str | None = None
    ):
        called["backend"] = backend
        called["sqlite_path"] = str(sqlite_path)
        called["postgres_dsn"] = str(postgres_dsn)
        return _FakeQueue()

    monkeypatch.setattr("tolokaforge.cli.main.create_run_queue", _fake_create_run_queue)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["status", "--run-dir", str(run_dir), "--config", str(config_path)],
    )

    assert result.exit_code == 0
    assert called["backend"] == "postgres"
    assert called["postgres_dsn"].startswith("postgresql://")
    assert "Queue:" in result.output
    assert "pending=3" in result.output
    assert "total=6" in result.output
