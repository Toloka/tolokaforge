"""Load + resolve + backend construction wiring for the shipped
``examples/native/multi_service_postgres_reset`` pack.

Proves the reset seam is wired from disk to backend construction: the
project loads and verifies the ``postgres_baseline`` seed digest, the
default environment resolves to a manifest that labels ``app-db``
``reset``, and the orchestrator constructs a
:class:`SharedStackRuntimeBackend` with the seed in its registry. No
Docker and no LLM key — this is the pure load/resolve/construct
contract; the end-to-end recipe firing is covered by the sibling
integration test.
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
from tolokaforge.core.project_loader import load_project_config, resolve
from tolokaforge.core.shared_stack_runtime import SharedStackRuntimeBackend
from tolokaforge.runner.models import TaskDescription

pytestmark = pytest.mark.canonical


_PACK_ROOT = (
    Path(__file__).resolve().parents[2] / "examples" / "native" / "multi_service_postgres_reset"
)
_PROJECT_YAML = _PACK_ROOT / "project.yaml"


def _make_run_config() -> RunConfig:
    return RunConfig(
        models={"agent": ModelConfig(provider="openrouter", name="anthropic/claude-haiku-4-5")},
        orchestrator=OrchestratorConfig(workers=1, repeats=2, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir="/tmp/reset_pack_smoke"),
    )


def test_pack_loads_and_verifies_seed_digest() -> None:
    """``load_project_config`` verifies the ``postgres_baseline`` seed's
    digest against the file bytes; a stale digest would raise here."""
    project = load_project_config(_PROJECT_YAML)
    assert project.assets is not None
    assert "postgres_baseline" in project.assets.seeds
    seed = project.assets.seeds["postgres_baseline"]
    assert seed.kind == "sql_dump"
    assert seed.digest.startswith("sha256:")


def test_default_environment_resolves_app_db_reset() -> None:
    """The default environment resolves to a manifest that labels
    ``app-db`` ``reset`` (bound to ``postgres_baseline``) and therefore
    requires per-trial materialisation."""
    project = load_project_config(_PROJECT_YAML)
    manifest = resolve(project.default_environment, None)
    assert manifest is not None
    app_db = manifest.services["app-db"]
    assert app_db.isolation == "reset"
    assert app_db.reset is not None
    assert app_db.reset.seed == "postgres_baseline"
    assert manifest.requires_per_trial is True


def test_backend_selector_routes_pack_to_per_trial() -> None:
    """A run whose task inherits this project's default environment has a
    ``reset`` service, so the orchestrator constructs
    :class:`SharedStackRuntimeBackend` with ``postgres_baseline`` in its
    seed registry — the composer sequences the per-trial reset recipe."""
    project = load_project_config(_PROJECT_YAML)
    manifest = resolve(project.default_environment, None)
    assert manifest is not None

    orch = Orchestrator(_make_run_config(), project=project)
    task = MagicMock()
    task.task_id = "reset_probe"
    orch.tasks = [task]
    task_desc = TaskDescription(
        task_id=task.task_id,
        name=task.task_id,
        category="reset_probe",
        description="",
        adapter_type="native",
        system_prompt="",
        environment_manifest=manifest,
    )
    orch.adapter = MagicMock()
    orch.adapter.to_task_description.side_effect = lambda tid: task_desc

    backend = orch._construct_runtime_backend(
        runner_address="sentinel:50051",
        env_manifest=None,
        run_id="reset-pack-smoke",
    )
    assert isinstance(backend, SharedStackRuntimeBackend)
    assert "postgres_baseline" in backend.seeds
