"""The comparison-view reference pack, graded end to end on both substrates.

``examples/native/comparison_view`` files a client's invoice and a correction against it,
and grades the trial by a state hash against its golden path read through a comparison
view that uses all three rule kinds. Each scenario below is a scripted trajectory: the
pack's own tools, called in order on a fresh copy of its initial state, leave the final
state both substrates grade.

- The runner: the pack through the native adapter onto ``RegisterTrial``; the trial's
  database is written to the final state; ``GradeTrial`` replays the golden path through
  the pack's own tools over that database and reads both full states back.
- Core: ``GradingEngine.grade_trajectory`` replays the golden path through the pack's
  ``mcp_server.py`` and hashes the final state against it.

An acceptable alternative passes, a near miss fails with the view diff naming the
correction, a trial whose documents share a key fails with the collision, and a released
hold a correction cites is kept and fails — with the same record of the view on both.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Any

import pytest
from click.testing import CliRunner

from tests.utils.comparison_view_runner import TRANSCRIPT, register, write_state
from tests.utils.example_packs import EXAMPLES_ROOT
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.grading.combine import GradingEngine
from tolokaforge.core.models import Trajectory
from tolokaforge.core.project_loader import load_project_config
from tolokaforge.dx.cli.main import cli
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.models import ComparisonViewGradeRecord

pytestmark = [pytest.mark.canonical, pytest.mark.grading]

_PROJECT = EXAMPLES_ROOT / "native" / "comparison_view"
_TASK_ID = "file_document_correction"
_TASK_DIR = _PROJECT / "dataset" / "tasks" / _TASK_ID
_INVOICE = {"client_id": "C-1", "source_id": "INV-2002", "kind": "invoice"}
_REASON = "amount should be 1240.00"
_NEW_KEY = 'documents:{"client_id":"C-1","source_id":"INV-2002"}'

Call = tuple[str, dict[str, Any]]


def _pack_tools() -> ModuleType:
    spec = importlib.util.spec_from_file_location("document_desk", _TASK_DIR / "mcp_server.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_TOOLS = _pack_tools().TOOLS


def _final_state(calls: tuple[Call, ...]) -> dict[str, Any]:
    """The database a trajectory making exactly these calls leaves behind."""
    data = json.loads((_TASK_DIR / "initial_state.json").read_text())
    for name, kwargs in calls:
        result = _TOOLS[name].invoke(data=data, **kwargs)
        assert "error" not in result, f"the scripted call {name}({kwargs}) failed: {result}"
    return data


@dataclass(frozen=True)
class _Scenario:
    name: str
    calls: tuple[Call, ...]
    state_checks: float
    check: Callable[[ComparisonViewGradeRecord], None]


def _the_alternative_is_seen_past(record: ComparisonViewGradeRecord) -> None:
    assert record.view_diff is None and record.trial is not None
    applied = {(a.kind, a.table): a for a in record.trial.applied}
    assert applied[("exclude_tables", "lookup_log")].rows_removed == 2
    assert applied[("exclude_records", "documents")].rows_removed == 1
    assert applied[("exclude_records", "holds")].rows_removed == 1
    normalized = applied[("normalize_ids", "documents")]
    assert (normalized.ids_rewritten, normalized.references_rewritten) == (1, 1)
    golden = {(a.kind, a.table): a for a in record.golden.applied}
    assert golden[("exclude_records", "documents")].rows_removed == 0
    assert [field.dotted for field in record.golden.rekeyed_fields] == ["documents.id"]


def _the_correction_cites_the_draft(record: ComparisonViewGradeRecord) -> None:
    assert record.view_diff is not None
    assert set(record.view_diff.tables) == {"corrections"}
    (different,) = record.view_diff.tables["corrections"].different
    assert {"field": "document_ref", "expected": _NEW_KEY, "actual": "DOC-002"} in different[
        "field_diffs"
    ]


def _the_documents_collide(record: ComparisonViewGradeRecord) -> None:
    assert record.trial is None and record.trial_collision is not None
    assert set(record.trial_collision.ids) == {"DOC-002", "DOC-003"}


def _the_cited_hold_is_kept(record: ComparisonViewGradeRecord) -> None:
    assert record.view_diff is not None
    assert set(record.view_diff.tables) == {"corrections", "holds"}
    (kept,) = record.view_diff.tables["holds"].extra
    assert kept["status"] == "released"


_SCENARIOS: tuple[_Scenario, ...] = (
    _Scenario(
        "the_golden_path_itself",
        (
            ("file_document", _INVOICE),
            ("file_correction", {"document_id": "DOC-002", "reason": _REASON}),
        ),
        1.0,
        lambda record: None,
    ),
    _Scenario(
        "an_acceptable_alternative",
        (
            ("find_client", {"client_id": "C-1"}),
            ("file_document", _INVOICE),
            ("lookup_document", {"document_id": "DOC-002"}),
            ("supersede_document", {"document_id": "DOC-002"}),
            ("file_document", _INVOICE),
            ("place_hold", {"client_id": "C-1", "amount": 1240.0}),
            ("release_hold", {"hold_id": "HOLD-001"}),
            ("file_correction", {"document_id": "DOC-003", "reason": _REASON}),
        ),
        1.0,
        _the_alternative_is_seen_past,
    ),
    _Scenario(
        "a_near_miss_citing_the_superseded_draft",
        (
            ("file_document", _INVOICE),
            ("supersede_document", {"document_id": "DOC-002"}),
            ("file_document", _INVOICE),
            ("file_correction", {"document_id": "DOC-002", "reason": _REASON}),
        ),
        0.0,
        _the_correction_cites_the_draft,
    ),
    _Scenario(
        "a_trial_filing_the_invoice_twice_collides",
        (
            ("file_document", _INVOICE),
            ("file_document", _INVOICE),
            ("file_correction", {"document_id": "DOC-003", "reason": _REASON}),
        ),
        0.0,
        _the_documents_collide,
    ),
    _Scenario(
        "a_released_hold_the_correction_cites_is_kept",
        (
            ("file_document", _INVOICE),
            ("place_hold", {"client_id": "C-1", "amount": 1240.0}),
            ("release_hold", {"hold_id": "HOLD-001"}),
            (
                "file_correction",
                {"document_id": "DOC-002", "reason": _REASON, "hold_id": "HOLD-001"},
            ),
        ),
        0.0,
        _the_cited_hold_is_kept,
    ),
)


def _adapter() -> NativeAdapter:
    project = load_project_config(_PROJECT / "project.yaml")
    return NativeAdapter(
        {
            "tasks_glob": "dataset/tasks/**/task.yaml",
            "task_packs": [str(_PROJECT)],
            "project_task_defaults": project.task_defaults.model_dump(exclude_defaults=True)
            or None,
        }
    )


def _in_process_tool(servicer: Any, trial_id: str, name: str):
    """The pack's own tool over the trial's database, where the runner would run its MCP server."""

    async def tool(arguments: dict[str, Any]) -> str:
        state = (await servicer.db_client.get_state(trial_id)).data
        result = _TOOLS[name].invoke(data=state, **arguments)
        await write_state(servicer, trial_id, state)
        return json.dumps(result)

    return tool


def _runner_grade(servicer: Any, context: Any, scenario: _Scenario) -> pb2.GradeTrialResponse:
    trial_id = f"comparison_view_example_{scenario.name}:0"
    description = _adapter().to_task_description(_TASK_ID)
    register(servicer, context, description.model_dump(mode="json"), trial_id)
    trial = servicer.trials[trial_id]
    for name in list(trial.agent_tools):
        trial.agent_tools[name] = _in_process_tool(servicer, trial_id, name)
    servicer._run_async(write_state(servicer, trial_id, _final_state(scenario.calls)))
    return servicer.GradeTrial(
        pb2.GradeTrialRequest(trial_id=trial_id, llm_messages_json=TRANSCRIPT), context
    )


def _core_grade(scenario: _Scenario):
    adapter = _adapter()
    task = adapter.get_task(_TASK_ID)
    return GradingEngine(
        adapter.get_grading_config(_TASK_ID),
        task_domain="document_desk",
        task_dir=adapter.get_task_dir(_TASK_ID),
        task_initial_state=task.initial_state,
        task_mcp_server=task.tools.agent["mcp_server"],
    ).grade_trajectory(
        Trajectory(
            task_id=_TASK_ID,
            trial_index=0,
            start_ts="2026-01-01T00:00:00Z",
            end_ts="2026-01-01T00:00:00Z",
            messages=[],
        ),
        {"db": _final_state(scenario.calls)},
    )


@pytest.mark.parametrize("scenario", _SCENARIOS, ids=[s.name for s in _SCENARIOS])
def test_both_substrates_grade_the_scenario_through_the_packs_view(
    scenario: _Scenario, runner_service, mock_grpc_context
) -> None:
    response = _runner_grade(runner_service, mock_grpc_context, scenario)
    assert response.success is True, response.error
    assert "GOLDEN REPLAY ERRORS" not in response.grade.reasons, response.grade.reasons
    runner_score = response.grade.components.state_checks
    assert runner_score == pytest.approx(scenario.state_checks), response.grade.reasons
    runner_record = ComparisonViewGradeRecord.model_validate_json(
        response.grade.comparison_view_json
    )
    scenario.check(runner_record)

    grade = _core_grade(scenario)
    assert grade.components.state_checks == pytest.approx(scenario.state_checks), grade.reasons
    same = grade.comparison_view == runner_record.model_dump(mode="json")
    assert same, "the two substrates recorded different views of the same trial"


def test_without_the_view_the_acceptable_alternative_fails() -> None:
    """The control: what the view sees past is a different state to the bare hash."""
    adapter = _adapter()
    task = adapter.get_task(_TASK_ID)
    config = adapter.get_grading_config(_TASK_ID)
    config.state_checks.comparison_view = None
    alternative = next(s for s in _SCENARIOS if s.name == "an_acceptable_alternative")
    grade = GradingEngine(
        config,
        task_domain="document_desk",
        task_dir=adapter.get_task_dir(_TASK_ID),
        task_initial_state=task.initial_state,
        task_mcp_server=task.tools.agent["mcp_server"],
    ).grade_trajectory(
        Trajectory(
            task_id=_TASK_ID,
            trial_index=0,
            start_ts="2026-01-01T00:00:00Z",
            end_ts="2026-01-01T00:00:00Z",
            messages=[],
        ),
        {"db": _final_state(alternative.calls)},
    )
    assert grade.components.state_checks == 0.0
    assert grade.comparison_view is None


def test_the_pack_uses_every_rule_kind_and_validates_strictly() -> None:
    view = _adapter().get_grading_config(_TASK_ID).state_checks.comparison_view
    assert view is not None
    assert {rule.kind for rule in view.rules} == {
        "exclude_records",
        "exclude_tables",
        "normalize_ids",
    }
    result = CliRunner().invoke(
        cli,
        [
            "validate",
            "--tasks",
            str(_PROJECT / "dataset" / "**" / "task.yaml"),
            "--strict-authoring",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "1 valid, 0 invalid" in result.output
    assert "⚠" not in result.output and "not checked" not in result.output


def test_the_scripted_final_states_differ_from_the_golden_one() -> None:
    """Every scenario but the golden path is a different raw state, so the view decides."""
    golden = _final_state(_SCENARIOS[0].calls)
    for scenario in _SCENARIOS[1:]:
        assert _strip_clock(_final_state(scenario.calls)) != _strip_clock(golden), scenario.name


def _strip_clock(state: dict[str, Any]) -> dict[str, Any]:
    stripped = copy.deepcopy(state)
    for rows in stripped.values():
        for row in rows:
            row.pop("created_at", None)
            row.pop("at", None)
    return stripped
