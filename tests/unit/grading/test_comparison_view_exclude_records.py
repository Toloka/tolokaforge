"""What ``exclude_records`` and ``exclude_tables`` remove, and what they refuse (ADR-0053).

A missing field reads as null; ``where`` is a conjunction; ``path`` filters the items
of a nested list, traversing lists on the way element by element;
``unless_referenced_by`` keeps a matching row another table references by id.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from tolokaforge.core.grading.comparison_view import (
    ComparisonViewConfig,
    ComparisonViewError,
    ComparisonViewResult,
    RuleApplication,
    apply_comparison_view,
)

pytestmark = pytest.mark.unit


def _apply(
    state: dict[str, Any],
    *rules: dict[str, Any],
    id_fields: dict[str, str | list[str]] | None = None,
) -> ComparisonViewResult:
    view = ComparisonViewConfig.model_validate({"version": 1, "rules": list(rules)})
    return apply_comparison_view(state, initial=None, view=view, id_fields=id_fields or {})


def _exclude(table: str, where: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"kind": "exclude_records", "table": table, "where": where, **extra}


def _refusal(entry: dict[str, Any]) -> str:
    with pytest.raises(ValidationError) as caught:
        ComparisonViewConfig.model_validate({"version": 1, "rules": [entry]})
    return str(caught.value)


# ---------------------------------------------------------------------------
# where
# ---------------------------------------------------------------------------

_ZERO = {"all_zero": ["amount", "tax"]}


@pytest.mark.parametrize(
    ("where", "record", "matches"),
    [
        # equality
        ({"status": "released"}, {"status": "released"}, True),
        ({"status": "released"}, {"status": "held"}, False),
        ({"quantity": 1}, {"quantity": 1.0}, True),
        ({"quantity": 1}, {"quantity": "1"}, False),
        ({"amount": "130"}, {"amount": "130.00"}, False),
        ({"quantity": 1}, {"quantity": True}, False),
        ({"flag": True}, {"flag": 1}, False),
        ({"flag": False}, {"flag": 0}, False),
        ({"flag": False}, {"flag": False}, True),
        ({"note": None}, {}, True),
        ({"note": None}, {"note": None}, True),
        ({"note": None}, {"note": ""}, False),
        ({"status": "released"}, {}, False),
        # in
        ({"status": {"in": ["draft", "superseded"]}}, {"status": "superseded"}, True),
        ({"status": {"in": ["draft", "superseded"]}}, {"status": "accepted"}, False),
        ({"status": {"in": ["draft", None]}}, {}, True),
        ({"quantity": {"in": [0]}}, {"quantity": False}, False),
        # is_null
        ({"note": {"is_null": True}}, {}, True),
        ({"note": {"is_null": True}}, {"note": None}, True),
        ({"note": {"is_null": True}}, {"note": 0}, False),
        ({"note": {"is_null": True}}, {"note": ""}, False),
        ({"note": {"is_null": False}}, {"note": 0}, True),
        ({"note": {"is_null": False}}, {}, False),
        # starts_with
        ({"id": {"starts_with": "AUT-"}}, {"id": "AUT-7"}, True),
        ({"id": {"starts_with": "AUT-"}}, {"id": "MAN-7"}, False),
        ({"id": {"starts_with": "7"}}, {"id": 7}, False),
        ({"id": {"starts_with": "AUT-"}}, {}, False),
        # all_zero
        (_ZERO, {"amount": 0, "tax": 0.0}, True),
        (_ZERO, {"amount": "0.00", "tax": "-0"}, True),
        (_ZERO, {"amount": " 0 ", "tax": "+0.0"}, True),
        (_ZERO, {"amount": ".0", "tax": "0."}, True),
        (_ZERO, {"amount": 0, "tax": "0.01"}, False),
        (_ZERO, {"amount": 0}, False),
        (_ZERO, {"amount": 0, "tax": None}, False),
        (_ZERO, {"amount": 0, "tax": False}, False),
        (_ZERO, {"amount": 0, "tax": "zero"}, False),
        (_ZERO, {"amount": 0, "tax": "0e0"}, False),
        (_ZERO, {"amount": 0, "tax": "NaN"}, False),
        (_ZERO, {"amount": 0, "tax": "٠"}, False),
        (_ZERO, {"amount": 0, "tax": [0]}, False),
        # a conjunction
        ({"status": "released", **_ZERO}, {"status": "released", "amount": 0, "tax": 0}, True),
        ({"status": "released", **_ZERO}, {"status": "released", "amount": 1, "tax": 0}, False),
        ({"status": "released", **_ZERO}, {"status": "held", "amount": 0, "tax": 0}, False),
    ],
)
def test_where_matches_a_record(
    where: dict[str, Any], record: dict[str, Any], matches: bool
) -> None:
    result = _apply({"rows": [record]}, _exclude("rows", where))
    assert (result.state["rows"] == []) is matches


@pytest.mark.parametrize(
    ("where", "fragment"),
    [
        ({}, "where: must be a non-empty mapping of conditions"),
        ("released", "where: must be a non-empty mapping of conditions"),
        ({"status": {"not_in": ["x"]}}, "'status': ['not_in'] is not one operator"),
        ({"status": {"in": ["x"], "is_null": True}}, "is not one operator"),
        ({"status": {}}, "is not one operator"),
        ({"status": ["draft", "superseded"]}, "equality takes one scalar; for any of several"),
        ({"status": {"in": []}}, "at least 1 item"),
        ({"status": {"in": "draft"}}, "'status': in: must be a list of values"),
        (
            {"status": {"in": [["draft"]]}},
            "'status': in: each value must be a string, a number, a bool or null, not a list",
        ),
        ({"note": {"is_null": "yes"}}, "'note': is_null: Input should be a valid boolean"),
        ({"id": {"starts_with": ""}}, "'id': starts_with: String should have at least 1"),
        ({"id": {"starts_with": 5}}, "'id': starts_with: Input should be a valid string"),
        ({"all_zero": []}, "'all_zero' takes a non-empty list of field names"),
        ({"all_zero": "amount"}, "'all_zero' takes a non-empty list of field names"),
        ({"all_zero": ["amount", "amount"]}, "'all_zero' lists a field more than once"),
        ({"all_zero": ["payment.amount"]}, "'payment.amount' names a field of the record"),
        ({"all_zero": [5]}, "'all_zero' lists field names, not a int"),
        ({"all_zero": [" "]}, "where: must not be blank"),
        ({"payment.status": "paid"}, "'payment.status' names a field of the record"),
        ({"": "paid"}, "must not be blank"),
        ({"created": {"date": "2026-01-01"}}, "is not one operator"),
        (
            {"created": date(2026, 1, 1)},
            "'created': the value must be a string, a number, a bool or null, not a date",
        ),
    ],
)
def test_a_malformed_where_is_refused(where: Any, fragment: str) -> None:
    assert fragment in _refusal(_exclude("rows", where))


def test_where_is_required() -> None:
    message = _refusal({"kind": "exclude_records", "table": "rows"})
    assert "rules[0] (exclude_records): where: Field required" in message


@pytest.mark.parametrize(
    ("extra", "fragment"),
    [
        ({"table": ""}, "table: must not be blank"),
        ({"table": "  "}, "table: must not be blank"),
        ({"reason": " "}, "reason: must not be blank"),
        ({"path": ""}, "has an empty segment"),
        ({"path": "a..b"}, "has an empty segment"),
        ({"path": ".a"}, "has an empty segment"),
        ({"path": "a."}, "has an empty segment"),
        ({"unless_referenced_by": [{"table": "t"}]}, "field: Field required"),
        ({"unless_referenced_by": [{"table": "t", "field": "a..b"}]}, "has an empty segment"),
        ({"unless_referenced_by": [{"table": "t", "field": "f", "via": "x"}]}, "Extra inputs"),
        (
            {"path": "allocations", "unless_referenced_by": [{"table": "t", "field": "f"}]},
            "nested items, which have no declared id, so the two do not combine",
        ),
    ],
)
def test_a_malformed_entry_is_refused(extra: dict[str, Any], fragment: str) -> None:
    assert fragment in _refusal({**_exclude("rows", {"status": "released"}), **extra})


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


def test_matching_rows_go_and_the_rest_stay_in_order() -> None:
    rows = [
        {"id": 1, "status": "released"},
        {"id": 2, "status": "held"},
        {"id": 3, "status": "released"},
        {"id": 4, "status": "confirmed"},
    ]
    other = [{"id": 9, "status": "released"}]
    result = _apply({"holds": rows, "other": other}, _exclude("holds", {"status": "released"}))
    assert result.state == {"holds": [rows[1], rows[3]], "other": other}
    assert result.record.applied == (
        RuleApplication(kind="exclude_records", table="holds", rows_removed=2),
    )


def test_a_table_the_state_does_not_hold_is_left_alone() -> None:
    result = _apply({"other": [{"id": 1}]}, _exclude("holds", {"status": "released"}))
    assert result.state == {"other": [{"id": 1}]}
    assert result.record.applied == (
        RuleApplication(kind="exclude_records", table="holds", rows_removed=0),
    )


@pytest.mark.parametrize(
    ("table", "fragment"),
    [
        (
            {"H1": {"status": "released"}},
            "exclude_records: table 'holds' holds a dict, not a list",
        ),
        (None, "holds a NoneType, not a list of records"),
        ([{"status": "held"}, "H2"], "holds a str item; a rule reads fields of mapping records"),
    ],
)
def test_a_table_that_is_not_a_list_of_records_is_refused(table: Any, fragment: str) -> None:
    with pytest.raises(ComparisonViewError, match=fragment):
        _apply({"holds": table}, _exclude("holds", {"status": "released"}))


# ---------------------------------------------------------------------------
# path
# ---------------------------------------------------------------------------

_ZERO_ALLOCATION = {"purchase_key": "P2", "amount": "0.00", "tax": 0}
_PAID_ALLOCATION = {"purchase_key": "P1", "amount": "40.00", "tax": "4.00"}


def _exclude_zero(table: str, path: str) -> dict[str, Any]:
    return _exclude(table, {"all_zero": ["amount", "tax"]}, path=path)


def test_path_filters_the_items_of_a_nested_list_in_each_row() -> None:
    decisions = [
        {"id": "D1", "purchase_allocations": [_PAID_ALLOCATION, _ZERO_ALLOCATION]},
        {"id": "D2", "purchase_allocations": [_ZERO_ALLOCATION, _ZERO_ALLOCATION]},
        {"id": "D3", "purchase_allocations": []},
    ]
    result = _apply({"decisions": decisions}, _exclude_zero("decisions", "purchase_allocations"))
    assert result.state == {
        "decisions": [
            {"id": "D1", "purchase_allocations": [_PAID_ALLOCATION]},
            {"id": "D2", "purchase_allocations": []},
            {"id": "D3", "purchase_allocations": []},
        ]
    }
    assert result.record.applied == (
        RuleApplication(
            kind="exclude_records",
            table="decisions",
            path="purchase_allocations",
            rows_removed=3,
        ),
    )


def test_path_traverses_a_list_on_the_way_element_by_element() -> None:
    """Two levels: the allocations of every payment of a notification."""
    notifications = [
        {
            "id": "N1",
            "expense_payments": [
                {"payment": 1, "purchase_allocations": [_ZERO_ALLOCATION, _PAID_ALLOCATION]},
                {"payment": 2, "purchase_allocations": [_ZERO_ALLOCATION]},
            ],
        }
    ]
    result = _apply(
        {"notifications": notifications},
        _exclude_zero("notifications", "expense_payments.purchase_allocations"),
    )
    assert result.state["notifications"] == [
        {
            "id": "N1",
            "expense_payments": [
                {"payment": 1, "purchase_allocations": [_PAID_ALLOCATION]},
                {"payment": 2, "purchase_allocations": []},
            ],
        }
    ]
    assert result.record.applied[0].rows_removed == 2


def test_path_descends_into_a_mapping() -> None:
    rows = [{"id": "L1", "payload": {"purchase_allocations": [_ZERO_ALLOCATION], "k": 1}}]
    result = _apply({"ledger": rows}, _exclude_zero("ledger", "payload.purchase_allocations"))
    assert result.state["ledger"] == [{"id": "L1", "payload": {"purchase_allocations": [], "k": 1}}]


@pytest.mark.parametrize(
    ("row", "path"),
    [
        ({"id": "D1"}, "purchase_allocations"),
        ({"id": "D1", "purchase_allocations": None}, "purchase_allocations"),
        ({"id": "D1"}, "expense_payments.purchase_allocations"),
        ({"id": "D1", "expense_payments": None}, "expense_payments.purchase_allocations"),
        (
            {"id": "D1", "expense_payments": [None, {"payment": 1}]},
            "expense_payments.purchase_allocations",
        ),
    ],
    ids=["missing", "null", "missing-on-the-way", "null-on-the-way", "null-or-missing-per-item"],
)
def test_a_row_without_the_nested_list_stays_as_it_is(row: dict[str, Any], path: str) -> None:
    result = _apply({"decisions": [row]}, _exclude_zero("decisions", path))
    assert result.state == {"decisions": [row]}
    assert result.record.applied[0].rows_removed == 0


@pytest.mark.parametrize(
    ("row", "path", "fragment"),
    [
        (
            {"purchase_allocations": "none"},
            "purchase_allocations",
            "exclude_records: 'decisions.purchase_allocations' holds a str, not a list",
        ),
        (
            {"purchase_allocations": {"P1": _ZERO_ALLOCATION}},
            "purchase_allocations",
            "holds a dict",
        ),
        ({"purchase_allocations": [0, 0]}, "purchase_allocations", "holds a int item"),
        (
            {"expense_payments": "n/a"},
            "expense_payments.purchase_allocations",
            "field 'purchase_allocations' is read from a str, not a mapping or a list",
        ),
        (
            {"expense_payments": [7]},
            "expense_payments.purchase_allocations",
            "field 'purchase_allocations' is read from a int",
        ),
    ],
    ids=["terminal-string", "terminal-mapping", "scalar-items", "scalar-on-the-way", "scalar-item"],
)
def test_a_value_that_does_not_fit_the_path_is_refused(
    row: dict[str, Any], path: str, fragment: str
) -> None:
    with pytest.raises(ComparisonViewError, match=fragment):
        _apply({"decisions": [row]}, _exclude_zero("decisions", path))


# ---------------------------------------------------------------------------
# unless_referenced_by
# ---------------------------------------------------------------------------

_HOLDS = [
    {"id": "H1", "status": "released"},
    {"id": "H2", "status": "released"},
    {"id": "H3", "status": "confirmed"},
]


def _released_unless(*references: dict[str, str]) -> dict[str, Any]:
    return _exclude("holds", {"status": "released"}, unless_referenced_by=list(references))


def _kept_holds(result: ComparisonViewResult) -> list[str]:
    return [row["id"] for row in result.state["holds"]]


@pytest.mark.parametrize(
    ("equipment", "field", "kept"),
    [
        ([{"id": "E1", "hold_id": "H1"}], "hold_id", ["H1", "H3"]),
        ([{"id": "E1", "hold_id": None}, {"id": "E2"}], "hold_id", ["H3"]),
        ([{"id": "E1", "hold_ids": ["H2", "H9"]}], "hold_ids", ["H2", "H3"]),
        (
            [{"id": "E1", "slots": [{"hold": "H1"}, {"hold": "H2"}]}],
            "slots.hold",
            ["H1", "H2", "H3"],
        ),
        ([{"id": "E1", "slot": {"hold": "H2"}}], "slot.hold", ["H2", "H3"]),
        ([{"id": "E1", "hold_id": "H3"}], "hold_id", ["H3"]),
    ],
    ids=["scalar", "null-or-missing", "list", "nested-list", "nested-mapping", "non-matching"],
)
def test_a_referenced_matching_row_is_kept(
    equipment: list[dict[str, Any]], field: str, kept: list[str]
) -> None:
    result = _apply(
        {"holds": _HOLDS, "equipment": equipment},
        _released_unless({"table": "equipment", "field": field}),
    )
    assert _kept_holds(result) == kept


def test_any_listed_reference_keeps_a_row() -> None:
    state = {
        "holds": _HOLDS,
        "equipment": [{"id": "E1", "hold_id": "H1"}],
        "invoices": [{"id": "I1", "hold": "H2"}],
    }
    result = _apply(
        state,
        _released_unless(
            {"table": "equipment", "field": "hold_id"}, {"table": "invoices", "field": "hold"}
        ),
    )
    assert _kept_holds(result) == ["H1", "H2", "H3"]


def test_a_referencing_table_the_state_does_not_hold_references_nothing() -> None:
    result = _apply({"holds": _HOLDS}, _released_unless({"table": "equipment", "field": "hold_id"}))
    assert _kept_holds(result) == ["H3"]


@pytest.mark.parametrize("declared", ["hold_ref", ["hold_ref"]])
def test_the_id_field_comes_from_id_fields(declared: str | list[str]) -> None:
    holds = [{"id": "X1", "hold_ref": "R1", "status": "released"}]
    result = _apply(
        {"holds": holds, "equipment": [{"id": "E1", "hold": "R1"}]},
        _released_unless({"table": "equipment", "field": "hold"}),
        id_fields={"holds": declared},
    )
    assert _kept_holds(result) == ["X1"]


def test_references_are_read_before_the_rule_removes_anything() -> None:
    """P1 is referenced by P2, which the rule drops; P1 stays."""
    proposals = [
        {"id": "P1", "status": "superseded", "replaces": None},
        {"id": "P2", "status": "draft", "replaces": "P1"},
        {"id": "P3", "status": "accepted", "replaces": None},
    ]
    result = _apply(
        {"proposals": proposals},
        _exclude(
            "proposals",
            {"status": {"in": ["draft", "superseded"]}},
            unless_referenced_by=[{"table": "proposals", "field": "replaces"}],
        ),
    )
    assert [row["id"] for row in result.state["proposals"]] == ["P1", "P3"]


def test_a_rows_reference_to_itself_does_not_keep_it() -> None:
    holds = [
        {"id": "H1", "status": "released", "follows": "H1"},
        {"id": "H2", "status": "released", "follows": "H3"},
        {"id": "H3", "status": "released", "follows": None},
    ]
    result = _apply({"holds": holds}, _released_unless({"table": "holds", "field": "follows"}))
    assert _kept_holds(result) == ["H3"]


def test_a_row_referenced_by_itself_and_by_another_row_is_kept() -> None:
    holds = [
        {"id": "H1", "status": "released", "follows": ["H1"]},
        {"id": "H2", "status": "confirmed", "follows": ["H1"]},
    ]
    result = _apply({"holds": holds}, _released_unless({"table": "holds", "field": "follows"}))
    assert _kept_holds(result) == ["H1", "H2"]


@pytest.mark.parametrize(
    ("field", "id_fields"),
    [("id", {}), ("hold_ref", {"holds": "hold_ref"}), ("hold_ref", {"holds": ["hold_ref"]})],
)
def test_naming_the_rules_own_id_field_is_refused(
    field: str, id_fields: dict[str, str | list[str]]
) -> None:
    rule = _released_unless({"table": "holds", "field": field})
    with pytest.raises(ComparisonViewError, match=f"names holds.{field}, the id field of the rule"):
        _apply({"holds": _HOLDS}, rule, id_fields=id_fields)


def test_a_field_named_id_is_an_ordinary_reference_when_the_key_is_another_field() -> None:
    holds = [
        {"hold_ref": "R1", "id": "R2", "status": "released"},
        {"hold_ref": "R2", "id": None, "status": "released"},
    ]
    result = _apply(
        {"holds": holds},
        _released_unless({"table": "holds", "field": "id"}),
        id_fields={"holds": "hold_ref"},
    )
    assert [row["hold_ref"] for row in result.state["holds"]] == ["R2"]


@pytest.mark.parametrize(
    ("hold_id", "reference", "kept"),
    [(1, 1.0, True), (1, "1", False), ("1", 1, False), (1, True, False), (True, 1, False)],
    ids=["int-float", "int-str", "str-int", "int-bool", "bool-int"],
)
def test_an_id_and_a_reference_match_as_json_values(
    hold_id: Any, reference: Any, kept: bool
) -> None:
    result = _apply(
        {"holds": [{"id": hold_id, "status": "released"}], "equipment": [{"hold_id": reference}]},
        _released_unless({"table": "equipment", "field": "hold_id"}),
    )
    assert (result.state["holds"] != []) is kept


def test_a_bool_reference_does_not_hold_a_numeric_id() -> None:
    holds = [{"id": 1, "status": "released"}]
    result = _apply(
        {"holds": holds, "equipment": [{"id": "E1", "hold_id": True}]},
        _released_unless({"table": "equipment", "field": "hold_id"}),
    )
    assert result.state["holds"] == []


def test_only_matching_rows_need_an_id() -> None:
    holds = [{"status": "confirmed"}, {"id": "H1", "status": "released"}]
    result = _apply({"holds": holds}, _released_unless({"table": "equipment", "field": "hold_id"}))
    assert result.state["holds"] == [{"status": "confirmed"}]


@pytest.mark.parametrize(
    ("state", "id_fields", "fragment"),
    [
        (
            {"holds": [{"hold_ref": "H1", "status": "released"}]},
            {},
            "its id field 'id' is missing or null",
        ),
        (
            {"holds": [{"id": ["H", 1], "status": "released"}]},
            {},
            "holds a list in its id field 'id'; an id is a string, a number or a bool",
        ),
        (
            {"holds": _HOLDS},
            {"holds": ["region", "id"]},
            "is \\['region', 'id'\\]; unless_referenced_by needs one id field",
        ),
        (
            {"holds": _HOLDS, "equipment": [{"id": "E1", "hold_id": {"id": "H1"}}]},
            {},
            "unless_referenced_by: 'equipment.hold_id' holds a dict, not an id",
        ),
        (
            {"holds": [{"id": None, "status": "released"}]},
            {},
            "its id field 'id' is missing or null",
        ),
        (
            {"holds": [{"id": date(2026, 1, 1), "status": "released"}]},
            {},
            "holds a date in its id field 'id'; an id is a string, a number or a bool",
        ),
        (
            {"holds": _HOLDS, "equipment": [{"id": "E1", "hold_id": {"H1", "H2"}}]},
            {},
            "unless_referenced_by: 'equipment.hold_id' holds a set, not an id",
        ),
        (
            {"holds": _HOLDS, "equipment": [{"id": "E1", "hold_id": [date(2026, 1, 1)]}]},
            {},
            "unless_referenced_by: 'equipment.hold_id' holds a date, not an id",
        ),
        (
            {"holds": _HOLDS, "equipment": {"E1": {"hold_id": "H1"}}},
            {},
            "unless_referenced_by: table 'equipment' holds a dict, not a list of records",
        ),
    ],
    ids=[
        "no-id",
        "list-id",
        "composite-key",
        "mapping-reference",
        "null-id",
        "date-id",
        "set-reference",
        "date-reference",
        "table-not-a-list",
    ],
)
def test_a_reference_that_cannot_be_resolved_is_refused(
    state: dict[str, Any], id_fields: dict[str, Any], fragment: str
) -> None:
    rule = _released_unless({"table": "equipment", "field": "hold_id"})
    with pytest.raises(ComparisonViewError, match=fragment):
        _apply(state, rule, id_fields=id_fields)


# ---------------------------------------------------------------------------
# exclude_tables
# ---------------------------------------------------------------------------


def test_exclude_tables_drops_the_named_tables_whole() -> None:
    state = {
        "agent_discoverable_tools": [{"tool": "a"}, {"tool": "b"}],
        "settings": {"locale": "en"},
        "orders": [{"id": 1}],
    }
    result = _apply(
        state,
        {
            "kind": "exclude_tables",
            "tables": ["agent_discoverable_tools", "settings", "user_discoverable_tools"],
            "reason": "bookkeeping",
        },
    )
    assert result.state == {"orders": [{"id": 1}]}
    assert result.record.applied == (
        RuleApplication(kind="exclude_tables", table="agent_discoverable_tools", rows_removed=2),
        RuleApplication(kind="exclude_tables", table="settings", rows_removed=1),
        RuleApplication(kind="exclude_tables", table="user_discoverable_tools", rows_removed=0),
    )
