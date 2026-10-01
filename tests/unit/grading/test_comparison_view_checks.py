"""A declared comparison view is checked against its task wherever the task loads.

One function decides (:func:`comparison_view_findings`); the native adapter's two loads,
the runner's ``RegisterTrial`` and the authoring gate each call it, so a view one of
them refuses is refused by all of them.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner
from pydantic import ValidationError

from tests.utils.runner_requests import register_request, trial_spec_json
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.grading.comparison_view import ComparisonViewConfig
from tolokaforge.core.grading.comparison_view_checks import (
    check_comparison_view,
    comparison_view_findings,
)
from tolokaforge.core.grading.config_validation import (
    SeededTablesLayer,
    SkipKind,
    ToolInventory,
    inspect_grading_authoring,
)
from tolokaforge.dx.cli.main import cli
from tolokaforge.runner import models as runner_models

pytestmark = pytest.mark.unit

_TABLES: dict[str, Any] = {
    "documents": [{"id": "D1", "client_id": "C1", "source_id": "S1"}],
    "corrections": [],
    "holds": [],
    "lookup_log": [],
}

_NORMALIZE = {
    "kind": "normalize_ids",
    "table": "documents",
    "key": ["client_id", "source_id"],
    "references": [{"table": "corrections", "field": "document_ref"}],
}
_RANKED = {
    "kind": "normalize_ids",
    "table": "documents",
    "ordinal_by": ["client_id"],
    "rank_by": ["filed_at"],
    "references": [{"table": "corrections", "field": "document_ref"}],
}
_RELEASED = {
    "kind": "exclude_records",
    "table": "holds",
    "where": {"status": "released"},
    "unless_referenced_by": [{"table": "corrections", "field": "hold_ref"}],
}
_LOOKUPS = {"kind": "exclude_tables", "tables": ["lookup_log"], "reason": "read tools write it"}


def _view(*rules: dict[str, Any]) -> ComparisonViewConfig:
    return ComparisonViewConfig.model_validate({"version": 1, "rules": list(rules)})


def _findings(*rules: dict[str, Any], **task: Any):
    task.setdefault("tables", _TABLES)
    task.setdefault("id_fields", {})
    return comparison_view_findings(_view(*rules), **task)


def _only_error(*rules: dict[str, Any], **task: Any) -> str:
    findings = _findings(*rules, **task)
    assert len(findings.errors) == 1, findings.errors
    return findings.errors[0]


def test_a_view_that_fits_its_task_is_accepted() -> None:
    findings = _findings(
        _LOOKUPS,
        _RELEASED,
        _NORMALIZE,
        unstable_fields=["documents.id", "documents.created_at", "corrections.id"],
        auto_mask_clock_columns=True,
        numeric_string_fields=["amount"],
    )
    assert findings.errors == ()
    assert findings.warnings == ()


# ---------------------------------------------------------------------------
# Tables and schema
# ---------------------------------------------------------------------------


def test_a_table_the_initial_state_does_not_seed_is_refused_naming_rule_and_table() -> None:
    error = _only_error({**_LOOKUPS, "tables": ["tool_log"]})
    assert "state_checks.comparison_view.rules[0] (exclude_tables)" in error
    assert "names table 'tool_log', which the initial state does not seed" in error
    assert "relaxed_validation" in error


def test_a_referencing_table_counts_as_a_named_table() -> None:
    error = _only_error({**_NORMALIZE, "references": [{"table": "notes", "field": "doc"}]})
    assert "names table 'notes'" in error


def test_relaxed_validation_downgrades_a_missing_table_to_a_warning() -> None:
    findings = _findings({**_LOOKUPS, "tables": ["tool_log"]}, relaxed_validation=True)
    assert findings.errors == ()
    (warning,) = findings.warnings
    assert "names table 'tool_log'" in warning and "relaxed_validation downgrades" in warning


def test_fields_are_checked_against_a_declared_schema_only() -> None:
    rule = {**_RELEASED, "where": {"state": "released"}}
    assert _findings(rule).errors == ()
    error = _only_error(
        rule,
        schemas={"holds": ("id", "status"), "corrections": ("id", "hold_ref")},
    )
    assert "reads field(s) ['state'] of table 'holds'" in error


def test_a_nested_path_is_checked_at_its_first_segment() -> None:
    rule = {
        "kind": "exclude_records",
        "table": "holds",
        "path": "allocations",
        "where": {"all_zero": ["amount"]},
    }
    assert _findings(rule, schemas={"holds": ("id", "allocations")}).errors == ()
    error = _only_error(rule, schemas={"holds": ("id",)})
    assert "reads field(s) ['allocations']" in error


# ---------------------------------------------------------------------------
# The id-field checks the rules make when they apply
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rule", "id_fields", "fragment"),
    [
        (_NORMALIZE, {"documents": ["client_id", "source_id"]}, "composite key"),
        (_NORMALIZE, {"documents": "source_id"}, "the id field it replaces"),
        (
            {**_NORMALIZE, "references": [{"table": "documents", "field": "id"}]},
            {},
            "a record's own id is not a reference to it",
        ),
        (_RELEASED, {"holds": ["id", "kind"]}, "composite key"),
    ],
)
def test_an_id_field_conflict_is_refused_at_load(
    rule: dict[str, Any], id_fields: dict[str, Any], fragment: str
) -> None:
    error = _only_error(rule, id_fields=id_fields)
    assert error.startswith(f"state_checks.comparison_view.rules[0] ({rule['kind']}): ")
    assert fragment in error


# ---------------------------------------------------------------------------
# What normalize_ids keys a record by, and what it rewrites
# ---------------------------------------------------------------------------


def test_a_key_field_the_unstable_filter_drops_is_refused() -> None:
    error = _only_error(_RANKED, unstable_fields=["documents.filed_at"])
    assert "builds its key from documents.filed_at, which unstable_fields masks" in error


def test_a_key_field_is_masked_by_the_unstable_table_name_the_db_service_resolves() -> None:
    """``document.filed_at`` names ``documents``, as the db-service resolves it."""
    error = _only_error(_RANKED, unstable_fields=["document.filed_at"])
    assert "documents.filed_at, which unstable_fields masks" in error


def test_a_key_field_the_clock_mask_drops_is_refused_only_while_the_mask_is_on() -> None:
    rule = {**_RANKED, "rank_by": ["updated_at"]}
    assert _findings(rule).errors == ()
    error = _only_error(rule, auto_mask_clock_columns=True)
    assert "documents.updated_at, a column auto_mask_clock_columns drops" in error


def test_a_key_field_numeric_string_fields_folds_is_refused() -> None:
    error = _only_error(
        {**_NORMALIZE, "key": ["client_id", "amount"]}, numeric_string_fields=["amount"]
    )
    assert "documents.amount, which numeric_string_fields folds" in error


def test_a_reference_the_unstable_filter_drops_is_refused() -> None:
    error = _only_error(_NORMALIZE, unstable_fields=["corrections.document_ref"])
    assert "rewrites the reference corrections.document_ref, and unstable_fields masks" in error


def test_a_nested_reference_is_masked_by_its_top_level_column() -> None:
    rule = {**_NORMALIZE, "references": [{"table": "corrections", "field": "updates.doc"}]}
    error = _only_error(rule, unstable_fields=["corrections.updates"])
    assert "rewrites the reference corrections.updates.doc" in error


def test_a_reference_the_clock_mask_drops_is_refused() -> None:
    rule = {**_NORMALIZE, "references": [{"table": "corrections", "field": "modified_at"}]}
    error = _only_error(rule, auto_mask_clock_columns=True)
    assert "auto_mask_clock_columns drops corrections.modified_at" in error


def test_the_re_keyed_id_field_may_be_declared_unstable() -> None:
    """The filter after the view leaves a re-keyed id in, so its mask is no conflict."""
    assert _findings(_NORMALIZE, unstable_fields=["documents.id"]).errors == ()


def test_a_re_keyed_id_field_the_clock_mask_drops_is_refused() -> None:
    error = _only_error(
        _NORMALIZE, id_fields={"documents": "modified_at"}, auto_mask_clock_columns=True
    )
    assert error.startswith(
        "state_checks.comparison_view.rules[0] (normalize_ids) re-keys documents.modified_at, "
        "a column auto_mask_clock_columns drops"
    )


# ---------------------------------------------------------------------------
# A view nothing reads
# ---------------------------------------------------------------------------


def test_a_view_beside_a_disabled_hash_is_a_warning_not_a_refusal() -> None:
    findings = _findings(_LOOKUPS, hash_enabled=False)
    assert findings.errors == ()
    (warning,) = findings.warnings
    assert "state_checks.hash is not enabled" in warning


def test_the_gate_names_the_task_and_logs_its_warnings(caplog: pytest.LogCaptureFixture) -> None:
    view = _view({**_LOOKUPS, "tables": ["tool_log"]}, _NORMALIZE)
    with caplog.at_level(logging.WARNING):
        message = check_comparison_view(
            view,
            context="task_x",
            tables=_TABLES,
            id_fields={"documents": "source_id"},
            hash_enabled=False,
        )
    assert message is not None and message.startswith("[task_x] ")
    assert "rules[0] (exclude_tables)" in message and "rules[1] (normalize_ids)" in message
    assert "[task_x]" in caplog.text and "state_checks.hash is not enabled" in caplog.text
    assert check_comparison_view(_view(_LOOKUPS), context="t", tables=_TABLES, id_fields={}) is None


# ---------------------------------------------------------------------------
# Every load calls it
# ---------------------------------------------------------------------------

_MASKED_KEY_VIEW = {"version": 1, "rules": [_RANKED]}


_FILED_AT_UNSTABLE = [{"table_name": "document", "field_name": "filed_at", "reason": "timestamp"}]


def _write_pack(
    root: Path,
    state_checks: dict[str, Any],
    *,
    tables: dict[str, Any] = _TABLES,
    unstable: list[dict[str, Any]] = _FILED_AT_UNSTABLE,
) -> NativeAdapter:
    task_dir = root / "tasks" / "view_task"
    (task_dir / "fixtures").mkdir(parents=True)
    (task_dir / "fixtures" / "unstable_fields.json").write_text(json.dumps(unstable))
    (task_dir / "initial_state.json").write_text(json.dumps(tables))
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": "view_task",
                "name": "A task whose view keys a record by a masked column",
                "category": "test",
                "description": "Every load refuses the view.",
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


def _validate(root: Path, *flags: str):
    return CliRunner().invoke(
        cli, ["validate", "--tasks", str(root / "tasks" / "**" / "task.yaml"), *flags]
    )


_STATE_CHECKS = {
    "hash": {"enabled": True, "expect_initial_state": True},
    "comparison_view": _MASKED_KEY_VIEW,
}


def test_the_native_adapter_refuses_the_view_on_both_loads(tmp_path: Path) -> None:
    adapter = _write_pack(tmp_path, _STATE_CHECKS)
    with pytest.raises(ValueError, match=r"\[view_task\] .*documents\.filed_at"):
        adapter.to_task_description("view_task")
    with pytest.raises(ValueError, match=r"\[view_task\] .*documents\.filed_at"):
        adapter.get_grading_config("view_task")


def test_register_trial_refuses_the_view(runner_service, mock_grpc_context) -> None:
    """The belt-and-suspenders for a description some other adapter built."""
    description = runner_models.TaskDescription.model_validate(
        {
            "task_id": "view_task",
            "name": "A view keyed by a masked column",
            "category": "test",
            "description": "RegisterTrial refuses it.",
            "adapter_type": "native",
            "system_prompt": "You are a test assistant.",
            "initial_state": {
                "tables": _TABLES,
                "unstable_fields": [{"table_name": "documents", "field_name": "filed_at"}],
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
                    "comparison_view": _MASKED_KEY_VIEW,
                },
            },
        }
    )
    trial_id = "view_task:0"
    response = runner_service.RegisterTrial(
        register_request(
            trial_spec_json(description.model_dump(mode="json"), trial_id=trial_id),
            trial_id=trial_id,
        ),
        mock_grpc_context,
    )
    assert response.success is False
    assert "[RegisterTrial: view_task:0]" in response.error
    assert "documents.filed_at, which unstable_fields masks" in response.error


def test_the_authoring_gate_refuses_the_view_and_hints_a_disabled_hash() -> None:
    layer = SeededTablesLayer(tables=_TABLES, unstable_fields=lambda: ("document.filed_at",))
    report = inspect_grading_authoring(
        {"state_checks": _STATE_CHECKS}, ToolInventory.unresolvable(), seeded_tables=layer
    )
    (error,) = [f for f in report.errors if f.where == "state_checks.comparison_view"]
    assert "documents.filed_at, which unstable_fields masks" in error.message

    disabled = {"hash": {"enabled": False}, "comparison_view": {"version": 1, "rules": [_LOOKUPS]}}
    report = inspect_grading_authoring(
        {"state_checks": disabled}, ToolInventory.unresolvable(), seeded_tables=layer
    )
    assert not [f for f in report.errors if f.where == "state_checks.comparison_view"]
    (hint,) = [f for f in report.hints if f.where == "state_checks.comparison_view"]
    assert "state_checks.hash is not enabled" in hint.message


def test_the_authoring_gate_skips_a_view_against_tables_nobody_resolved() -> None:
    report = inspect_grading_authoring(
        {"state_checks": _STATE_CHECKS},
        ToolInventory.unresolvable(),
        seeded_tables=SeededTablesLayer.unresolvable(),
    )
    (skip,) = [s for s in report.unchecked if s.where == "state_checks.comparison_view"]
    assert skip.kind is SkipKind.ADAPTER_DECLARED


def test_the_native_layer_reports_the_unstable_fields_the_run_path_reads(tmp_path: Path) -> None:
    adapter = _write_pack(tmp_path, {"hash": {"enabled": True, "expect_initial_state": True}})
    task = adapter.get_task("view_task")
    layer = NativeAdapter.grading_seeded_tables(task, adapter.get_task_dir("view_task"))
    assert layer.unstable_fields() == ("document.filed_at",)
    assert set(layer.tables) == set(_TABLES)


# ---------------------------------------------------------------------------
# A table seeded as a mapping is refused on every load path
# ---------------------------------------------------------------------------

_KEYED_DOCUMENTS = {
    **_TABLES,
    "documents": {"D1": {"id": "D1", "client_id": "C1", "source_id": "S1"}},
}
_VIEW_OF_THE_DOCUMENTS = {
    "hash": {"enabled": True, "expect_initial_state": True},
    "comparison_view": {"version": 1, "rules": [_NORMALIZE]},
}
_MAPPING_REFUSAL = (
    "rules[0] (normalize_ids) names table 'documents', which the initial state seeds as a "
    "mapping of records keyed by id, not a list of records"
)


def test_a_view_naming_a_table_seeded_as_a_mapping_is_refused_by_the_native_loads(
    tmp_path: Path,
) -> None:
    adapter = _write_pack(tmp_path, _VIEW_OF_THE_DOCUMENTS, tables=_KEYED_DOCUMENTS, unstable=[])
    with pytest.raises(
        ValueError, match=re.escape(f"[view_task] state_checks.comparison_view.{_MAPPING_REFUSAL}")
    ):
        adapter.to_task_description("view_task")
    with pytest.raises(ValueError, match=re.escape(_MAPPING_REFUSAL)):
        adapter.get_grading_config("view_task")


def test_a_view_naming_a_table_seeded_as_a_mapping_is_refused_by_the_authoring_gate(
    tmp_path: Path,
) -> None:
    adapter = _write_pack(tmp_path, _VIEW_OF_THE_DOCUMENTS, tables=_KEYED_DOCUMENTS, unstable=[])
    layer = NativeAdapter.grading_seeded_tables(
        adapter.get_task("view_task"), adapter.get_task_dir("view_task")
    )
    assert layer.table_shapes == {"documents": "a mapping of records keyed by id"}
    report = inspect_grading_authoring(
        {"state_checks": _VIEW_OF_THE_DOCUMENTS}, ToolInventory.unresolvable(), seeded_tables=layer
    )
    assert [f.message for f in report.errors if f.where == "state_checks.comparison_view"] == [
        f"state_checks.comparison_view.{_MAPPING_REFUSAL}. The runner reads the table as the "
        "list of its records and core's hash as written, so the view would grade one trial "
        "two ways: seed 'documents' as a list of records"
    ]
    result = _validate(tmp_path)
    assert result.exit_code != 0 and _MAPPING_REFUSAL in result.output


def test_register_trial_cannot_be_handed_a_table_seeded_as_a_mapping() -> None:
    """The wire's tables are lists by type, so a description carrying one never registers."""
    with pytest.raises(ValidationError, match="tables.documents\\n  Input should be a valid list"):
        runner_models.RunnerInitialStateConfig(tables=_KEYED_DOCUMENTS)


