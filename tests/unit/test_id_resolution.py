"""Unit tests for :mod:`tolokaforge.runner.id_resolution`.

The shared helper backs three diff-sync paths (DB proxy for tools, Tau
in-memory sync, MCP subprocess sync). These tests exercise it directly so
failures point at the helper rather than at a caller's plumbing.
"""

import logging

import pytest

from tolokaforge.runner.id_resolution import (
    IdFieldResolutionError,
    TableKey,
    check_id_fields_reference_known_tables,
    compute_diff_ops,
    resolve_record_id,
    table_key,
)

pytestmark = pytest.mark.unit


def test_table_key_configured_wins():
    assert table_key("widgets", {"widgets": "widget_id"}) == TableKey(("widget_id",))


def test_table_key_defaults_to_id():
    assert table_key("users", {}) == TableKey(("id",))
    assert table_key("users", {"widgets": "widget_id"}) == TableKey(("id",))


def test_table_key_blank_or_empty_configured_falls_through_to_id():
    # Truthiness fallthrough (``or``-style, not ``.get(..., default)``) so a
    # blank string and an empty list fold to the default, matching the DB
    # proxy's resolver.
    assert table_key("widgets", {"widgets": ""}) == TableKey(("id",))
    assert table_key("widgets", {"widgets": []}) == TableKey(("id",))


def test_table_key_composite_preserves_declared_order():
    key = table_key("positions", {"positions": ["account_id", "symbol"]})
    assert key == TableKey(("account_id", "symbol"))


def test_table_key_one_element_list_equals_string_form():
    assert table_key("widgets", {"widgets": ["widget_id"]}) == table_key(
        "widgets", {"widgets": "widget_id"}
    )


def test_table_key_blank_component_raises_naming_table():
    with pytest.raises(IdFieldResolutionError) as ei:
        table_key("positions", {"positions": ["account_id", ""]})
    assert "positions" in str(ei.value)


def test_table_key_non_string_component_raises():
    with pytest.raises(IdFieldResolutionError):
        table_key("positions", {"positions": [1]})


def test_table_key_unsupported_declaration_shape_raises():
    with pytest.raises(IdFieldResolutionError) as ei:
        table_key("positions", {"positions": 7})
    assert "positions" in str(ei.value)


def test_table_key_requires_at_least_one_field():
    with pytest.raises(IdFieldResolutionError):
        TableKey(())


def test_resolve_record_id_configured_key():
    value = resolve_record_id(
        {"widget_id": "W1", "status": "new"},
        "widgets",
        {"widgets": "widget_id"},
    )
    assert value == ("W1",)


def test_resolve_record_id_defaults_to_id_field():
    assert resolve_record_id({"id": "X1", "name": "a"}, "items", {}) == ("X1",)


def test_resolve_record_id_composite_returns_component_tuple():
    value = resolve_record_id(
        {"account_id": "A1", "symbol": "MSFT", "qty": 5},
        "positions",
        {"positions": ["account_id", "symbol"]},
    )
    assert value == ("A1", "MSFT")


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


def test_resolve_record_id_composite_missing_component_raises():
    with pytest.raises(IdFieldResolutionError) as ei:
        resolve_record_id(
            {"account_id": "A1", "qty": 5},
            "positions",
            {"positions": ["account_id", "symbol"]},
        )
    msg = str(ei.value)
    assert "'symbol'" in msg  # the missing component
    assert "['account_id', 'symbol']" in msg  # the full declared key
    assert "positions" in msg
    assert "account_id" in msg  # record keys surfaced for debugging


def test_resolve_record_id_falsy_value_returned_not_raised():
    # Key present with a falsy value (0, "", False) must return the value,
    # not treat the record as keyless.
    assert resolve_record_id({"id": 0, "name": "a"}, "items", {}) == (0,)
    assert resolve_record_id({"id": "", "name": "a"}, "items", {}) == ("",)


