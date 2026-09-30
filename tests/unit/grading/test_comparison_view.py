"""The comparison view's block, rule table, invariants and record (ADR-0053).

What each ``exclude_records`` condition, path and reference matches is covered in
``test_comparison_view_exclude_records.py``; the load-time check against a task's
initial state in ``test_comparison_view_findings.py``.
"""

from __future__ import annotations

import ast
import copy
import inspect
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tolokaforge.core.grading import comparison_view
from tolokaforge.core.grading.comparison_view import (
    COMPARISON_VIEW_FUNCTION_VERSION,
    ComparisonViewConfig,
    ComparisonViewError,
    ComparisonViewRule,
    ExcludeRecords,
    ExcludeRecordsConfig,
    ExcludeTablesConfig,
    RuleApplication,
    apply_comparison_view,
    comparison_view_rules,
    resolve_comparison_view_rule,
)
from tolokaforge.core.hash import compute_stable_hash

pytestmark = pytest.mark.unit


def _view(*rules: dict[str, Any]) -> ComparisonViewConfig:
    return ComparisonViewConfig.model_validate({"version": 1, "rules": list(rules)})


def _refusal(block: dict[str, Any]) -> str:
    with pytest.raises(ValidationError) as caught:
        ComparisonViewConfig.model_validate(block)
    return str(caught.value)


_RELEASED_HOLDS = {
    "kind": "exclude_records",
    "table": "transfer_holds",
    "where": {"status": "released"},
    "unless_referenced_by": [{"table": "transfer_equipment", "field": "hold_id"}],
    "reason": "a released hold binds nothing",
}
_ZERO_ALLOCATIONS = {
    "kind": "exclude_records",
    "table": "recovery_expense_decisions",
    "path": "purchase_allocations",
    "where": {"all_zero": ["amount", "tax"]},
}
_DRAFT_PROPOSALS = {
    "kind": "exclude_records",
    "table": "wallet_proposals",
    "where": {
        "status": {"in": ["draft", "superseded"]},
        "accepted_at": {"is_null": True},
        "id": {"starts_with": "WP-"},
    },
}
_BOOKKEEPING = {
    "kind": "exclude_tables",
    "tables": ["agent_discoverable_tools", "user_discoverable_tools"],
    "reason": "written by read tools; not business state",
}

_STATE: dict[str, Any] = {
    "transfer_holds": [
        {"id": "HOLD-1", "status": "confirmed"},
        {"id": "HOLD-2", "status": "released"},
        {"id": "HOLD-3", "status": "released"},
    ],
    "transfer_equipment": [{"id": "EQ-1", "hold_id": "HOLD-3"}],
    "recovery_expense_decisions": [
        {
            "id": "RED-1",
            "purchase_allocations": [
                {"purchase_key": "P1", "amount": "40.00", "tax": "4.00"},
                {"purchase_key": "P2", "amount": "0.00", "tax": 0},
            ],
        }
    ],
    "agent_discoverable_tools": [{"tool": "search"}, {"tool": "lookup"}],
}
_INITIAL: dict[str, Any] = {
    "transfer_holds": [{"id": "HOLD-1", "status": "held"}],
    "transfer_equipment": [],
    "recovery_expense_decisions": [],
    "agent_discoverable_tools": [],
}


# ---------------------------------------------------------------------------
# The rule table
# ---------------------------------------------------------------------------


def test_the_rule_table_holds_each_built_in_rule_under_its_kind() -> None:
    table = comparison_view_rules()
    assert sorted(table) == ["exclude_records", "exclude_tables"]
    for kind, rule in table.items():
        assert isinstance(rule, ComparisonViewRule)
        assert rule.kind == kind
        assert rule.config_model.model_fields["kind"].default == kind
        assert resolve_comparison_view_rule(kind) is rule


def test_the_rule_table_cannot_be_changed_in_place() -> None:
    with pytest.raises(TypeError):
        comparison_view_rules()["custom"] = ExcludeRecords()  # type: ignore[index]


def test_resolving_an_unknown_kind_names_the_known_ones() -> None:
    with pytest.raises(ComparisonViewError, match=r"'custom'; known kinds: \['exclude_records'"):
        resolve_comparison_view_rule("custom")


