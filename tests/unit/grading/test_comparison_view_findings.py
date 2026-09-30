"""``comparison_view_findings``: the tables and fields a view names must exist (ADR-0053).

A table exists when ``initial_state.tables`` seeds it or ``initial_state.schemas``
declares it. A field exists when a seeded record at its path carries it or, for a
field of the row itself, the table's schema declares it; a path that reaches no
seeded record and no schema is not checked.
"""

from __future__ import annotations

from typing import Any

import pytest

from tolokaforge.core.grading.comparison_view import ComparisonViewConfig, comparison_view_findings

pytestmark = pytest.mark.unit

_TABLES: dict[str, list[dict[str, Any]]] = {
    "transfer_holds": [{"id": "H1", "status": "held"}],
    "transfer_equipment": [
        {"id": "E1", "hold_id": "H1", "slots": [{"hold": "H1", "bay": 2}]},
    ],
    "recovery_expense_decisions": [
        {
            "id": "D1",
            "purchase_allocations": [{"purchase_key": "P1", "amount": "1.00", "tax": "0"}],
            "notes": [],
        }
    ],
    "notifications": [],
}
_SCHEMA_FIELDS: dict[str, list[str]] = {
    "transfer_holds": ["id", "status", "released_at"],
    "refunds": ["id", "amount"],
}


def _findings(*rules: dict[str, Any], id_fields: dict[str, Any] | None = None) -> list[str]:
    view = ComparisonViewConfig.model_validate({"version": 1, "rules": list(rules)})
    return comparison_view_findings(
        view, tables=_TABLES, schema_fields=_SCHEMA_FIELDS, id_fields=id_fields or {}
    )


def _exclude(table: str, where: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"kind": "exclude_records", "table": table, "where": where, **extra}


def test_a_view_that_fits_the_initial_state_has_no_findings() -> None:
    assert (
        _findings(
            _exclude(
                "transfer_holds",
                {"status": "released", "released_at": {"is_null": False}},
                unless_referenced_by=[
                    {"table": "transfer_equipment", "field": "hold_id"},
                    {"table": "transfer_equipment", "field": "slots.hold"},
                ],
            ),
            _exclude(
                "recovery_expense_decisions",
                {"all_zero": ["amount", "tax"]},
                path="purchase_allocations",
            ),
            _exclude("refunds", {"amount": 0}),
            {"kind": "exclude_tables", "tables": ["notifications"], "reason": "outbox"},
        )
        == []
    )


@pytest.mark.parametrize(
    "rule",
    [
        _exclude("transfer_holdz", {"status": "released"}),
        _exclude(
            "transfer_holds",
            {"status": "released"},
            unless_referenced_by=[{"table": "transfer_holdz", "field": "hold_id"}],
        ),
        {"kind": "exclude_tables", "tables": ["notifications", "transfer_holdz"], "reason": "r"},
    ],
    ids=["exclude_records", "unless_referenced_by", "exclude_tables"],
)
def test_a_table_the_initial_state_does_not_declare_is_a_finding(rule: dict[str, Any]) -> None:
    (finding,) = _findings({"kind": "exclude_tables", "tables": ["refunds"], "reason": "r"}, rule)
    assert finding.startswith(f"state_checks.comparison_view.rules[1] ({rule['kind']}) names ")
    assert "table(s) ['transfer_holdz'] absent from initial_state" in finding
    assert "state_checks.relaxed_validation: true" in finding


@pytest.mark.parametrize(
    ("rule", "dotted", "table"),
    [
        (_exclude("transfer_holds", {"statuz": "released"}), "statuz", "transfer_holds"),
        (_exclude("refunds", {"amont": 0}), "amont", "refunds"),
        (
            _exclude(
                "recovery_expense_decisions",
                {"all_zero": ["amount", "taxx"]},
                path="purchase_allocations",
            ),
            "purchase_allocations.taxx",
            "recovery_expense_decisions",
        ),
        (
            _exclude(
                "recovery_expense_decisions", {"all_zero": ["amount"]}, path="purchase_allocationz"
            ),
            "purchase_allocationz",
            "recovery_expense_decisions",
        ),
        (
            _exclude(
                "transfer_holds",
                {"status": "released"},
                unless_referenced_by=[{"table": "transfer_equipment", "field": "slots.hols"}],
            ),
            "slots.hols",
            "transfer_equipment",
        ),
        (
            _exclude(
                "transfer_holds",
                {"status": "released"},
                unless_referenced_by=[{"table": "transfer_equipment", "field": "slot.hold"}],
            ),
            "slot",
            "transfer_equipment",
        ),
    ],
    ids=[
        "where-field",
        "schema-only-table",
        "nested-where-field",
        "path-segment",
        "nested-reference",
        "reference-path-segment",
    ],
)
def test_a_field_no_seeded_record_or_schema_carries_is_a_finding(
    rule: dict[str, Any], dotted: str, table: str
) -> None:
    (finding,) = _findings(rule)
    assert f"names field {dotted!r} of table {table!r}" in finding
    assert "(known: [" in finding


@pytest.mark.parametrize(
    "rule",
    [
        _exclude("transfer_holds", {"released_at": {"is_null": False}}),
        _exclude("notifications", {"anything": 1}),
        _exclude("recovery_expense_decisions", {"anything": 1}, path="notes"),
        _exclude("recovery_expense_decisions", {"anything": 1}, path="notes.parts"),
    ],
    ids=[
        "declared-by-the-schema",
        "table-seeded-empty",
        "nested-list-seeded-empty",
        "path-past-the-seeded-data",
    ],
)
def test_a_field_with_nothing_to_check_it_against_is_not_a_finding(rule: dict[str, Any]) -> None:
    assert _findings(rule) == []


def test_the_id_field_of_a_table_kept_by_reference_must_exist() -> None:
    rule = _exclude(
        "transfer_holds",
        {"status": "released"},
        unless_referenced_by=[{"table": "transfer_equipment", "field": "hold_id"}],
    )
    assert _findings(rule, id_fields={"transfer_holds": "id"}) == []
    (finding,) = _findings(rule, id_fields={"transfer_holds": "hold_ref"})
    assert "names field 'hold_ref' of table 'transfer_holds'" in finding
    (finding,) = _findings(rule, id_fields={"transfer_holds": ["region", "id"]})
    assert finding.startswith("state_checks.comparison_view.rules[0] (exclude_records): ")
    assert "a composite key has no single field a reference could hold" in finding


def test_every_rule_reports_its_own_findings() -> None:
    findings = _findings(
        _exclude("transfer_holds", {"statuz": "released"}),
        {"kind": "exclude_tables", "tables": ["logz"], "reason": "r"},
    )
    assert [finding.split(" names ")[0] for finding in findings] == [
        "state_checks.comparison_view.rules[0] (exclude_records)",
        "state_checks.comparison_view.rules[1] (exclude_tables)",
    ]
