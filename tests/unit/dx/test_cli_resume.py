"""Unit tests for ``tolokaforge run --resume`` CLI wiring.

Locks:

* ``--resume`` requires ``--run-dir`` and vice versa; either alone is a
  ``UsageError``.
* On a fully-completed run directory the CLI prints the nothing-to-do
  line, emits the artifact path on stdout, exits 0, and never
  constructs :class:`Orchestrator`.
* On a partial run directory the CLI prints the resume summary line,
  emits the ``→ Resume: <run-id>`` banner variant, and hands the
  resolved ``(run_id, run_dir)`` to ``Orchestrator.run`` with
  ``resume=True``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

import tolokaforge.dx.cli.main as cli_main
from tolokaforge.core.engine_run_state import write_engine_run_state
from tolokaforge.core.model_data_fingerprint import compute_models_fingerprint
from tolokaforge.core.resume import RunStateManager
from tolokaforge.dx.cli.main import cli

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner(mix_stderr=False)


@pytest.fixture
def valid_config(tmp_path: Path) -> Path:
    """Minimal run config accepted by ``load_effective_run_config``."""
    config_path = tmp_path / "run.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "models": {"user": {"provider": "openai", "name": "gpt-4o"}},
                "evaluation": {
                    "output_dir": str(tmp_path / "out"),
                    "tasks_glob": str(tmp_path / "tasks" / "*"),
                },
                "orchestrator": {"repeats": 1, "auto_start_services": False},
                "compute": {"workers": 1},
            }
        )
    )
    return config_path


def _seed_run_dir(
    run_dir: Path,
    *,
    run_id: str,
    task_ids: list[str],
    repeats: int,
    completed: list[tuple[str, int]] | None = None,
) -> RunStateManager:
    """Materialise a resumable run directory with a persisted engine
    state and a ``run_state.json`` reflecting *completed*."""
    run_dir.mkdir(parents=True, exist_ok=True)
    write_engine_run_state(
        run_dir,
        run_id=run_id,
        presets_file=None,
        models_fingerprint=compute_models_fingerprint(),
    )
    manager = RunStateManager(run_dir)
    state = manager.initialize_run(
        run_id=run_id, config_path="run.yaml", task_ids=task_ids, repeats=repeats
    )
    for task_id, trial_idx in completed or []:
        state.mark_completed(task_id, trial_idx, binary_pass=True, score=1.0)
    manager.save_state(state)
    return manager


class _RecordingOrchestrator:
    """Stub that records the ``run`` kwargs so the test can assert
    ``resume=True`` + the resolver-supplied ``run_id`` / ``output_dir``."""

    captured: dict[str, Any] = {}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        type(self).captured["init_kwargs"] = kwargs
        self.tasks: list[object] = [object()]

    def load_tasks(self) -> None:
        return None

    def run(self, **kwargs: Any) -> Path:
        type(self).captured["run_kwargs"] = kwargs
        output_dir = Path(kwargs["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir


class _ExplodingOrchestrator:
    """Fails on construction — proves the no-op branch never builds an
    :class:`Orchestrator`."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError(
            "Orchestrator must not be constructed when --resume finds a complete run"
        )


class TestResumeGuardrails:
    """Flag interactions on the ``run`` command."""

    def test_resume_without_run_dir_is_usage_error(
        self, runner: CliRunner, valid_config: Path
    ) -> None:
        result = runner.invoke(cli, ["run", "--config", str(valid_config), "--resume"])

        assert result.exit_code != 0
        assert "--resume requires --run-dir" in result.stderr

    def test_run_dir_without_resume_is_usage_error(
        self, runner: CliRunner, valid_config: Path, tmp_path: Path
    ) -> None:
        existing = tmp_path / "existing"
        existing.mkdir()
        result = runner.invoke(
            cli, ["run", "--config", str(valid_config), "--run-dir", str(existing)]
        )

        assert result.exit_code != 0
        assert "--run-dir requires --resume" in result.stderr