# ---------------------------------------------------------------------------
# The block
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["drop_fields", "unordered", "custom", 3, ["exclude_tables"]])
def test_an_unknown_kind_is_refused_naming_the_known_ones(kind: Any) -> None:
    message = _refusal({"version": 1, "rules": [{"kind": kind}]})
    assert f"rules[0]: unknown comparison_view rule kind {kind!r}" in message
    assert "known kinds: ['exclude_records', 'exclude_tables']" in message


@pytest.mark.parametrize("version", [2, 0, True, 1.0, "1", None])
def test_an_unknown_version_is_refused_naming_the_known_ones(version: Any) -> None:
    message = _refusal({"version": version, "rules": []})
    assert f"comparison_view.version {version!r} is not a version this engine reads" in message
    assert "known versions: [1]" in message


def test_the_version_is_required() -> None:
    assert "version" in _refusal({"rules": []})


@pytest.mark.parametrize(
    ("block", "fragment"),
    [
        ({"version": 1, "rules": [], "profiles": {}}, "profiles\n  Extra inputs are not permitted"),
        ({"version": 1}, "rules\n  Field required"),
        ({"version": 1, "rules": _BOOKKEEPING}, "must be a list of rule entries"),
        ({"version": 1, "rules": [{"table": "t"}]}, "rules[0] must be a mapping with a 'kind'"),
        ({"version": 1, "rules": ["exclude_tables"]}, "rules[0] must be a mapping with a 'kind'"),
    ],
)
def test_a_malformed_block_is_refused(block: dict[str, Any], fragment: str) -> None:
    assert fragment in _refusal(block)


def test_an_entry_is_validated_by_its_own_rule_and_refuses_undeclared_keys() -> None:
    entry = {**_RELEASED_HOLDS, "order": "unordered"}
    message = _refusal({"version": 1, "rules": [_BOOKKEEPING, entry]})
    assert "rules[1] (exclude_records): order: Extra inputs are not permitted" in message


def test_an_entry_is_validated_into_its_rules_config_model() -> None:
    view = _view(_RELEASED_HOLDS, _ZERO_ALLOCATIONS, _BOOKKEEPING)
    assert [type(rule) for rule in view.rules] == [
        ExcludeRecordsConfig,
        ExcludeRecordsConfig,
        ExcludeTablesConfig,
    ]


def test_a_config_instance_stands_in_for_its_entry() -> None:
    config = ExcludeTablesConfig(tables=("agent_discoverable_tools",), reason="bookkeeping")
    view = ComparisonViewConfig(version=1, rules=(config,))
    assert view.rules == (config,)


def test_a_config_instance_under_another_rules_kind_is_refused() -> None:
    config = ExcludeRecordsConfig.model_construct(kind="exclude_tables", table="t", where={"a": 1})
    with pytest.raises(ValidationError, match="not the ExcludeTablesConfig the exclude_tables"):
        ComparisonViewConfig(version=1, rules=(config,))


def test_a_validated_view_cannot_change() -> None:
    view = _view(_RELEASED_HOLDS, _DRAFT_PROPOSALS)
    sha = view.config_sha256()
    with pytest.raises(TypeError):
        view.rules[1].where["status"] = "draft"  # type: ignore[index]
    with pytest.raises(ValidationError):
        view.rules[0].table = "other"  # type: ignore[misc]
    assert isinstance(view.rules, tuple)
    assert isinstance(view.rules[0].unless_referenced_by, tuple)
    assert view.config_sha256() == sha


def test_the_block_round_trips_through_its_json_dump() -> None:
    view = _view(_RELEASED_HOLDS, _ZERO_ALLOCATIONS, _DRAFT_PROPOSALS, _BOOKKEEPING)
    dump = view.model_dump(mode="json", by_alias=True)
    assert dump["rules"][2]["where"]["status"] == {"in": ["draft", "superseded"]}
    again = ComparisonViewConfig.model_validate(dump)
    assert again == view
    assert again.config_sha256() == view.config_sha256()


