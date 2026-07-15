"""End-to-end loader + backend-selection wiring against the shipped
``examples/native/example-microservices-pack``.

Covers the load path all the way from ``project.yaml`` (seed digests
verified, default_environment resolved) through per-task manifest
resolution and the task-driven backend selector. The pack labels
``postgres`` ``reset`` with ``reset.seed: app_baseline``; the selector
must therefore route onto :class:`PerTrialRuntimeBackend`. Deliberately
does not spin up Docker or invoke an LLM — those surfaces are exercised
by dedicated integration tests (per-recipe tests, cross-mode isolation
test). This test is the wiring proof for the shipped example.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.env_identity import resolve_environment_identity
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

pytestmark = pytest.mark.integration


_PACK_ROOT = (
    Path(__file__).resolve().parents[2] / "examples" / "native" / "example-microservices-pack"
)
_PROJECT_YAML = _PACK_ROOT / "project.yaml"

_EXPECTED_TASK_IDS = {
    "api_endpoint_add",
    "db_query_tuning",
    "long_debugging_session",
    "postgres_upgrade_test",
    "schema_isolation_migration",
}

# Per-task compose file the resolved manifest must anchor to. Four tasks
# inherit the project's shared stack; ``schema_isolation_migration`` ships a
# full ``stack.compose_file`` override that replaces it.
_SHARED_COMPOSE = _PACK_ROOT / "shared" / "environment.compose.yaml"
_EXPECTED_COMPOSE_FILE = {
    "api_endpoint_add": _SHARED_COMPOSE,
    "db_query_tuning": _SHARED_COMPOSE,
    "long_debugging_session": _SHARED_COMPOSE,
    "postgres_upgrade_test": _SHARED_COMPOSE,
    "schema_isolation_migration": (
        _PACK_ROOT / "tasks" / "schema_isolation_migration" / "environment.compose.yaml"
    ),
}


def _make_run_config() -> RunConfig:
    return RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4")},
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir="/tmp/pack_smoke"),
    )


def _make_pack_adapter() -> NativeAdapter:
    """Build a ``NativeAdapter`` for the pack via the same wiring the
    :class:`Orchestrator` uses: the project's discovery glob under the pack
    root, with ``project.task_defaults`` and ``default_environment`` layered
    in (see ``Orchestrator._create_adapter``)."""
    project = load_project_config(_PROJECT_YAML)
    defaults = project.task_defaults.model_dump(exclude_defaults=True)
    return NativeAdapter(
        {
            "tasks_glob": project.tasks.discovery.glob,
            "task_packs": [str(_PACK_ROOT)],
            "project_task_defaults": defaults or None,
            "project_default_environment": project.default_environment,
        }
    )


def test_pack_loads_and_verifies_digests() -> None:
    """The shipped ``app_baseline`` seed's digest must match the file
    bytes; the loader raises otherwise."""
    project = load_project_config(_PROJECT_YAML)
    assert project.assets is not None
    assert "app_baseline" in project.assets.seeds
    baseline = project.assets.seeds["app_baseline"]
    assert baseline.kind == "sql_dump"
    assert baseline.digest.startswith("sha256:")


def test_default_environment_resolves_with_expected_services() -> None:
    """The project's default_environment resolves to a manifest with
    the four declared services and the recipe-bound ``postgres``."""
    project = load_project_config(_PROJECT_YAML)
    manifest = resolve(project.default_environment, None)
    assert manifest is not None
    assert set(manifest.services) == {"runner", "db-service", "postgres", "backend-api"}
    postgres = manifest.services["postgres"]
    assert postgres.isolation == "reset"
    assert postgres.reset is not None
    assert postgres.reset.seed == "app_baseline"
    assert manifest.services["db-service"].isolation == "shared"
    assert manifest.services["backend-api"].isolation == "shared"
    assert manifest.services["runner"].isolation == "ephemeral"
    assert manifest.requires_per_trial is True


def test_backend_selector_routes_pack_to_per_trial() -> None:
    """A run whose tasks come from this pack has at least one
    ``reset`` service, so task-driven backend selection picks
    :class:`PerTrialRuntimeBackend`."""
    project = load_project_config(_PROJECT_YAML)
    manifest = resolve(project.default_environment, None)
    assert manifest is not None

    orch = Orchestrator(_make_run_config(), project=project)
    task = MagicMock()
    task.task_id = "postgres_upgrade_test"
    orch.tasks = [task]
    task_desc = TaskDescription(
        task_id=task.task_id,
        name=task.task_id,
        category="microservices",
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
        run_id="pack-smoke",
    )
    assert isinstance(backend, PerTrialRuntimeBackend)
    assert "app_baseline" in backend.seeds


def test_pack_manifest_has_stable_environment_identity() -> None:
    """``resolve_environment_identity`` returns a stable
    ``sha256:...`` digest over the pack's default environment."""
    project = load_project_config(_PROJECT_YAML)
    manifest = resolve(project.default_environment, None)
    assert manifest is not None
    seed_digests = {name: seed.digest for name, seed in project.assets.seeds.items()}
    identity = resolve_environment_identity(manifest, seed_digests)
    again = resolve_environment_identity(manifest, seed_digests)
    assert identity == again
    assert identity.startswith("sha256:")
    assert len(identity) == len("sha256:") + 64


def test_adapter_discovers_all_five_pack_tasks() -> None:
    """The native adapter, wired the orchestrator's way, discovers every
    task in the pack — the minimal ``task_id`` + ``description`` shape loads
    without the four formerly-required fields."""
    adapter = _make_pack_adapter()
    assert set(adapter.get_task_ids()) == _EXPECTED_TASK_IDS


@pytest.mark.parametrize("task_id", sorted(_EXPECTED_TASK_IDS))
def test_task_builds_description_with_judge_rubric_and_llm_user(task_id: str) -> None:
    """Each minimal pack task builds a ``TaskDescription`` whose grading
    carries the per-task ``llm_judge`` rubric (auto-picked from the sibling
    ``grading.yaml``) and whose user simulator is the inherited cooperative
    LLM default — the end-to-end proof that the schema relaxation and
    sibling-grading pickup wire through ``NativeAdapter``."""
    adapter = _make_pack_adapter()

    task_desc = adapter.to_task_description(task_id)

    assert task_desc.grading.llm_judge is not None
    assert len(task_desc.grading.llm_judge.rubric.criteria) > 0
    assert task_desc.user_simulator.mode == "llm"


@pytest.mark.parametrize("task_id", sorted(_EXPECTED_TASK_IDS))
def test_task_manifest_resolves_expected_stack(task_id: str) -> None:
    """``schema_isolation_migration`` resolves its task-local
    ``stack.compose_file`` override; the other four resolve the project's
    shared stack. Both anchor to an absolute compose path on disk."""
    adapter = _make_pack_adapter()

    manifest = adapter.to_task_description(task_id).environment_manifest

    assert manifest is not None
    assert manifest.compose_file == _EXPECTED_COMPOSE_FILE[task_id].resolve()