def test_a_mapping_table_no_rule_names_is_no_concern_of_the_view(tmp_path: Path) -> None:
    tables = {**_TABLES, "settings": {"currency": "EUR"}}
    adapter = _write_pack(
        tmp_path,
        {**_VIEW_OF_THE_DOCUMENTS, "comparison_view": {"version": 1, "rules": [_NORMALIZE]}},
        tables=tables,
        unstable=[],
    )
    adapter.to_task_description("view_task")
    adapter.get_grading_config("view_task")


def test_a_table_value_that_is_not_a_list_is_refused_whoever_reads_it() -> None:
    """A plugin adapter handing its tables as declared is held to the same rule."""
    error = _only_error(_NORMALIZE, tables={**_TABLES, "documents": {"D1": {"id": "D1"}}})
    assert "names table 'documents', which the initial state seeds as a dict" in error


# ---------------------------------------------------------------------------
# Seeded records are not a schema
# ---------------------------------------------------------------------------

#: Every table a rule reads is seeded with rows, and no seeded row carries the fields
#: the rules read: agents write fields no seeded record carries, so none of them is
#: a reason to refuse the view on any path.
_ROWS_WITHOUT_THE_FIELDS: dict[str, Any] = {
    "documents": [{"id": "D1", "client_id": "C1"}],
    "corrections": [{"id": "R0", "reason": "seeded"}],
    "holds": [{"id": "H0", "client_id": "C1"}],
    "lookup_log": [{"id": "L0"}],
}
_VIEW_READING_UNSEEDED_FIELDS = {
    "hash": {"enabled": True, "expect_initial_state": True},
    "comparison_view": {
        "version": 1,
        "rules": [
            {**_RELEASED, "where": {"status": "released", "reason_code": {"in": ["x"]}}},
            _NORMALIZE,
            _LOOKUPS,
        ],
    },
}