def test_resolve_record_id_unhashable_component_raises_contract_error():
    # A list-valued key component must raise the module's contract error, not
    # surface as a bare TypeError from dict insertion two frames later.
    with pytest.raises(IdFieldResolutionError) as ei:
        resolve_record_id({"id": ["not", "hashable"]}, "items", {})
    msg = str(ei.value)
    assert "items" in msg
    assert "'id'" in msg


# ---------------------------------------------------------------------------
# check_id_fields_reference_known_tables — shared by NativeAdapter and the
# runner's RegisterTrial belt-and-suspenders block.
# ---------------------------------------------------------------------------


_SEEDED_WIDGETS = {"widgets": [{"widget_id": "W1", "status": "new"}]}


def test_check_returns_none_when_id_fields_empty():
    assert (
        check_id_fields_reference_known_tables({}, _SEEDED_WIDGETS, context="t", relaxed=False)
        is None
    )


def test_check_returns_none_when_all_tables_known():
    assert (
        check_id_fields_reference_known_tables(
            {"widgets": "widget_id"},
            {**_SEEDED_WIDGETS, "users": [{"id": "U1"}]},
            context="t",
            relaxed=False,
        )
        is None
    )


def test_check_returns_error_string_on_unknown_table():
    err = check_id_fields_reference_known_tables(
        {"widgetz": "widget_id"}, _SEEDED_WIDGETS, context="task_x", relaxed=False
    )
    assert err is not None
    assert "task_x" in err  # context prefix included
    assert "widgetz" in err  # unknown key surfaced
    assert "widgets" in err  # known tables listed
    assert "relaxed_validation" in err  # escape-hatch hint


def test_check_returns_none_when_relaxed_but_logs_warning(caplog):
    with caplog.at_level(logging.WARNING):
        err = check_id_fields_reference_known_tables(
            {"widgetz": "widget_id"}, _SEEDED_WIDGETS, context="task_x", relaxed=True
        )
    assert err is None
    warns = [rec for rec in caplog.records if rec.levelno >= logging.WARNING]
    assert any("widgetz" in rec.getMessage() for rec in warns)


def test_check_message_shape_stable_across_call_sites():
    # NativeAdapter and RunnerServiceImpl.RegisterTrial both call this helper;
    # the message must remain actionable regardless of the `context` prefix.
    for context in ("widgets_task", "RegisterTrial: trial-42"):
        err = check_id_fields_reference_known_tables(
            {"unknown_tbl": "id"}, _SEEDED_WIDGETS, context=context, relaxed=False
        )
        assert err is not None
        assert context in err
        assert "Fix a typo" in err


def test_check_component_present_in_any_seeded_record_passes():
    # The component check reads the union of the seeded records' keys, so a
    # field carried by only some records (sparse seeding) is still a legal key
    # component at the gate; a record actually missing it fails loud at
    # resolve_record_id time instead.
    err = check_id_fields_reference_known_tables(
        {"positions": ["account_id", "symbol"]},
        {"positions": [{"account_id": "A1"}, {"account_id": "A2", "symbol": "MSFT"}]},
        context="t",
        relaxed=False,
    )
    assert err is None


def test_check_reports_unknown_table_and_absent_component_in_one_message():
    err = check_id_fields_reference_known_tables(
        {"widgetz": "widget_id", "positions": ["account_id", "ticker"]},
        {**_SEEDED_WIDGETS, "positions": [{"account_id": "A1", "symbol": "AAPL"}]},
        context="task_x",
        relaxed=False,
    )
    assert err is not None
    assert "widgetz" in err  # finding 1: unknown table
    assert "ticker" in err  # finding 2: absent component
    assert "symbol" in err  # the fields the seeded records do carry


# ---------------------------------------------------------------------------
# compute_diff_ops — shared by TauSyncToolWrapper and _sync_mcp_state_to_db.
# ---------------------------------------------------------------------------


def _positions_fixture() -> tuple[list[dict], list[dict]]:
    """Three seeded rows where ``account_id`` alone collides — the #919 reproduction."""
    before = [
        {"account_id": "A1", "symbol": "MSFT", "qty": 5},
        {"account_id": "A1", "symbol": "AAPL", "qty": 10},
        {"account_id": "B2", "symbol": "MSFT", "qty": 7},
    ]
    after = [
        {"account_id": "A1", "symbol": "MSFT", "qty": 99},
        {"account_id": "A1", "symbol": "AAPL", "qty": 10},
        {"account_id": "B2", "symbol": "MSFT", "qty": 7},
    ]
    return before, after


