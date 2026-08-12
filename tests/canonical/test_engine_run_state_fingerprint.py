"""Locks the ``models_fingerprint`` field on ``engine_run_state.json``.

Two behaviour surfaces:

* **Overlay sensitivity of the on-disk field** — writing the fingerprint
  from a baseline module state, then reinstalling an overlay and rewriting,
  yields a different persisted ``content_sha256``. This is the AC bullet
  the issue calls "the overlay is folded into the hash".
* **Orchestrator wiring** — after ``Orchestrator.run()`` completes, the
  fingerprint persisted on the run's ``engine_run_state.json`` matches the
  fingerprint the pure seam computes for the current module state. Without
  this lock, a future refactor could swap the computed fingerprint for a
  static fixture at the write site and still compile with every other
  test green.

Canonical tier: resolution goes through the in-memory conductor + runtime
backend so the orchestrator run is Docker-free and LLM-free.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.canonical._factories import write_yaml_file
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.conductor import InMemoryConductor
from tolokaforge.core.engine_run_state import (
    read_engine_run_state,
    read_persisted_models_fingerprint,
    write_engine_run_state,
)
from tolokaforge.core.llm.presets import set_overlay_path
from tolokaforge.core.model_data_fingerprint import compute_models_fingerprint
from tolokaforge.core.models import (
    EvaluationConfig,
    GradingFindingSeverity,
    GradingValidationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
)
from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
from tolokaforge.core.pricing import MODEL_PRICING, reload_pricing
from tolokaforge.core.runtime import InMemoryRuntimeBackend

pytestmark = pytest.mark.canonical

_AGENT = {"provider": "openai", "name": "gpt-4"}


@pytest.fixture
def flat_pack(tmp_path: Path) -> Path:
    """A flat-layout, MCP-free pack the orchestrator can run trivially.

    Uses ``combine.weights.transcript_rules`` because the pack's empty
    tools list makes ``state_checks`` unauthorable at the pre-run gate;
    ``transcript_rules`` has no such dependency and stays valid under the
    same empty pack.
    """
    task_dir = tmp_path / "tasks" / "flat"
    task_dir.mkdir(parents=True)
    (task_dir / "initial_state.json").write_text('{"notes": []}')
    write_yaml_file(
        task_dir / "task.yaml",
        {
            "task_id": "flat",
            "name": "flat",
            "category": "tool_use",
            "description": "flat",
            "initial_state": {"json_db": "initial_state.json"},
            "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
            "grading": "grading.yaml",
        },
    )
    write_yaml_file(
        task_dir / "grading.yaml",
        {
            "transcript_rules": {"max_turns": 100},
            "combine": {
                "method": "weighted",
                "weights": {"transcript_rules": 1.0},
                "pass_threshold": 1.0,
            },
        },
    )
    return tmp_path


@pytest.fixture
def _restore_pricing():
    """Snapshot and restore :data:`MODEL_PRICING` around any test that
    reloads the pricing overlay."""
    snapshot = {k: dict(v) for k, v in MODEL_PRICING.items()}
    yield
    reload_pricing()
    MODEL_PRICING.clear()
    MODEL_PRICING.update(snapshot)


def test_overlay_changes_persisted_fingerprint(
    tmp_path: Path, write_overlay, _restore_pricing: None
) -> None:
    """Two writes bracketed by an overlay-install → different persisted sha.

    Uses the ``write_engine_run_state`` seam directly rather than a full
    orchestrator: the write is a pure function of resolved module state,
    which is exactly what the overlay swap changes.
    """
    baseline_fp = compute_models_fingerprint()
    write_engine_run_state(
        tmp_path,
        run_id="fingerprint-baseline",
        presets_file=None,
        models_fingerprint=baseline_fp,
    )
    baseline_state = read_engine_run_state(tmp_path)

    import tolokaforge_models

    assert baseline_state["models_fingerprint"]["package_version"] == tolokaforge_models.__version__
    assert baseline_state["models_fingerprint"]["api_version"] == 1
    persisted_baseline = read_persisted_models_fingerprint(tmp_path)
    assert persisted_baseline == baseline_fp
    assert len(persisted_baseline.content_sha256) == 64
    assert persisted_baseline.content_sha256 == persisted_baseline.content_sha256.lower()

    preset_overlay = write_overlay(
        {
            "presets": {
                "fingerprint_ac_preset": {
                    "match": ["fingerprint-ac/*"],
                    "schema_sanitizer": "strict",
                }
            }
        }
    )
    set_overlay_path(preset_overlay)

    pricing_overlay = tmp_path / "pricing_overlay.json"
    pricing_overlay.write_text(
        json.dumps({"models": {"fingerprint-ac/probe": {"input": 3.21, "output": 6.54}}})
    )
    reload_pricing(overlay_path=pricing_overlay)

    overlay_fp = compute_models_fingerprint()
    write_engine_run_state(
        tmp_path,
        run_id="fingerprint-overlay",
        presets_file=preset_overlay,
        models_fingerprint=overlay_fp,
    )
    persisted_overlay = read_persisted_models_fingerprint(tmp_path)

    assert persisted_overlay is not None
    assert persisted_overlay.content_sha256 != persisted_baseline.content_sha256


def test_orchestrator_persists_fingerprint_matching_seam(flat_pack: Path, tmp_path: Path) -> None:
    """``Orchestrator.run()`` populates the persisted fingerprint from the seam.

    Runs the orchestrator wired to the in-memory runtime + conductor (no
    Docker, no LLM) over a flat native pack, then checks the fingerprint
    on the resulting ``engine_run_state.json`` matches the same seam call
    made post-run.

    Pins the two ``write_engine_run_state`` call sites in
    ``orchestrator.py``: a refactor that stopped calling
    :func:`compute_models_fingerprint` at either site would fail this
    assertion.
    """
    adapter = NativeAdapter({"base_dir": str(flat_pack), "tasks_glob": "tasks/**/task.yaml"})
    task = adapter.get_task("flat")
    task_desc = adapter.to_task_description(task.task_id)
    task_dir = adapter.get_task_dir(task.task_id)

    run_id = "fingerprint-orchestrator-lock"
    output_dir = tmp_path / run_id
    config = RunConfig(
        models={"agent": ModelConfig(**_AGENT)},
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(
            output_dir=str(output_dir),
            grading_validation=GradingValidationConfig(fail_on=GradingFindingSeverity.ERROR),
        ),
    )
    orch = Orchestrator(
        config,
        deps=OrchestratorDeps(
            runtime_backend=InMemoryRuntimeBackend(),
            conductor_factory=lambda _ctx: InMemoryConductor(),
        ),
    )
    orch.tasks = [task]
    adapter_stub = MagicMock()
    adapter_stub.to_task_description.side_effect = lambda _tid: task_desc
    adapter_stub.get_task_dir.side_effect = lambda _tid: task_dir
    adapter_stub.docker_stack_requirements.return_value = None
    adapter_stub.trial_grader_name = "runner_rpc"
    orch.adapter = adapter_stub
    orch.run(run_id=run_id, output_dir=output_dir)

    persisted = read_persisted_models_fingerprint(output_dir)
    expected = compute_models_fingerprint()

    assert persisted is not None
    assert persisted == expected