def test_fields_no_seeded_row_carries_load_on_every_path(
    tmp_path: Path, runner_service, mock_grpc_context
) -> None:
    adapter = _write_pack(
        tmp_path, _VIEW_READING_UNSEEDED_FIELDS, tables=_ROWS_WITHOUT_THE_FIELDS, unstable=[]
    )
    description = adapter.to_task_description("view_task")
    assert description.grading.state_checks.comparison_view is not None
    assert adapter.get_grading_config("view_task").state_checks.comparison_view is not None

    trial_id = "seeded_rows_are_no_schema:0"
    registered = runner_service.RegisterTrial(
        register_request(
            trial_spec_json(description.model_dump(mode="json"), trial_id=trial_id),
            trial_id=trial_id,
        ),
        mock_grpc_context,
    )
    assert registered.success is True, registered.error

    layer = NativeAdapter.grading_seeded_tables(
        adapter.get_task("view_task"), adapter.get_task_dir("view_task")
    )
    report = inspect_grading_authoring(
        {"state_checks": _VIEW_READING_UNSEEDED_FIELDS},
        ToolInventory.unresolvable(),
        seeded_tables=layer,
    )
    assert not [f for f in report.errors if f.where == "state_checks.comparison_view"]
    result = _validate(tmp_path, "--strict-authoring")
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# What the gate is not told, it does not assume
# ---------------------------------------------------------------------------


