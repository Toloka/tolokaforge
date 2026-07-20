"""Canonical-shape load contract for the six project-bearing
``multi_service_*`` example packs.

Each pack ships a ``project.yaml``, a ``run_configs/dev.yaml`` run config,
and a single ``task.yaml``. This suite drives every pack through the same
loader chain the CLI uses — ``load_effective_run_config`` for the run
config and ``load_task_yaml`` (layered under the project's
``task_defaults``) for the task — and asserts two things per pack:

- the pack loads with **zero** ``DeprecationWarning`` (no ``task_packs``
  alias, no legacy ``run_config/`` directory, no top-level
  ``user_simulator``, no flat ``stack`` fields);
- ``actors.user`` drives the resolved user simulator with the pack's
  intended ``mode`` / ``persona``.

Fast: no Docker, no LLM. The end-to-end runs live in the integration suite.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from pathlib import Path

import pytest

from tolokaforge.adapters._task_loader import load_task_yaml
from tolokaforge.core.project_loader import load_effective_run_config, load_project_config

pytestmark = pytest.mark.unit

_PACKS_ROOT = Path(__file__).resolve().parents[2] / "examples" / "native"


@dataclass(frozen=True)
class _PackCase:
    pack: str
    task_rel: str
    task_id: str
    persona: str


_CASES = [
    _PackCase(
        "multi_service_cache_debug",
        "dataset/tasks/cache_debug/task.yaml",
        "cache_debug",
        "on-call engineer",
    ),
    _PackCase(
        "multi_service_endpoint_add",
        "dataset/tasks/endpoint_add/task.yaml",
        "endpoint_add",
        "teammate engineer",
    ),
    _PackCase(
        "multi_service_helpdesk_workflow",
        "dataset/tasks/helpdesk_01/task.yaml",
        "helpdesk_01",
        "logistics coordinator",
    ),
    _PackCase(
        "multi_service_lot_ops", "dataset/tasks/lot_ops_01/task.yaml", "lot_ops_01", "cooperative"
    ),
    _PackCase(
        "multi_service_postgres_reset",
        "dataset/tasks/reset_probe/task.yaml",
        "reset_probe",
        "cooperative",
    ),
    _PackCase(
        "multi_service_slow_start",
        "dataset/tasks/startup_probe/task.yaml",
        "startup_probe",
        "cooperative",
    ),
]

_IDS = [case.pack for case in _CASES]


def _deprecations(caught: list[warnings.WarningMessage]) -> list[warnings.WarningMessage]:
    return [w for w in caught if issubclass(w.category, DeprecationWarning)]


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_run_config_loads_canonically_without_warnings(case: _PackCase) -> None:
    run_config = _PACKS_ROOT / case.pack / "run_configs" / "dev.yaml"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        merged, _ = load_effective_run_config(run_config)
    assert _deprecations(caught) == []
    assert merged["evaluation"]["projects"], "evaluation.projects must be populated"
    assert not merged["evaluation"].get("task_packs"), "legacy task_packs alias must be absent"


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_actors_user_drives_simulator_without_warnings(case: _PackCase) -> None:
    pack_root = _PACKS_ROOT / case.pack
    project = load_project_config(pack_root / "project.yaml")
    project_task_defaults = project.task_defaults.model_dump(exclude_defaults=True) or None
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        task, _ = load_task_yaml(
            pack_root / case.task_rel,
            project_task_defaults=project_task_defaults,
        )
    assert _deprecations(caught) == []
    assert task.task_id == case.task_id
    sim = task.resolve_user_simulator()
    assert sim.mode == "llm"
    assert sim.persona == case.persona
    assert sim.backstory
