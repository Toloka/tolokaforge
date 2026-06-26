"""A structured rubric survives the core→runner adapter mapping intact.

The NativeAdapter reads ``grading.yaml`` and builds the runner-side
``LLMJudgeConfig`` that crosses to the runner (serialized inside the TrialSpec).
This pins that a structured ``rubric:`` block — criteria, flags, references —
arrives on ``TaskDescription.grading.llm_judge.rubric`` without loss.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tolokaforge.adapters.native import NativeAdapter

pytestmark = pytest.mark.unit


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _build_task(tmp_path: Path, grading: dict) -> NativeAdapter:
    task_dir = tmp_path / "tasks" / "rubric_task"
    task_dir.mkdir(parents=True)
    (task_dir / "system_prompt.md").write_text("system\n")
    (task_dir / "initial_state.json").write_text("{}")
    _write_yaml(
        task_dir / "task.yaml",
        {
            "task_id": "rubric_task",
            "name": "rubric task",
            "category": "tool_use",
            "description": "rubric task",
            "initial_state": {"json_db": "initial_state.json"},
            "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
            "user_simulator": {"mode": "llm", "persona": "cooperative"},
            "grading": "grading.yaml",
            "system_prompt": "system_prompt.md",
        },
    )
    _write_yaml(task_dir / "grading.yaml", grading)
    return NativeAdapter({"base_dir": str(tmp_path), "tasks_glob": "tasks/**/task.yaml"})


def test_structured_rubric_maps_into_runner_llm_judge(tmp_path: Path):
    adapter = _build_task(
        tmp_path,
        {
            "combine": {
                "method": "weighted",
                "weights": {"llm_judge": 1.0},
                "pass_threshold": 0.8,
            },
            "llm_judge": {
                "rubric": {
                    "reference": "Correct refund is $328.50.",
                    "criteria": [
                        {
                            "id": "refund_amount",
                            "description": "Reply quotes the correct refund amount",
                            "expected": "$328.50",
                            "kind": "binary",
                            "required": True,
                            "weight": 1.0,
                        },
                        {
                            "id": "tone",
                            "description": "Reply is polite and professional",
                            "kind": "graded",
                            "weight": 0.5,
                        },
                    ],
                },
            },
        },
    )

    task_desc = adapter.to_task_description("rubric_task")

    judge = task_desc.grading.llm_judge
    assert judge is not None
    assert judge.rubric.reference == "Correct refund is $328.50."

    by_id = {c.id: c for c in judge.rubric.criteria}
    assert set(by_id) == {"refund_amount", "tone"}
    assert by_id["refund_amount"].kind == "binary"
    assert by_id["refund_amount"].required is True
    assert by_id["refund_amount"].expected == "$328.50"
    assert by_id["tone"].kind == "graded"
    assert by_id["tone"].required is False
    assert by_id["tone"].weight == pytest.approx(0.5)

    # And it survives the gRPC JSON wire trip (TrialSpec embeds the TaskDescription).
    from tolokaforge.runner.models import TaskDescription

    rehydrated = TaskDescription.model_validate_json(task_desc.model_dump_json())
    assert rehydrated.grading.llm_judge.rubric.criteria[0].id == "refund_amount"


def test_legacy_rubric_str_in_task_grading_is_rejected(tmp_path: Path):
    adapter = _build_task(
        tmp_path,
        {
            "combine": {"method": "weighted", "weights": {"llm_judge": 1.0}},
            "llm_judge": {
                "rubric": "free-text rubric blob",
                "output_schema": {"type": "object"},
            },
        },
    )

    with pytest.raises(ValueError, match="rubric is now a structured Rubric"):
        adapter.to_task_description("rubric_task")


def test_legacy_model_ref_in_task_grading_is_rejected(tmp_path: Path):
    """The judge model moved to the run config; a lingering ``model_ref`` fails loud."""
    adapter = _build_task(
        tmp_path,
        {
            "combine": {"method": "weighted", "weights": {"llm_judge": 1.0}},
            "llm_judge": {
                "model_ref": "openai/gpt-4o-mini",
                "rubric": {
                    "criteria": [
                        {"id": "a", "description": "d", "kind": "binary", "weight": 1.0},
                    ],
                },
            },
        },
    )

    with pytest.raises(ValueError, match="model_ref moved to the run config"):
        adapter.to_task_description("rubric_task")
