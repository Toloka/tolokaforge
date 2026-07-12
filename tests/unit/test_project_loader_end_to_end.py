"""End-to-end tests for the project loader flow — mirrors what the CLI
does when it processes ``--config``.

Each scenario writes a small ``project.yaml`` + ``run_configs/*.yaml``
tree under ``tmp_path`` and drives it through the same resolver chain
the CLI uses. Fast: no Docker, no LLM, no filesystem outside tmp_path.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tolokaforge.core.models import RunConfig
from tolokaforge.core.project_loader import (
    detect_project_layout,
    load_project_config,
    resolve_effective_run_config_data,
    synthesize_default_project,
    warn_legacy_run_config_dir,
)

pytestmark = pytest.mark.unit


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        yaml.safe_dump(data, f)


def _drive_cli_loader(config_path: Path) -> RunConfig:
    """Replay the sequence the CLI uses. Returns the effective RunConfig.

    Kept in sync with ``tolokaforge.cli.main.run``:
    1. Read the YAML at *config_path*.
    2. Detect the enclosing project layout.
    3. Load ``project.yaml`` or synthesise a default.
    4. Merge ``project.run_defaults`` under the run-config dict.
    5. Construct ``RunConfig`` from the merged dict.
    """
    with config_path.open() as f:
        config_data = yaml.safe_load(f)
    project_root, used_legacy_dir = detect_project_layout(config_path)
    if used_legacy_dir:
        warn_legacy_run_config_dir(config_path)
    if project_root is not None:
        project = load_project_config(project_root / "project.yaml")
    else:
        project = synthesize_default_project(project_root=config_path.parent)
    config_data = resolve_effective_run_config_data(project, config_data)
    return RunConfig(**config_data)


class TestProjectAwareLoad:
    def test_run_defaults_layered_under_dev_run_config(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "project.yaml",
            {
                "name": "demo",
                "run_defaults": {
                    "compute": {"workers": 2, "max_budget_usd": 20.0},
                    "orchestrator": {"repeats": 1},
                },
            },
        )
        _write_yaml(
            tmp_path / "run_configs" / "dev.yaml",
            {
                "models": {"agent": {"provider": "openrouter", "name": "test/model"}},
                "orchestrator": {"repeats": 5},  # override just repeats
                "evaluation": {"output_dir": "results/dev"},
            },
        )
        run_config = _drive_cli_loader(tmp_path / "run_configs" / "dev.yaml")
        # Inherited from run_defaults:
        assert run_config.compute is not None
        assert run_config.compute.workers == 2
        assert run_config.compute.max_budget_usd == 20.0
        # Overridden by delta:
        assert run_config.orchestrator.repeats == 5
        # Delta-only fields:
        assert run_config.evaluation.output_dir == "results/dev"

    def test_run_config_delta_wins_on_nested_conflict(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "project.yaml",
            {
                "name": "demo",
                "run_defaults": {
                    "compute": {"workers": 2, "max_budget_usd": 20.0},
                    "orchestrator": {"repeats": 1},  # required by RunConfig schema
                },
            },
        )
        _write_yaml(
            tmp_path / "run_configs" / "nightly.yaml",
            {
                "models": {"agent": {"provider": "openrouter", "name": "test/m"}},
                "compute": {"workers": 16, "max_budget_usd": 200.0},
                "evaluation": {"output_dir": "results/nightly"},
            },
        )
        run_config = _drive_cli_loader(tmp_path / "run_configs" / "nightly.yaml")
        assert run_config.compute is not None
        assert run_config.compute.workers == 16  # delta wins
        assert run_config.compute.max_budget_usd == 200.0
        # orchestrator inherits from run_defaults since delta omits it
        assert run_config.orchestrator.repeats == 1

    def test_run_config_without_project_yaml_uses_synthesised_default(self, tmp_path: Path) -> None:
        _write_yaml(
            tmp_path / "run_configs" / "dev.yaml",
            {
                "models": {"agent": {"provider": "openrouter", "name": "test/m"}},
                "orchestrator": {"repeats": 3},
                "evaluation": {"output_dir": "results/dev"},
            },
        )
        # No project.yaml — loader must synthesise a default silently
        # (info log, no warning).
        run_config = _drive_cli_loader(tmp_path / "run_configs" / "dev.yaml")
        assert run_config.orchestrator.repeats == 3
        assert run_config.compute is None  # nothing to inject

    def test_legacy_run_config_dir_emits_deprecation_warning(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path / "project.yaml", {"name": "demo"})
        _write_yaml(
            tmp_path / "run_config" / "dev.yaml",  # singular — legacy
            {
                "models": {"agent": {"provider": "openrouter", "name": "test/m"}},
                "orchestrator": {"repeats": 1},
                "evaluation": {"output_dir": "results/dev"},
            },
        )
        with pytest.warns(DeprecationWarning, match="run_configs"):
            run_config = _drive_cli_loader(tmp_path / "run_config" / "dev.yaml")
        assert run_config.evaluation.output_dir == "results/dev"

    def test_canonical_run_configs_dir_no_warning(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path / "project.yaml", {"name": "demo"})
        _write_yaml(
            tmp_path / "run_configs" / "dev.yaml",  # plural — canonical
            {
                "models": {"agent": {"provider": "openrouter", "name": "test/m"}},
                "orchestrator": {"repeats": 1},
                "evaluation": {"output_dir": "results/dev"},
            },
        )
        import warnings as _warnings

        with _warnings.catch_warnings():
            _warnings.simplefilter("error", DeprecationWarning)
            _drive_cli_loader(tmp_path / "run_configs" / "dev.yaml")


class TestTaskLoaderWithProjectDefaults:
    """Task-side resolution flowing through the modified ``load_task_yaml``."""

    def test_project_task_defaults_layered_under_task(self, tmp_path: Path) -> None:
        from tolokaforge.adapters._task_loader import load_task_yaml

        # Minimal task.yaml with only per-task identity + one override.
        task_dir = tmp_path / "tasks" / "sample_task"
        task_dir.mkdir(parents=True)
        _write_yaml(
            task_dir / "task.yaml",
            {
                "task_id": "sample",
                "name": "Sample",
                "category": "demo",
                "description": "sample task",
                "initial_state": {},
                "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
                "user_simulator": {"mode": "llm"},
                "grading": "grading.yaml",
                "max_turns": 60,  # task-level override
            },
        )
        # Project supplies max_turns=20 by default; adapter_type=native.
        project_defaults = {
            "adapter_type": "native",
            "max_turns": 20,
            "continue_prompt": "Continue.",
        }
        task, task_dir_out = load_task_yaml(
            task_dir / "task.yaml",
            project_task_defaults=project_defaults,
        )
        assert task.task_id == "sample"
        assert task.max_turns == 60  # task wins
        assert task.adapter_type == "native"  # from defaults

    def test_task_load_without_defaults_matches_pre_m2_behaviour(self, tmp_path: Path) -> None:
        from tolokaforge.adapters._task_loader import load_task_yaml

        task_dir = tmp_path / "tasks" / "legacy"
        task_dir.mkdir(parents=True)
        _write_yaml(
            task_dir / "task.yaml",
            {
                "task_id": "legacy",
                "name": "Legacy",
                "category": "demo",
                "description": "no project.yaml",
                "initial_state": {},
                "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
                "user_simulator": {"mode": "llm"},
                "grading": "grading.yaml",
                "adapter_type": "native",
                "max_turns": 40,
            },
        )
        task, _ = load_task_yaml(task_dir / "task.yaml")
        assert task.task_id == "legacy"
        assert task.max_turns == 40
