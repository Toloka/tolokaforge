"""Load + resolve + backend-selection wiring for the shipped
``examples/native/multi_service_endpoint_add`` pack.

Proves the ``filesystem_dir`` seam is wired from disk to backend construction
for a real pack: the project loads and verifies the ``pristine_source``
*directory* seed's tree digest (end-to-end proof that directory-seed digest
support works for a shipped project), the default environment resolves to a
manifest that labels ``testrunner`` ``reset``, and the task-driven selector
routes the run onto :class:`PerTrialRuntimeBackend` with the seed in its
registry. No Docker and no LLM key — the end-to-end recipe firing is covered by
the sibling integration test.
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
    Path(__file__).resolve().parents[2] / "examples" / "native" / "multi_service_endpoint_add"
)
_PROJECT_YAML = _PACK_ROOT / "project.yaml"


def _make_run_config() -> RunConfig:
    return RunConfig(
        models={"agent": ModelConfig(provider="openrouter", name="anthropic/claude-haiku-4-5")},
        orchestrator=OrchestratorConfig(workers=1, repeats=2, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir="/tmp/endpoint_add_pack_smoke"),
    )


def test_pack_loads_and_verifies_directory_seed_digest() -> None:
    """``load_project_config`` verifies the ``pristine_source`` directory
    seed's tree digest against the on-disk tree; a stale digest would raise
    here — proving directory-seed digest support is wired for a real pack."""
    project = load_project_config(_PROJECT_YAML)
    assert project.assets is not None
    assert "pristine_source" in project.assets.seeds
    seed = project.assets.seeds["pristine_source"]
    assert seed.kind == "filesystem_dir"
    assert seed.digest.startswith("sha256:")


def test_default_environment_resolves_testrunner_reset() -> None:
    """The default environment resolves to a manifest that labels
    ``testrunner`` ``reset`` (bound to ``pristine_source``) and therefore
    requires per-trial materialisation."""
    project = load_project_config(_PROJECT_YAML)
    manifest = resolve(project.default_environment, None)
    assert manifest is not None
    testrunner = manifest.services["testrunner"]
    assert testrunner.isolation == "reset"
    assert testrunner.reset is not None
    assert testrunner.reset.seed == "pristine_source"
    assert manifest.requires_per_trial is True


def test_backend_selector_routes_pack_to_per_trial() -> None:
    """A run whose task inherits this project's default environment has a
    ``reset`` service, so backend selection picks
    :class:`PerTrialRuntimeBackend` with ``pristine_source`` in its seed
    registry."""
    project = load_project_config(_PROJECT_YAML)
    manifest = resolve(project.default_environment, None)
    assert manifest is not None

    orch = Orchestrator(_make_run_config(), project=project)
    task = MagicMock()
    task.task_id = "endpoint_add"
    orch.tasks = [task]
    task_desc = TaskDescription(
        task_id=task.task_id,
        name=task.task_id,
        category="endpoint_add",
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
        run_id="endpoint-add-pack-smoke",
    )
    assert isinstance(backend, PerTrialRuntimeBackend)
    assert "pristine_source" in backend.seeds
