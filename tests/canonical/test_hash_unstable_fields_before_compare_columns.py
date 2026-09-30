"""Both substrates drop a pack's unstable fields before the ``compare_columns`` pipeline.

The runner's db-service drops ``unstable_fields`` in ``get_stable_state``, and the
runner runs the ``compare_columns`` pipeline on what comes back. Core used to run the
pipeline on the raw state and drop the unstable columns only when it hashed. The
two orders disagree wherever the pipeline reads a whole row: ``order: unordered``
sorts a table's rows by their canonical JSON, so in core the sort still saw a
generated id. A trial that re-created its rows under new ids, in another order,
then matched on the runner and missed in core.

Each arm reaches its verdict its own way: the runner mutates its own db-service and
grades over its real handlers; core grades the final mapping through
``GradingEngine``, reading the pack's ``fixtures/unstable_fields.json``. The hash
source is ``expect_initial_state``, the one both substrates can drive in process.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.utils.grading_parity_packs import FIXTURE_TIMESTAMP
from tests.utils.runner_requests import register_request, trial_spec_json
from tolokaforge.core import models as core_models
from tolokaforge.core.grading.combine import GradingEngine
from tolokaforge.core.models import Trajectory
from tolokaforge.runner import models as runner_models
from tolokaforge.runner import runner_pb2 as pb2

pytestmark = pytest.mark.canonical

_TASK_ID = "parity_unstable_before_compare_columns"
_TABLE = "rows"
_UNSTABLE = [{"table_name": _TABLE, "field_name": "id", "reason": "auto_id"}]
_COMPARE_COLUMNS = {_TABLE: {"label": {"order": "unordered"}}}
_INITIAL_ROWS = [{"id": "R1", "label": "A"}, {"id": "R2", "label": "B"}]


@dataclass(frozen=True)
class _Cell:
    """What the trial left in the table, and the verdict both substrates owe it."""

    name: str
    final_rows: tuple[dict[str, str], ...]
    state_checks: float


#: Every cell re-creates the rows under new ids, which the pack declared unstable.
#: Only the labels count, in any order. The id column is ``id``, which sorts before
#: ``label`` in a row's canonical JSON — so a sort that still sees it orders the rows
#: by the id, as it does for any generated key named before the columns that count.
_CELLS: tuple[_Cell, ...] = (
    _Cell("same_order", ({"id": "R8", "label": "A"}, {"id": "R9", "label": "B"}), 1.0),
    _Cell("reordered", ({"id": "R8", "label": "B"}, {"id": "R9", "label": "A"}), 1.0),
    _Cell("changed_label", ({"id": "R8", "label": "B"}, {"id": "R9", "label": "C"}), 0.0),
)


def _runner_task() -> dict[str, Any]:
    return {
        "task_id": _TASK_ID,
        "name": "Unstable fields before compare_columns",
        "category": "test",
        "description": "A trial that re-created its rows under new ids",
        "adapter_type": "native",
        "system_prompt": "You are a test assistant.",
        "initial_state": {
            "tables": {_TABLE: _INITIAL_ROWS},
            "schemas": [
                {
                    "table_name": _TABLE,
                    "fields": {"id": "string", "label": "string"},
                    "primary_key": "id",
                }
            ],
            "unstable_fields": _UNSTABLE,
        },
        "agent_tools": [],
        "user_tools": [],
        "grading": {
            "combine_method": "weighted",
            "weights": {"state_checks": 1.0},
            "pass_threshold": 0.5,
            "state_checks": {
                "hash_enabled": True,
                "expect_initial_state": True,
                "compare_columns": _COMPARE_COLUMNS,
            },
        },
    }


def _runner_verdict(servicer: Any, context: Any, cell: _Cell) -> float:
    trial_id = f"unstable_before_compare_columns_{cell.name}:0"
    task = runner_models.TaskDescription.model_validate(_runner_task())
    registered = servicer.RegisterTrial(
        register_request(
            trial_spec_json(task.model_dump(mode="json"), trial_id=trial_id), trial_id=trial_id
        ),
        context,
    )
    assert registered.success is True, registered.error
    operations: list[dict[str, Any]] = [{"op": "delete", "filter": {}}]
    operations += [{"op": "insert", "record": dict(row)} for row in cell.final_rows]
    servicer._run_async(servicer.db_client.mutate(trial_id, _TABLE, operations))
    response = servicer.GradeTrial(
        pb2.GradeTrialRequest(
            trial_id=trial_id,
            llm_messages_json=json.dumps([{"role": "assistant", "content": "Done."}]),
        ),
        context,
    )
    assert response.success is True, response.error
    return response.grade.components.state_checks


def _core_verdict(task_dir: Path, cell: _Cell) -> float:
    (task_dir / "fixtures").mkdir(parents=True)
    (task_dir / "fixtures" / "unstable_fields.json").write_text(json.dumps(_UNSTABLE))
    grade = GradingEngine(
        core_models.GradingConfig(
            combine={"method": "weighted", "weights": {"state_checks": 1.0}, "pass_threshold": 0.5},
            state_checks={
                "hash": {"enabled": True, "expect_initial_state": True},
                "compare_columns": _COMPARE_COLUMNS,
            },
        ),
        task_dir=task_dir,
        task_initial_state=core_models.InitialStateConfig(json_db={_TABLE: _INITIAL_ROWS}),
    ).grade_trajectory(
        Trajectory(
            task_id=_TASK_ID,
            trial_index=0,
            start_ts=FIXTURE_TIMESTAMP,
            end_ts=FIXTURE_TIMESTAMP,
            messages=[],
        ),
        {"db": {_TABLE: [dict(row) for row in cell.final_rows]}},
    )
    return grade.components.state_checks


@pytest.mark.parametrize("cell", tuple(pytest.param(cell, id=cell.name) for cell in _CELLS))
def test_both_substrates_drop_unstable_fields_before_ordering_rows(
    cell: _Cell, runner_service: Any, mock_grpc_context: Any, tmp_path: Path
) -> None:
    assert _runner_verdict(runner_service, mock_grpc_context, cell) == pytest.approx(
        cell.state_checks
    )
    assert _core_verdict(tmp_path / "pack", cell) == pytest.approx(cell.state_checks)
