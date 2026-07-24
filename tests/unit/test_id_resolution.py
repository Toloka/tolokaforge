"""Unit tests for :mod:`tolokaforge.runner.id_resolution`.

The shared helper backs three diff-sync paths (DB proxy for tools, Tau
in-memory sync, MCP subprocess sync). These tests exercise it directly so
failures point at the helper rather than at a caller's plumbing.
"""

import logging

import pytest

from tolokaforge.runner.id_resolution import (
    IdFieldResolutionError,
    check_id_fields_reference_known_tables,
    compute_diff_ops,
    id_field_for_table,
    resolve_record_id,
)

pytestmark = pytest.mark.unit


def test_id_field_for_table_configured_wins():
    assert id_field_for_table("widgets", {"widgets": "widget_id"}) == "widget_id"


def test_id_field_for_table_defaults_to_id():
    assert id_field_for_table("users", {}) == "id"
    assert id_field_for_table("users", {"widgets": "widget_id"}) == "id"


def test_id_field_for_table_blank_configured_falls_through_to_id():
    # ``or`` (not ``.get(..., "id")``) so blank config folds to the default,
    # matching the DB proxy's resolver.
    assert id_field_for_table("widgets", {"widgets": ""}) == "id"


def test_resolve_record_id_configured_key():
    value = resolve_record_id(
        {"widget_id": "W1", "status": "new"},
        "widgets",
        {"widgets": "widget_id"},
    )
    assert value == "W1"


def test_resolve_record_id_defaults_to_id_field():
    value = resolve_record_id({"id": "X1", "name": "a"}, "items", {})
    assert value == "X1"


def test_resolve_record_id_missing_key_raises():
    with pytest.raises(IdFieldResolutionError) as ei:
        resolve_record_id({"widget_id": "W1"}, "widgets", {})
    # The default "id" was resolved (no config), record doesn't have it → fail loud.
    msg = str(ei.value)
    assert "widgets" in msg
    assert "'id'" in msg
    assert "widget_id" in msg  # record keys surfaced for debugging


def test_resolve_record_id_wrong_configured_key_raises():
    with pytest.raises(IdFieldResolutionError) as ei:
        resolve_record_id({"id": "X1"}, "widgets", {"widgets": "widget_id"})
    msg = str(ei.value)
    assert "widget_id" in msg
    assert "widgets" in msg
    assert "state_checks.id_fields" in msg  # points authors at the fix


def test_resolve_record_id_falsy_value_returned_not_raised():
    # Key present with a falsy value (0, "", False) must return the value,
    # not treat the record as keyless.
    assert resolve_record_id({"id": 0, "name": "a"}, "items", {}) == 0
    assert resolve_record_id({"id": "", "name": "a"}, "items", {}) == ""


# ---------------------------------------------------------------------------
# check_id_fields_reference_known_tables — shared by NativeAdapter and the
# runner's RegisterTrial belt-and-suspenders block.
# ---------------------------------------------------------------------------


def test_check_returns_none_when_id_fields_empty():
    assert (
        check_id_fields_reference_known_tables({}, ["widgets"], context="t", relaxed=False) is None
    )


def test_check_returns_none_when_all_tables_known():
    assert (
        check_id_fields_reference_known_tables(
            {"widgets": "widget_id"}, ["widgets", "users"], context="t", relaxed=False
        )
        is None
    )


def test_check_returns_error_string_on_unknown_table():
    err = check_id_fields_reference_known_tables(
        {"widgetz": "widget_id"}, ["widgets"], context="task_x", relaxed=False
    )
    assert err is not None
    assert "task_x" in err  # context prefix included
    assert "widgetz" in err  # unknown key surfaced
    assert "widgets" in err  # known tables listed
    assert "relaxed_validation" in err  # escape-hatch hint


def test_check_returns_none_when_relaxed_but_logs_warning(caplog):
    with caplog.at_level(logging.WARNING):
        err = check_id_fields_reference_known_tables(
            {"widgetz": "widget_id"}, ["widgets"], context="task_x", relaxed=True
        )
    assert err is None
    warns = [rec for rec in caplog.records if rec.levelno >= logging.WARNING]
    assert any("widgetz" in rec.getMessage() for rec in warns)


def test_check_message_shape_stable_across_call_sites():
    # NativeAdapter and RunnerServiceImpl.RegisterTrial both call this helper;
    # the message must remain actionable regardless of the `context` prefix.
    for context in ("widgets_task", "RegisterTrial: trial-42"):
        err = check_id_fields_reference_known_tables(
            {"unknown_tbl": "id"}, ["widgets"], context=context, relaxed=False
        )
        assert err is not None
        assert context in err
        assert "Fix a typo" in err


# ---------------------------------------------------------------------------
# compute_diff_ops — shared by TauSyncToolWrapper and _sync_mcp_state_to_db.
# ---------------------------------------------------------------------------


def test_compute_diff_ops_emits_inserts_then_upserts_then_deletes():
    # Ordering matters for downstream consumers: pre-refactor call sites emitted
    # [all inserts, all upserts, all deletes]. This locks the batch shape.
    before = [{"id": "A", "v": 1}, {"id": "B", "v": 2}]
    after = [{"id": "A", "v": 1}, {"id": "B", "v": 99}, {"id": "C", "v": 3}]
    ops = compute_diff_ops(before, after, "items", {})
    op_order = [op["op"] for op in ops]
    assert op_order == ["insert", "upsert"]  # A unchanged, B changed, C new


def test_compute_diff_ops_delete_uses_resolved_key():
    ops = compute_diff_ops(
        [{"lot_id": "L1"}, {"lot_id": "L2"}],
        [{"lot_id": "L1"}],
        "lots",
        {"lots": "lot_id"},
    )
    deletes = [op for op in ops if op["op"] == "delete"]
    assert deletes == [{"op": "delete", "filter": {"lot_id": "L2"}}]


def test_compute_diff_ops_raises_on_duplicate_key():
    with pytest.raises(IdFieldResolutionError) as ei:
        compute_diff_ops(
            [{"id": "A"}, {"id": "A"}],  # two records collapse to one — silent-corruption class
            [],
            "items",
            {},
        )
    msg = str(ei.value)
    assert "Duplicate" in msg
    assert "items" in msg


def test_compute_diff_ops_empty_both_sides():
    assert compute_diff_ops([], [], "items", {}) == []