def test_unreported_unstable_fields_leave_the_masked_field_checks_unchecked() -> None:
    findings = _findings(_RANKED, unstable_fields=None, numeric_string_fields=["filed_at"])
    assert findings.unchecked and "reports no unstable fields" in findings.unchecked[0]
    (error,) = findings.errors
    assert "numeric_string_fields folds" in error, "the checks not reading them still run"
    assert _findings(_LOOKUPS, unstable_fields=None).unchecked == (), "no key, nothing to mask"


def test_a_layer_reporting_only_tables_reports_the_masked_field_checks_unchecked() -> None:
    """A plugin adapter's layer naming no unstable fields cannot pass a masked key."""
    report = inspect_grading_authoring(
        {"state_checks": _STATE_CHECKS},
        ToolInventory.unresolvable(),
        seeded_tables=SeededTablesLayer(tables=_TABLES),
    )
    assert not [f for f in report.errors if f.where == "state_checks.comparison_view"]
    (skip,) = [s for s in report.unchecked if s.where == "state_checks.comparison_view"]
    assert skip.kind is SkipKind.ADAPTER_DECLARED
    assert "reports no unstable fields" in skip.reason

    told = SeededTablesLayer(tables=_TABLES, unstable_fields=lambda: ("document.filed_at",))
    report = inspect_grading_authoring(
        {"state_checks": _STATE_CHECKS}, ToolInventory.unresolvable(), seeded_tables=told
    )
    assert not [s for s in report.unchecked if s.where == "state_checks.comparison_view"]


def test_the_gate_reports_a_view_its_model_refuses_as_a_finding() -> None:
    """The gate is called on raw blocks too (replay, migration); it reports, never raises."""
    report = inspect_grading_authoring(
        {
            "state_checks": {
                "hash": {"enabled": True, "expect_initial_state": True},
                "comparison_view": {"version": 9, "rules": [_LOOKUPS]},
            }
        },
        ToolInventory.unresolvable(),
        seeded_tables=SeededTablesLayer(tables=_TABLES, unstable_fields=tuple),
    )
    (error,) = [f for f in report.errors if f.where == "state_checks.comparison_view"]
    assert "is not a view this engine reads" in error.message
    assert "comparison_view.version 9 is not a version this engine reads" in error.message