def test_compute_diff_ops_emits_inserts_then_upserts_then_deletes():
    # Ordering matters for downstream consumers: pre-refactor call sites emitted
    # [all inserts, all upserts, all deletes]. This locks the batch shape.
    before = [{"id": "A", "v": 1}, {"id": "B", "v": 2}]
    after = [{"id": "A", "v": 1}, {"id": "B", "v": 99}, {"id": "C", "v": 3}]
    ops = compute_diff_ops(before, after, "items", {})
    op_order = [op["op"] for op in ops]
    assert op_order == ["insert", "upsert"]  # A unchanged, B changed, C new


def test_compute_diff_ops_single_key_upsert_key_stays_string():
    # Drift lock: the wire shape every single-key pack ships today — ``key``
    # is a plain string, never a one-element list.
    ops = compute_diff_ops(
        [{"id": "A", "v": 1}],
        [{"id": "A", "v": 2}],
        "items",
        {},
    )
    assert ops == [{"op": "upsert", "record": {"id": "A", "v": 2}, "key": "id"}]


def test_compute_diff_ops_delete_uses_resolved_key():
    ops = compute_diff_ops(
        [{"lot_id": "L1"}, {"lot_id": "L2"}],
        [{"lot_id": "L1"}],
        "lots",
        {"lots": "lot_id"},
    )
    deletes = [op for op in ops if op["op"] == "delete"]
    assert deletes == [{"op": "delete", "filter": {"lot_id": "L2"}}]


def test_compute_diff_ops_composite_upsert_carries_component_list():
    before, after = _positions_fixture()
    ops = compute_diff_ops(before, after, "positions", {"positions": ["account_id", "symbol"]})
    assert ops == [
        {
            "op": "upsert",
            "record": {"account_id": "A1", "symbol": "MSFT", "qty": 99},
            "key": ["account_id", "symbol"],
        }
    ]


def test_compute_diff_ops_single_field_over_composite_rows_raises_duplicate():
    # The same fixture keyed on ``account_id`` alone collides — the silent
    # corruption #919 reproduced stays a loud error on this path.
    before, after = _positions_fixture()
    with pytest.raises(IdFieldResolutionError) as ei:
        compute_diff_ops(before, after, "positions", {"positions": "account_id"})
    assert "Duplicate" in str(ei.value)
    assert "positions" in str(ei.value)


def test_compute_diff_ops_composite_delete_filter_carries_every_component():
    before, _ = _positions_fixture()
    after = [r for r in before if (r["account_id"], r["symbol"]) != ("A1", "MSFT")]
    ops = compute_diff_ops(before, after, "positions", {"positions": ["account_id", "symbol"]})
    assert ops == [{"op": "delete", "filter": {"account_id": "A1", "symbol": "MSFT"}}]


def test_compute_diff_ops_composite_rows_sharing_one_component_index_distinctly():
    after = [
        {"account_id": "A1", "symbol": "MSFT", "qty": 1},
        {"account_id": "A1", "symbol": "AAPL", "qty": 2},
    ]
    ops = compute_diff_ops([], after, "positions", {"positions": ["account_id", "symbol"]})
    assert [op["op"] for op in ops] == ["insert", "insert"]


def test_compute_diff_ops_composite_full_value_collision_raises_quoting_components():
    rows = [
        {"account_id": "A1", "symbol": "MSFT", "qty": 1},
        {"account_id": "A1", "symbol": "MSFT", "qty": 2},
    ]
    with pytest.raises(IdFieldResolutionError) as ei:
        compute_diff_ops(rows, [], "positions", {"positions": ["account_id", "symbol"]})
    msg = str(ei.value)
    assert "Duplicate" in msg
    assert "account_id" in msg and "A1" in msg
    assert "symbol" in msg and "MSFT" in msg


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