# ---------------------------------------------------------------------------
# exclude_tables guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entry", "fragment"),
    [
        ({"kind": "exclude_tables", "tables": ["t"]}, "reason: Field required"),
        ({"kind": "exclude_tables", "tables": ["t"], "reason": "  "}, "must not be blank"),
        ({"kind": "exclude_tables", "tables": [], "reason": "r"}, "at least 1 item"),
        ({"kind": "exclude_tables", "reason": "r"}, "tables: Field required"),
        ({"kind": "exclude_tables", "tables": ["t", "t"], "reason": "r"}, "more than once"),
        ({"kind": "exclude_tables", "tables": [""], "reason": "r"}, "must not be blank"),
    ],
)
def test_exclude_tables_refuses_a_malformed_entry(entry: dict[str, Any], fragment: str) -> None:
    assert fragment in _refusal({"version": 1, "rules": [entry]})


@pytest.mark.parametrize(
    "other",
    [
        {"kind": "exclude_records", "table": "transfer_holds", "where": {"status": "released"}},
        {
            "kind": "exclude_records",
            "table": "wallet_proposals",
            "where": {"status": "draft"},
            "unless_referenced_by": [{"table": "transfer_holds", "field": "proposal_id"}],
        },
        {"kind": "exclude_tables", "tables": ["transfer_holds"], "reason": "again"},
    ],
    ids=["its-table", "its-references", "another-exclude-tables"],
)
@pytest.mark.parametrize("dropping_first", [True, False])
def test_exclude_tables_refuses_a_table_another_rule_names(
    other: dict[str, Any], dropping_first: bool
) -> None:
    dropping = {"kind": "exclude_tables", "tables": ["logs", "transfer_holds"], "reason": "r"}
    rules = [dropping, other] if dropping_first else [other, dropping]
    message = _refusal({"version": 1, "rules": rules})
    assert "(exclude_tables) drops table(s) ['transfer_holds'] that rules[" in message
    assert "a table is dropped whole or shaped by other rules, not both" in message


def test_exclude_tables_accepts_tables_no_other_rule_names() -> None:
    view = _view(_RELEASED_HOLDS, _ZERO_ALLOCATIONS, _BOOKKEEPING)
    assert len(view.rules) == 3


# ---------------------------------------------------------------------------
# Invariants of the function
# ---------------------------------------------------------------------------


def test_the_input_is_never_mutated_and_the_view_shares_nothing_with_it() -> None:
    state, initial = copy.deepcopy(_STATE), copy.deepcopy(_INITIAL)
    result = apply_comparison_view(
        state,
        initial=initial,
        view=_view(_RELEASED_HOLDS, _ZERO_ALLOCATIONS, _BOOKKEEPING),
        id_fields={},
    )
    assert state == _STATE
    assert initial == _INITIAL
    for rows in result.state.values():
        for row in rows:
            row["touched"] = True
            for allocation in row.get("purchase_allocations", []):
                allocation["touched"] = True
    assert state == _STATE


def test_the_same_inputs_give_the_same_view_and_record() -> None:
    view = _view(_RELEASED_HOLDS, _ZERO_ALLOCATIONS, _BOOKKEEPING)
    first = apply_comparison_view(_STATE, initial=_INITIAL, view=view, id_fields={})
    second = apply_comparison_view(
        copy.deepcopy(_STATE),
        initial=copy.deepcopy(_INITIAL),
        view=_view(*view.rules),
        id_fields={},
    )
    assert first.state == second.state
    assert first.record == second.record


def test_the_golden_is_not_a_parameter() -> None:
    parameters = inspect.signature(apply_comparison_view).parameters
    assert list(parameters) == ["state", "initial", "view", "id_fields"]
    assert [p.kind for p in parameters.values()][1:] == [inspect.Parameter.KEYWORD_ONLY] * 3


@pytest.mark.parametrize(
    ("state", "fragment"),
    [
        ({**_STATE, "transfer_holds": {"HOLD-1": {"status": "released"}}}, "not a list of records"),
        ({**_STATE, "transfer_holds": ["HOLD-1"]}, "holds a str item"),
        (
            {**_STATE, "recovery_expense_decisions": [{"purchase_allocations": "none"}]},
            "holds a str, not a list of records",
        ),
        ({**_STATE, "transfer_holds": [{"status": "released"}]}, "has no id field 'id'"),
    ],
    ids=["table-not-a-list", "row-not-a-mapping", "path-not-a-list", "matched-row-without-id"],
)
def test_a_failing_rule_raises_instead_of_returning_a_state(
    state: dict[str, Any], fragment: str
) -> None:
    before = copy.deepcopy(state)
    view = _view(_BOOKKEEPING, _RELEASED_HOLDS, _ZERO_ALLOCATIONS)
    with pytest.raises(ComparisonViewError, match=fragment):
        apply_comparison_view(state, initial=_INITIAL, view=view, id_fields={})
    assert state == before


