"""``state_checks.comparison_view`` on both config models and across the wire.

The block is declared once on the core ``StateChecksConfig`` and once on the runner's
``RunnerStateChecksConfig``, both validated by the same ``ComparisonViewConfig``. A
block that declares no view dumps without the key at all, so a recorded grading config
keeps its bytes and an image predating the key accepts every spec that does not use it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.grading.comparison_view import ComparisonViewConfig, InCondition
from tolokaforge.core.models import Grade, GradingConfig
from tolokaforge.core.models.task_config import StateChecksConfig
from tolokaforge.runner.models import RunnerStateChecksConfig, TaskDescription

pytestmark = pytest.mark.unit

_VIEW: dict[str, Any] = {
    "version": 1,
    "rules": [
        {
            "kind": "exclude_records",
            "table": "holds",
            "where": {"status": {"in": ["released", "expired"]}},
            "reason": "a released hold binds nothing",
        },
        {
            "kind": "normalize_ids",
            "table": "documents",
            "key": ["source_id"],
            "references": [{"table": "corrections", "field": "document_ref"}],
        },
    ],
}


def test_both_models_validate_the_block_through_the_view_model() -> None:
    core = StateChecksConfig(comparison_view=_VIEW)
    runner = RunnerStateChecksConfig(comparison_view=_VIEW)
    assert core.comparison_view == runner.comparison_view == ComparisonViewConfig(**_VIEW)


@pytest.mark.parametrize("model", [StateChecksConfig, RunnerStateChecksConfig])
def test_a_malformed_view_is_refused_by_either_model(model: type[BaseModel]) -> None:
    with pytest.raises(ValidationError, match="unknown comparison_view rule kind 'drop_fields'"):
        model(comparison_view={"version": 1, "rules": [{"kind": "drop_fields"}]})


@pytest.mark.parametrize("model", [StateChecksConfig, RunnerStateChecksConfig])
def test_a_block_without_a_view_dumps_without_the_key(model: type[BaseModel]) -> None:
    block = model()
    assert "comparison_view" not in block.model_dump()
    assert "comparison_view" not in block.model_dump(mode="json")
    assert "comparison_view" not in json.loads(block.model_dump_json())


@pytest.mark.parametrize("model", [StateChecksConfig, RunnerStateChecksConfig])
def test_a_declared_view_round_trips_through_a_plain_json_dump(model: type[BaseModel]) -> None:
    """No ``by_alias``: the trial spec crosses as a plain ``model_dump_json()``."""
    block = model(comparison_view=_VIEW)
    dumped = json.loads(block.model_dump_json())
    assert dumped["comparison_view"]["rules"][0]["where"] == {
        "status": {"in": ["released", "expired"]}
    }
    assert model.model_validate(dumped) == block
    assert model.model_validate_json(block.model_dump_json()) == block


_IMAGE_PREDATING_THE_KEY = create_model(
    "RunnerStateChecksConfigBeforeComparisonView",
    __config__=ConfigDict(extra="forbid"),
    **{
        name: (field.annotation, field)
        for name, field in RunnerStateChecksConfig.model_fields.items()
        if name != "comparison_view"
    },
)
"""The runner's block as an image that never declared the key validates it."""


def test_an_image_predating_the_key_accepts_a_spec_without_a_view_and_refuses_one_with() -> None:
    without = RunnerStateChecksConfig(hash_enabled=True).model_dump_json()
    with_view = RunnerStateChecksConfig(hash_enabled=True, comparison_view=_VIEW).model_dump_json()
    _IMAGE_PREDATING_THE_KEY.model_validate_json(without)
    with pytest.raises(ValidationError, match="comparison_view"):
        _IMAGE_PREDATING_THE_KEY.model_validate_json(with_view)


def _write_pack(root: Path, state_checks: dict[str, Any]) -> NativeAdapter:
    task_dir = root / "tasks" / "view_task"
    task_dir.mkdir(parents=True)
    (task_dir / "initial_state.json").write_text(
        json.dumps(
            {
                "documents": [{"id": "D1", "source_id": "S1"}],
                "corrections": [],
                "holds": [],
            }
        )
    )
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": "view_task",
                "name": "A task declaring a comparison view",
                "category": "test",
                "description": "The adapter translates the view onto the wire.",
                "initial_state": {"json_db": "initial_state.json"},
                "tools": {"agent": {"enabled": ["write_file"]}, "user": {"enabled": []}},
                "actors": {"user": {"mode": "scripted", "scripted_flow": []}},
                "grading": "grading.yaml",
            }
        )
    )
    (task_dir / "grading.yaml").write_text(
        yaml.safe_dump(
            {
                "combine": {
                    "method": "weighted",
                    "weights": {"state_checks": 1.0},
                    "pass_threshold": 0.5,
                },
                "state_checks": state_checks,
            }
        )
    )
    return NativeAdapter({"base_dir": str(root), "tasks_glob": "**/task.yaml"})


_HASH = {"enabled": True, "expect_initial_state": True}


def test_the_native_adapter_carries_the_view_onto_the_wire(tmp_path: Path) -> None:
    adapter = _write_pack(tmp_path, {"hash": _HASH, "comparison_view": _VIEW})
    description = adapter.to_task_description("view_task")
    assert description.grading.state_checks.comparison_view == ComparisonViewConfig(**_VIEW)
    again = TaskDescription.model_validate_json(description.model_dump_json())
    assert again.grading.state_checks.comparison_view == ComparisonViewConfig(**_VIEW)
    core = adapter.get_grading_config("view_task")
    assert core.state_checks.comparison_view == ComparisonViewConfig(**_VIEW)


def test_a_task_without_a_view_puts_no_key_on_the_wire(tmp_path: Path) -> None:
    adapter = _write_pack(tmp_path, {"hash": _HASH})
    description = adapter.to_task_description("view_task")
    wire = json.loads(description.model_dump_json())
    assert "comparison_view" not in wire["grading"]["state_checks"]
    grading = adapter.get_grading_config("view_task")
    assert "comparison_view" not in json.loads(grading.model_dump_json())["state_checks"]
    assert isinstance(grading, GradingConfig)


@pytest.mark.parametrize(
    "model",
    [StateChecksConfig, RunnerStateChecksConfig, Grade, GradingConfig, TaskDescription],
    ids=lambda model: model.__name__,
)
def test_leaving_the_key_out_keeps_the_serialization_schema(model: type[BaseModel]) -> None:
    """The dump that drops an absent view still describes every field it can carry."""
    serialization = model.model_json_schema(mode="serialization")
    validation = model.model_json_schema(mode="validation")
    assert set(serialization["properties"]) == set(validation["properties"])
    for name, definition in validation.get("$defs", {}).items():
        if "properties" in definition and name in serialization["$defs"]:
            assert set(serialization["$defs"][name].get("properties", {})) == set(
                definition["properties"]
            ), f"{model.__name__}: the serialization schema lost {name}'s fields"


def test_the_in_operator_keeps_its_serialization_schema() -> None:
    schema = InCondition.model_json_schema(mode="serialization")
    assert set(schema["properties"]) == {"in"} and schema["required"] == ["in"]
