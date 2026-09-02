"""``Orchestrator(config_path=...)`` records the operator's YAML path in ``run_state.json``.

The fresh-run branch of :meth:`Orchestrator.run` threads the constructor's
``config_path`` verbatim into :meth:`RunStateManager.initialize_run`, which
normalises it to CWD-relative and writes it to disk. A programmatic caller that
supplies no path gets the ``""`` sentinel — :class:`RunState.config_path` stays
``str``.

The test exercises the real seam (``__init__`` → ``run()`` → ``initialize_run``
→ disk) and short-circuits ``run()`` immediately after the state file lands, by
patching :func:`write_engine_run_state` — the very next call after the seam
under test — to raise a sentinel. :class:`RunStateManager` is not stubbed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

from tests.canonical._factories import make_task_config
from tolokaforge.core.models import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
)
from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
from tolokaforge.core.runtime import InMemoryRuntimeBackend
from tolokaforge.runner.models import RunnerGradingConfig, TaskDescription


class _AbortRunAfterStateWrite(Exception):
    """Sentinel: ``write_engine_run_state`` fired, so ``run_state.json`` is on disk."""


def _stub_task_description(task_id: str) -> TaskDescription:
    return TaskDescription(
        task_id=task_id,
        name=task_id,
        category="test",
        description="d",
        adapter_type="native",
        system_prompt="sys",
        grading=RunnerGradingConfig(llm_judge=None),
    )


def _minimal_run_config(output_dir: Path) -> RunConfig:
    return RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4")},
        orchestrator=OrchestratorConfig(
            workers=1,
            repeats=1,
            auto_start_services=False,
            shuffle_trials=False,
        ),
        evaluation=EvaluationConfig(output_dir=str(output_dir)),
    )


def _drive_fresh_run_state_write(
    orch: Orchestrator,
    *,
    run_id: str,
    output_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Call ``orch.run()`` far enough to write ``run_state.json``, then stop.

    Sentinel raised from the ``write_engine_run_state`` call right after
    ``RunStateManager.initialize_run`` returns, so the state file is on disk
    when the exception propagates.
    """

    def _abort(*_args: Any, **_kwargs: Any) -> None:
        raise _AbortRunAfterStateWrite

    monkeypatch.setattr("tolokaforge.core.orchestrator.write_engine_run_state", _abort)
    with pytest.raises(_AbortRunAfterStateWrite):
        orch.run(run_id=run_id, output_dir=output_dir)


def _orchestrator_ready_for_fresh_run(
    config: RunConfig,
    *,
    config_path: Path | None,
    task_id: str = "TASK-A",
) -> Orchestrator:
    orch = Orchestrator(
        config,
        deps=OrchestratorDeps(runtime_backend=InMemoryRuntimeBackend()),
        config_path=config_path,
    )
    orch.tasks = [make_task_config(task_id)]
    adapter = MagicMock()
    adapter.to_task_description.side_effect = _stub_task_description
    adapter.fingerprint.return_value = None
    orch.adapter = adapter
    return orch


class TestOrchestratorConfigPathThreading:
    """``config_path`` supplied at construction reaches ``run_state.json``."""

    def test_relative_cli_path_lands_verbatim_in_run_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        cfg_dir = tmp_path / "configs"
        cfg_dir.mkdir()
        (cfg_dir / "run.yaml").write_text("# placeholder\n")

        output_dir = tmp_path / "results" / "run_0"
        run_config = _minimal_run_config(output_dir=output_dir)
        orch = _orchestrator_ready_for_fresh_run(run_config, config_path=Path("configs/run.yaml"))

        _drive_fresh_run_state_write(
            orch, run_id="run_0", output_dir=output_dir, monkeypatch=monkeypatch
        )

        state = json.loads((output_dir / "run_state.json").read_text())
        assert state["config_path"] == "configs/run.yaml"

    def test_none_config_path_writes_empty_sentinel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)

        output_dir = tmp_path / "results" / "run_0"
        run_config = _minimal_run_config(output_dir=output_dir)
        orch = _orchestrator_ready_for_fresh_run(run_config, config_path=None)

        _drive_fresh_run_state_write(
            orch, run_id="run_0", output_dir=output_dir, monkeypatch=monkeypatch
        )

        state = json.loads((output_dir / "run_state.json").read_text())
        assert state["config_path"] == ""
