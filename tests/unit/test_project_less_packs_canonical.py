"""Canonical-shape load contract for the seven example packs that gained a
``project.yaml`` in the M4 migration.

``browser_task``, ``coding``, ``tool_use``, ``multi_service``,
``multi_service_advanced``, ``multi_service_postgres`` and
``native_shared_domain`` each ship a ``project.yaml``, one or more
``run_configs/*.yaml`` run configs, and a ``dataset/`` task tree. This
suite drives every pack through the same loader chain the CLI uses —
``load_effective_run_config`` for each run config and ``load_task_yaml``
(layered under the project's ``task_defaults``) for each task — and asserts:

- every run config loads with **zero** ``DeprecationWarning`` and resolves
  ``evaluation.projects`` (no ``task_packs`` alias, no legacy
  ``run_config/`` directory);
- every task loads with **zero** ``DeprecationWarning`` (no top-level
  ``user_simulator``, no flat ``stack`` fields) and ``actors.user`` drives
  the resolved user simulator.

Fast: no Docker, no LLM. End-to-end runs live in the integration suite.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from tolokaforge.adapters._task_loader import load_task_yaml
from tolokaforge.core.project_loader import load_effective_run_config, load_project_config

pytestmark = pytest.mark.unit

_PACKS_ROOT = Path(__file__).resolve().parents[2] / "examples" / "native"

_PACKS = [
    "browser_task",
    "coding",
    "tool_use",
    "multi_service",
    "multi_service_advanced",
    "multi_service_postgres",
    "native_shared_domain",
]


def _run_configs() -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for pack in _PACKS:
        for cfg in sorted((_PACKS_ROOT / pack / "run_configs").glob("*.yaml")):
            out.append((f"{pack}/{cfg.name}", cfg))
    return out


def _tasks() -> list[tuple[str, str, Path]]:
    out: list[tuple[str, str, Path]] = []
    for pack in _PACKS:
        for task in sorted((_PACKS_ROOT / pack / "dataset").glob("**/task.yaml")):
            out.append((f"{pack}/{task.parent.name}", pack, task))
    return out


_RUN_CONFIGS = _run_configs()
_TASKS = _tasks()


def _deprecations(caught: list[warnings.WarningMessage]) -> list[warnings.WarningMessage]:
    return [w for w in caught if issubclass(w.category, DeprecationWarning)]


def test_every_pack_has_project_yaml() -> None:
    for pack in _PACKS:
        assert (_PACKS_ROOT / pack / "project.yaml").is_file(), f"{pack} missing project.yaml"


def test_no_run_config_at_pack_root() -> None:
    for pack in _PACKS:
        stale = _PACKS_ROOT / pack / "run_config.yaml"
        assert not stale.exists(), f"{pack} still has a run_config.yaml at its root"


@pytest.mark.parametrize("case", _RUN_CONFIGS, ids=[c[0] for c in _RUN_CONFIGS])
def test_run_config_loads_canonically_without_warnings(case: tuple[str, Path]) -> None:
    _, run_config = case
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        merged, _ = load_effective_run_config(run_config)
    assert _deprecations(caught) == []
    assert merged["evaluation"]["projects"], "evaluation.projects must be populated"
    assert not merged["evaluation"].get("task_packs"), "legacy task_packs alias must be absent"


@pytest.mark.parametrize("case", _TASKS, ids=[c[0] for c in _TASKS])
def test_task_loads_and_actors_user_drives_simulator(case: tuple[str, str, Path]) -> None:
    _, pack, task_path = case
    project = load_project_config(_PACKS_ROOT / pack / "project.yaml")
    project_task_defaults = project.task_defaults.model_dump(exclude_defaults=True) or None
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        task, _ = load_task_yaml(task_path, project_task_defaults=project_task_defaults)
    assert _deprecations(caught) == []
    sim = task.resolve_user_simulator()
    assert sim.mode == "llm"
    assert sim.persona == "cooperative"
    assert sim.backstory
