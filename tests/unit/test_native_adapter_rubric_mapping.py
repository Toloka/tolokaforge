"""A structured rubric survives the core→runner adapter mapping intact.

The NativeAdapter reads ``grading.yaml`` and builds the runner-side
``LLMJudgeConfig`` that crosses to the runner (serialized inside the TrialSpec).
This pins that a structured ``rubric:`` block — criteria, flags, references —
arrives on ``TaskDescription.grading.llm_judge.rubric`` without loss.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.canonical._factories import write_yaml_file
from tolokaforge.adapters.native import NativeAdapter

pytestmark = pytest.mark.unit


def _build_task(
    tmp_path: Path, grading: dict, *, project_task_defaults: dict | None = None
) -> NativeAdapter:
    task_dir = tmp_path / "tasks" / "rubric_task"
    task_dir.mkdir(parents=True)
    (task_dir / "system_prompt.md").write_text("system\n")
    (task_dir / "initial_state.json").write_text("{}")
    write_yaml_file(
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
    write_yaml_file(task_dir / "grading.yaml", grading)
    params: dict = {"base_dir": str(tmp_path), "tasks_glob": "tasks/**/task.yaml"}
    if project_task_defaults is not None:
        params["project_task_defaults"] = project_task_defaults
    return NativeAdapter(params)


def _rubric_grading(customization: dict | None = None) -> dict:
    """A minimal valid rubric grading block, optionally with a customization sub-block."""
    llm_judge: dict = {
        "rubric": {
            "criteria": [
                {"id": "a", "description": "d", "kind": "binary", "weight": 1.0},
            ],
        },
    }
    if customization is not None:
        llm_judge["customization"] = customization
    return {
        "combine": {"method": "weighted", "weights": {"llm_judge": 1.0}},
        "llm_judge": llm_judge,
    }


def _judge_defaults(customization: dict) -> dict:
    """A project ``task_defaults`` carrying only a judge customization default."""
    return {"grading_defaults": {"llm_judge": {"customization": customization}}}


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


# ---------------------------------------------------------------------------
# Judge customization: project→task layering + "attach only when set"
# ---------------------------------------------------------------------------


def test_customization_absent_leaves_llm_judge_config_identical(tmp_path: Path):
    """A rubric task with no customization block (and no project default) produces
    ``customization is None`` — the parsed config is identical to one built without
    the feature, and no nested customization object is attached (the wire carries a
    plain ``"customization": null``)."""
    from tolokaforge.runner.models import LLMJudgeConfig

    adapter = _build_task(tmp_path, _rubric_grading())
    judge = adapter.to_task_description("rubric_task").grading.llm_judge

    assert judge.customization is None
    # No nested-null object on the wire; reconstructs identically.
    rehydrated = LLMJudgeConfig.model_validate_json(judge.model_dump_json())
    assert rehydrated.customization is None


def test_explicit_null_llm_judge_key_loads_as_no_judge(tmp_path: Path):
    """A grading.yaml whose ``llm_judge:`` key is explicit-null (YAML parses it to
    ``None``) loads with ``grading.llm_judge is None`` instead of crashing the
    customization extraction on a ``None`` block."""
    adapter = _build_task(
        tmp_path,
        {
            "combine": {"method": "weighted", "weights": {"state_checks": 1.0}},
            "llm_judge": None,
        },
    )

    task_desc = adapter.to_task_description("rubric_task")

    assert task_desc.grading.llm_judge is None


def test_task_customization_attached_when_set(tmp_path: Path):
    adapter = _build_task(tmp_path, _rubric_grading({"disable_knowledge_search": True}))
    judge = adapter.to_task_description("rubric_task").grading.llm_judge

    assert judge.customization is not None
    assert judge.customization.disable_knowledge_search is True


def test_project_default_inherited_when_task_unset(tmp_path: Path):
    """A project default disables KB search; a rubric task with no customization
    block inherits it (task-unset never clears a set project default)."""
    adapter = _build_task(
        tmp_path,
        _rubric_grading(),
        project_task_defaults=_judge_defaults({"disable_knowledge_search": True}),
    )
    judge = adapter.to_task_description("rubric_task").grading.llm_judge

    assert judge.customization is not None
    assert judge.customization.disable_knowledge_search is True


def test_task_false_overrides_project_true(tmp_path: Path):
    """Tri-state: an explicit task ``false`` overrides a project ``true``."""
    adapter = _build_task(
        tmp_path,
        _rubric_grading({"disable_knowledge_search": False}),
        project_task_defaults=_judge_defaults({"disable_knowledge_search": True}),
    )
    judge = adapter.to_task_description("rubric_task").grading.llm_judge

    assert judge.customization is not None
    assert judge.customization.disable_knowledge_search is False


def test_system_prompt_task_string_overrides_project_string(tmp_path: Path):
    """A task ``system_prompt`` string wins over a project ``system_prompt`` string."""
    adapter = _build_task(
        tmp_path,
        _rubric_grading({"system_prompt": "Task judge voice."}),
        project_task_defaults=_judge_defaults({"system_prompt": "Project judge voice."}),
    )
    judge = adapter.to_task_description("rubric_task").grading.llm_judge

    assert judge.customization is not None
    assert judge.customization.system_prompt == "Task judge voice."


def test_system_prompt_inherited_when_task_key_absent(tmp_path: Path):
    """A task with no ``system_prompt`` key inherits the project string (absent
    never clears a set project default)."""
    adapter = _build_task(
        tmp_path,
        _rubric_grading(),
        project_task_defaults=_judge_defaults({"system_prompt": "Project judge voice."}),
    )
    judge = adapter.to_task_description("rubric_task").grading.llm_judge

    assert judge.customization is not None
    assert judge.customization.system_prompt == "Project judge voice."


def test_system_prompt_task_null_resets_project_string_to_default(tmp_path: Path):
    """A task ``system_prompt: null`` (key present, value null) resets a project
    string back to the default prompt — distinct from omitting the key, which
    inherits. ``deep_merge`` treats a present null as an override."""
    adapter = _build_task(
        tmp_path,
        _rubric_grading({"system_prompt": None}),
        project_task_defaults=_judge_defaults({"system_prompt": "Project judge voice."}),
    )
    judge = adapter.to_task_description("rubric_task").grading.llm_judge

    assert judge.customization is not None
    assert judge.customization.system_prompt is None


def test_system_prompt_none_when_no_layer_sets_it(tmp_path: Path):
    """Both layers unset ⇒ ``system_prompt is None`` (the default judge prompt)."""
    adapter = _build_task(tmp_path, _rubric_grading({"disable_knowledge_search": True}))
    judge = adapter.to_task_description("rubric_task").grading.llm_judge

    assert judge.customization is not None
    assert judge.customization.system_prompt is None


def test_include_agent_system_prompt_task_true_overrides_project_false(tmp_path: Path):
    """A task ``true`` re-includes the agent policy over a project ``false``."""
    adapter = _build_task(
        tmp_path,
        _rubric_grading({"include_agent_system_prompt": True}),
        project_task_defaults=_judge_defaults({"include_agent_system_prompt": False}),
    )
    judge = adapter.to_task_description("rubric_task").grading.llm_judge

    assert judge.customization is not None
    assert judge.customization.include_agent_system_prompt is True


def test_include_agent_system_prompt_task_false_overrides_project_true(tmp_path: Path):
    """A task ``false`` gates the agent policy out over a project ``true``."""
    adapter = _build_task(
        tmp_path,
        _rubric_grading({"include_agent_system_prompt": False}),
        project_task_defaults=_judge_defaults({"include_agent_system_prompt": True}),
    )
    judge = adapter.to_task_description("rubric_task").grading.llm_judge

    assert judge.customization is not None
    assert judge.customization.include_agent_system_prompt is False


def test_include_agent_system_prompt_inherited_when_task_key_absent(tmp_path: Path):
    """A task with no ``include_agent_system_prompt`` key inherits the project
    value (absent never clears a set project default)."""
    adapter = _build_task(
        tmp_path,
        _rubric_grading(),
        project_task_defaults=_judge_defaults({"include_agent_system_prompt": False}),
    )
    judge = adapter.to_task_description("rubric_task").grading.llm_judge

    assert judge.customization is not None
    assert judge.customization.include_agent_system_prompt is False


def test_include_agent_system_prompt_task_null_resets_project_value_to_default(tmp_path: Path):
    """A task ``include_agent_system_prompt: null`` (key present, value null)
    resets a project ``false`` back to the include default — distinct from
    omitting the key, which inherits. ``deep_merge`` treats a present null as an
    override; the resolved ``None`` means the default (include)."""
    adapter = _build_task(
        tmp_path,
        _rubric_grading({"include_agent_system_prompt": None}),
        project_task_defaults=_judge_defaults({"include_agent_system_prompt": False}),
    )
    judge = adapter.to_task_description("rubric_task").grading.llm_judge

    assert judge.customization is not None
    assert judge.customization.include_agent_system_prompt is None


def test_include_agent_system_prompt_none_when_no_layer_sets_it(tmp_path: Path):
    """Both layers unset ⇒ ``include_agent_system_prompt is None`` (the include
    default)."""
    adapter = _build_task(tmp_path, _rubric_grading({"disable_knowledge_search": True}))
    judge = adapter.to_task_description("rubric_task").grading.llm_judge

    assert judge.customization is not None
    assert judge.customization.include_agent_system_prompt is None