class TestResumeIdempotentNoOp:
    """A fully-completed run directory short-circuits before any
    orchestrator construction."""

    def test_complete_run_prints_nothing_to_do_and_never_builds_orchestrator(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run_dir = tmp_path / "complete_run"
        _seed_run_dir(
            run_dir,
            run_id="complete_run_id",
            task_ids=["alpha", "beta"],
            repeats=1,
            completed=[("alpha", 0), ("beta", 0)],
        )
        monkeypatch.setattr(cli_main, "Orchestrator", _ExplodingOrchestrator)

        result = runner.invoke(
            cli,
            ["run", "--config", str(valid_config), "--resume", "--run-dir", str(run_dir)],
        )

        assert result.exit_code == 0, result.stderr
        assert "Nothing to do; run already complete (2/2 completed)" in result.stderr
        # Artifact path is emitted on stdout even on the no-op branch.
        assert result.stdout.strip() == str(run_dir.resolve())


class TestResumeSummaryAndBanner:
    """Partial run: summary line + Resume banner variant + orchestrator
    dispatched with the resolved directory."""

    def test_partial_run_prints_summary_and_resume_banner(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run_dir = tmp_path / "partial_run"
        _seed_run_dir(
            run_dir,
            run_id="partial_run_id",
            task_ids=["alpha", "beta", "gamma", "delta", "epsilon"],
            repeats=1,
            completed=[("alpha", 0), ("beta", 0), ("gamma", 0)],
        )
        _RecordingOrchestrator.captured = {}
        monkeypatch.setattr(cli_main, "Orchestrator", _RecordingOrchestrator)

        result = runner.invoke(
            cli,
            ["run", "--config", str(valid_config), "--resume", "--run-dir", str(run_dir)],
        )

        assert result.exit_code == 0, result.stderr
        assert "Resuming: 3/5 completed, 2 to retry" in result.stderr
        # Banner reads "Resume:" instead of "Run:" in the first line.
        assert "Resume: partial_run_id" in result.stderr
        assert "→ Run: partial_run_id" not in result.stderr

        captured = _RecordingOrchestrator.captured
        assert captured["init_kwargs"]["resume"] is True
        assert captured["run_kwargs"]["run_id"] == "partial_run_id"
        assert Path(captured["run_kwargs"]["output_dir"]) == run_dir

    def test_partial_run_completes_pending_only_via_stub_dispatch(
        self,
        runner: CliRunner,
        valid_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """3 completed + 2 pending → orchestrator receives the shared
        run-dir with ``resume=True`` and the resolver-supplied run-id.

        The stub does not itself execute trials — the invariant this
        test locks is that the CLI hands the correct pair to the
        orchestrator on the resume path (fresh-run's
        ``resolve_run_directory`` is bypassed)."""
        run_dir = tmp_path / "resume_dispatch"
        _seed_run_dir(
            run_dir,
            run_id="dispatch_run_id",
            task_ids=["a", "b", "c", "d", "e"],
            repeats=1,
            completed=[("a", 0), ("b", 0), ("c", 0)],
        )
        _RecordingOrchestrator.captured = {}
        monkeypatch.setattr(cli_main, "Orchestrator", _RecordingOrchestrator)

        result = runner.invoke(
            cli,
            ["run", "--config", str(valid_config), "--resume", "--run-dir", str(run_dir)],
        )

        assert result.exit_code == 0, result.stderr
        captured = _RecordingOrchestrator.captured
        assert captured["init_kwargs"]["resume"] is True
        assert Path(captured["run_kwargs"]["output_dir"]) == run_dir
        # Fresh-run allocator would have produced a NEW timestamped path
        # under the config's evaluation.output_dir; assert the CLI did NOT
        # do that on the resume branch.
        assert captured["run_kwargs"]["output_dir"] != Path("out")