def test_an_empty_view_returns_the_state_unchanged_with_a_record() -> None:
    result = apply_comparison_view(_STATE, initial=None, view=_view(), id_fields={})
    assert result.state == _STATE
    assert result.state is not _STATE
    assert result.applied == ()
    assert result.record.version == 1
    assert result.record.function_version == COMPARISON_VIEW_FUNCTION_VERSION
    assert result.record.applied == ()


def test_a_rule_handed_another_rules_config_is_a_programming_error() -> None:
    config = ExcludeTablesConfig(tables=("t",), reason="r")
    with pytest.raises(TypeError, match="applies an ExcludeRecordsConfig"):
        ExcludeRecords().apply({}, initial=None, id_fields={}, config=config)


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


def test_the_record_names_the_version_the_function_and_what_each_rule_did() -> None:
    view = _view(_RELEASED_HOLDS, _ZERO_ALLOCATIONS, _BOOKKEEPING)
    result = apply_comparison_view(_STATE, initial=_INITIAL, view=view, id_fields={})
    assert result.record.version == 1
    assert result.record.function_version == COMPARISON_VIEW_FUNCTION_VERSION == 1
    assert result.record.config_sha256 == view.config_sha256()
    assert (
        result.record.applied
        == result.applied
        == (
            RuleApplication(kind="exclude_records", table="transfer_holds", rows_removed=1),
            RuleApplication(
                kind="exclude_records",
                table="recovery_expense_decisions",
                path="purchase_allocations",
                rows_removed=1,
            ),
            RuleApplication(
                kind="exclude_tables", table="agent_discoverable_tools", rows_removed=2
            ),
            RuleApplication(kind="exclude_tables", table="user_discoverable_tools", rows_removed=0),
        )
    )
    assert all(application.ids_rewritten == 0 for application in result.applied)


def test_the_config_sha_is_pinned() -> None:
    """The sha is part of the record: a change to what is hashed changes every recorded one."""
    view = _view(_RELEASED_HOLDS, _ZERO_ALLOCATIONS, _DRAFT_PROPOSALS, _BOOKKEEPING)
    assert view.config_sha256() == (
        "751b8799c0409f2594e6b5394e8b9a4302b82a0ee5e5935d660cc29d75454ef4"
    )


def test_the_key_order_of_the_declaration_does_not_change_the_sha() -> None:
    reordered = {
        "reason": "a released hold binds nothing",
        "unless_referenced_by": [{"field": "hold_id", "table": "transfer_equipment"}],
        "where": {"status": "released"},
        "table": "transfer_holds",
        "kind": "exclude_records",
    }
    two_conditions = {"kind": "exclude_records", "table": "t", "where": {"a": 1, "b": {"in": [2]}}}
    swapped = {"kind": "exclude_records", "table": "t", "where": {"b": {"in": [2]}, "a": 1}}
    assert _view(reordered).config_sha256() == _view(_RELEASED_HOLDS).config_sha256()
    assert _view(swapped).config_sha256() == _view(two_conditions).config_sha256()


def test_a_setting_spelled_out_at_its_default_hashes_like_one_left_out() -> None:
    """So a new optional field whose default keeps behaviour keeps every existing sha."""
    spelled_out = {**_ZERO_ALLOCATIONS, "unless_referenced_by": [], "reason": None}
    assert _view(spelled_out).config_sha256() == _view(_ZERO_ALLOCATIONS).config_sha256()


def test_the_reason_is_not_hashed() -> None:
    reworded = {**_BOOKKEEPING, "reason": "bookkeeping of the discovery tools"}
    without = {key: value for key, value in _RELEASED_HOLDS.items() if key != "reason"}
    assert _view(reworded).config_sha256() == _view(_BOOKKEEPING).config_sha256()
    assert _view(without).config_sha256() == _view(_RELEASED_HOLDS).config_sha256()


