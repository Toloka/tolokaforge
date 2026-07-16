"""Load + resolve + backend-selection wiring for the shipped
``examples/native/multi_service_slow_start`` pack.

Proves the pack routes from disk to backend construction on the
no-services-block seam: the project loads with no ``assets.seeds`` and no
per-service isolation, so the default environment resolves to a manifest
whose every service is ``ephemeral`` (making ``requires_per_trial`` true),
and the task-driven selector routes the run onto
:class:`PerTrialRuntimeBackend`. No Docker and no LLM key — this is the
pure load/resolve/select contract; the end-to-end slow start firing is
covered by the sibling integration test.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.models import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
)
from tolokaforge.core.orchestrator import Orchestrator
from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend
from tolokaforge.core.project_loader import load_project_config, resolve
from tolokaforge.runner.models import TaskDescription

pytestmark = pytest.mark.canonical


_PACK_ROOT = (
    Path(__file__).resolve().parents[2] / "examples" / "native" / "multi_service_slow_start"
)
_PROJECT_YAML = _PACK_ROOT / "project.yaml"


def _make_run_config() -> RunConfig:
    return RunConfig(
        models={"agent": ModelConfig(provider="openrouter", name="anthropic/claude-haiku-4-5")},
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir="/tmp/slow_start_pack_smoke"),
    )


def test_pack_loads_without_seed_assets() -> None:
    """The pack is additive over the reset pack minus the reset asset: it
    declares no seeds, so ``project.assets`` is absent or carries an empty
    seed map. A stray ``assets.seeds`` would mean the pack accidentally
    became a reset pack."""
    project = load_project_config(_PROJECT_YAML)
    assert project.assets is None or project.assets.seeds == {}


def test_default_environment_resolves_all_ephemeral() -> None:
    """With no ``default_environment.services`` block the loader fills
    every compose service with the ``ephemeral`` default — none is
    ``shared`` or ``reset`` — so the manifest requires per-trial
    materialisation."""
    project = load_project_config(_PROJECT_YAML)
    manifest = resolve(project.default_environment, None)
    assert manifest is not None
    assert manifest.services
    assert all(spec.isolation == "ephemeral" for spec in manifest.services.values())
    assert all(spec.reset is None for spec in manifest.services.values())
    assert manifest.requires_per_trial is True


def test_backend_selector_routes_pack_to_per_trial() -> None:
    """A run whose task inherits this project's all-ephemeral default
    environment requires per-trial substrate, so backend selection picks
    :class:`PerTrialRuntimeBackend`."""
    project = load_project_config(_PROJECT_YAML)
    manifest = resolve(project.default_environment, None)
    assert manifest is not None

    orch = Orchestrator(_make_run_config(), project=project)
    task = MagicMock()
    task.task_id = "startup_probe"
    orch.tasks = [task]
    task_desc = TaskDescription(
        task_id=task.task_id,
        name=task.task_id,
        category="startup_probe",
        description="",
        adapter_type="native",
        system_prompt="",
        environment_manifest=manifest,
    )
    orch.adapter = MagicMock()
    orch.adapter.to_task_description.side_effect = lambda tid: task_desc

    assert orch._select_backend_from_tasks() == "per_trial"

    backend = orch._construct_runtime_backend(
        runner_address="sentinel:50051",
        env_manifest=None,
        run_id="slow-start-pack-smoke",
    )
    assert isinstance(backend, PerTrialRuntimeBackend)