@pytest.mark.parametrize(
    "rules",
    [
        [{**_RELEASED_HOLDS, "where": {"status": "cancelled"}}, _ZERO_ALLOCATIONS],
        [_ZERO_ALLOCATIONS, _RELEASED_HOLDS],
        [_RELEASED_HOLDS],
        [{**_RELEASED_HOLDS, "unless_referenced_by": []}, _ZERO_ALLOCATIONS],
    ],
    ids=["other-condition", "other-order", "fewer-rules", "no-references"],
)
def test_a_different_declaration_gives_a_different_sha(rules: list[dict[str, Any]]) -> None:
    assert (
        _view(*rules).config_sha256() != _view(_RELEASED_HOLDS, _ZERO_ALLOCATIONS).config_sha256()
    )


# ---------------------------------------------------------------------------
# The A0 finding: optional records stop failing the hash, real differences do not
# ---------------------------------------------------------------------------

_A0_GOLDEN: dict[str, Any] = {
    "transfer_holds": [{"id": "HOLD-1", "status": "confirmed", "segment": "S1"}],
    "transfer_equipment": [{"id": "EQ-1", "hold_id": "HOLD-1"}],
    "recovery_expense_decisions": [
        {
            "id": "RED-1",
            "purchase_allocations": [{"purchase_key": "P1", "amount": "40.00", "tax": "4.00"}],
        }
    ],
}


_RELEASED_HOLD = {"id": "HOLD-2", "status": "released", "segment": "S2"}


def _a0_trial(
    *,
    extra_hold: dict[str, Any] | None = None,
    equipment_on_it: bool = False,
    zero_allocation: bool = False,
    amount: str = "40.00",
) -> dict[str, Any]:
    """The golden, plus an extra hold (optionally referenced), a zero allocation or another amount."""
    trial = copy.deepcopy(_A0_GOLDEN)
    if extra_hold is not None:
        trial["transfer_holds"].append(extra_hold)
    if equipment_on_it:
        trial["transfer_equipment"].append({"id": "EQ-2", "hold_id": extra_hold["id"]})
    allocations = trial["recovery_expense_decisions"][0]["purchase_allocations"]
    allocations[0]["amount"] = amount
    if zero_allocation:
        allocations.append({"purchase_key": "P2", "amount": "0.00", "tax": 0})
    return trial


@pytest.mark.parametrize(
    ("trial", "gets_the_goldens_digest"),
    [
        (_a0_trial(extra_hold=_RELEASED_HOLD), True),
        (_a0_trial(zero_allocation=True), True),
        (_a0_trial(extra_hold=_RELEASED_HOLD, zero_allocation=True), True),
        (_a0_trial(amount="45.00"), False),
        (_a0_trial(extra_hold={**_RELEASED_HOLD, "status": "confirmed"}), False),
        (_a0_trial(extra_hold=_RELEASED_HOLD, equipment_on_it=True), False),
    ],
    ids=[
        "released-hold",
        "zero-allocation",
        "both",
        "real-amount",
        "active-extra-hold",
        "released-but-referenced",
    ],
)
def test_a_trial_that_differs_only_in_optional_records_gets_the_goldens_digest(
    trial: dict[str, Any], gets_the_goldens_digest: bool
) -> None:
    view = _view(_RELEASED_HOLDS, _ZERO_ALLOCATIONS)
    initial = {"transfer_holds": [], "transfer_equipment": [], "recovery_expense_decisions": []}

    def digest(state: dict[str, Any]) -> str:
        viewed = apply_comparison_view(state, initial=initial, view=view, id_fields={})
        return compute_stable_hash(viewed.state)

    assert compute_stable_hash(trial) != compute_stable_hash(_A0_GOLDEN)
    assert (digest(trial) == digest(_A0_GOLDEN)) is gets_the_goldens_digest


# ---------------------------------------------------------------------------
# Runner-reachable
# ---------------------------------------------------------------------------


def test_the_module_depends_on_the_standard_library_and_pydantic_only() -> None:
    """The runner applies the view too, so the module must not reach an orchestrator-only file."""
    tree = ast.parse(Path(comparison_view.__file__).read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            roots.add("<relative>" if node.level else (node.module or "").partition(".")[0])
    assert roots - set(sys.stdlib_module_names) - {"pydantic"} == set()
