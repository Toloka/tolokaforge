"""``normalize_ids``: both key forms, the rendered key, scope, references and every guard.

The algebraic guarantees (bijective, references follow, idempotent, independent of
row order, a renamed trial gets the golden's digest) are property tests in
``test_comparison_view_normalize_ids_properties.py``.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from tolokaforge.core.grading.comparison_view import (
    ComparisonViewConfig,
    ComparisonViewError,
    ComparisonViewResult,
    RekeyedField,
    RuleApplication,
    apply_comparison_view,
)
from tolokaforge.core.grading.state_checks import state_digest
from tolokaforge.core.hash import compute_stable_hash, filter_unstable_fields

pytestmark = pytest.mark.unit

_KEY = ["account_id", "fee_id", "delta"]


def _normalize(table: str = "journal", **extra: Any) -> dict[str, Any]:
    return {"kind": "normalize_ids", "table": table, **({"key": _KEY} | extra)}


def _apply(
    state: dict[str, Any],
    *rules: dict[str, Any],
    initial: dict[str, Any] | None = None,
    id_fields: dict[str, str | list[str]] | None = None,
) -> ComparisonViewResult:
    view = ComparisonViewConfig.model_validate({"version": 1, "rules": list(rules)})
    return apply_comparison_view(
        state, initial={} if initial is None else initial, view=view, id_fields=id_fields or {}
    )


def _raises(fragment: str) -> Any:
    return pytest.raises(ComparisonViewError, match=re.escape(fragment))


def _refusal(*rules: dict[str, Any]) -> str:
    with pytest.raises(ValidationError) as caught:
        ComparisonViewConfig.model_validate({"version": 1, "rules": list(rules)})
    return str(caught.value)


def _entry(entry_id: Any, account: str = "A1", fee: str = "F1", delta: Any = -5) -> dict[str, Any]:
    return {"id": entry_id, "account_id": account, "fee_id": fee, "delta": delta}


_A1_KEY = 'journal:{"account_id":"A1","delta":-5,"fee_id":"F1"}'


# ---------------------------------------------------------------------------
# The key form
# ---------------------------------------------------------------------------


def test_new_records_get_their_content_key_and_references_follow() -> None:
    journal = [_entry("J0", account="A0"), _entry("FCJ-7"), _entry("FCJ-8", fee="F2")]
    notices = [
        {"id": "N1", "entry": "FCJ-7", "entries": ["FCJ-8", "J0"], "lines": [{"entry": "FCJ-7"}]},
        {"id": "N2", "entry": "FCJ-99", "entries": [], "lines": []},
    ]
    references = [
        {"table": "notices", "field": "entry"},
        {"table": "notices", "field": "entries"},
        {"table": "notices", "field": "lines.entry"},
    ]
    result = _apply(
        {"journal": journal, "notices": notices},
        _normalize(references=references),
        initial={"journal": [_entry("J0", account="A0")]},
    )
    f2_key = 'journal:{"account_id":"A1","delta":-5,"fee_id":"F2"}'
    assert [row["id"] for row in result.state["journal"]] == ["J0", _A1_KEY, f2_key]
    assert result.state["notices"] == [
        {"id": "N1", "entry": _A1_KEY, "entries": [f2_key, "J0"], "lines": [{"entry": _A1_KEY}]},
        {"id": "N2", "entry": "FCJ-99", "entries": [], "lines": []},
    ]
    assert result.record.applied == (
        RuleApplication(
            kind="normalize_ids",
            table="journal",
            rows_removed=0,
            ids_rewritten=2,
            references_rewritten=3,
        ),
    )


@pytest.mark.parametrize(
    ("delta", "rendered"),
    [
        (-5.0, 'journal:{"account_id":"A1","delta":-5,"fee_id":"F1"}'),
        (2.5, 'journal:{"account_id":"A1","delta":2.5,"fee_id":"F1"}'),
        (True, 'journal:{"account_id":"A1","delta":true,"fee_id":"F1"}'),
        (None, 'journal:{"account_id":"A1","delta":null,"fee_id":"F1"}'),
        ("−5 €", 'journal:{"account_id":"A1","delta":"−5 €","fee_id":"F1"}'),
    ],
    ids=["integral-float", "float", "bool", "null", "non-ascii"],
)
def test_the_key_renders_as_canonical_json_of_the_key_fields(delta: Any, rendered: str) -> None:
    result = _apply({"journal": [_entry("FCJ-1", delta=delta)]}, _normalize())
    assert result.state["journal"][0]["id"] == rendered


def test_the_initial_states_records_keep_their_ids_and_references_to_them() -> None:
    journal = [_entry("J0"), _entry("FCJ-1", fee="F2")]
    result = _apply(
        {"journal": journal, "notices": [{"id": "N1", "entry": "J0"}]},
        _normalize(references=[{"table": "notices", "field": "entry"}]),
        initial={"journal": [_entry("J0")]},
    )
    assert result.state["journal"][0] == _entry("J0")
    assert result.state["notices"] == [{"id": "N1", "entry": "J0"}]


def test_scope_all_re_keys_the_initial_states_records_too() -> None:
    result = _apply(
        {"journal": [_entry("J0")]},
        _normalize(scope="all"),
        initial={"journal": [_entry("J0")]},
    )
    assert result.state["journal"][0]["id"] == _A1_KEY


def test_scope_all_does_not_need_the_initial_state() -> None:
    view = ComparisonViewConfig.model_validate({"version": 1, "rules": [_normalize(scope="all")]})
    result = apply_comparison_view(
        {"journal": [_entry("J0")]}, initial=None, view=view, id_fields={}
    )
    assert result.state["journal"][0]["id"] == _A1_KEY


def test_without_the_table_in_the_initial_state_every_record_is_new() -> None:
    result = _apply({"journal": [_entry("J0")]}, _normalize(), initial={"notices": []})
    assert result.state["journal"][0]["id"] == _A1_KEY


def test_a_reference_within_the_table_follows_its_record() -> None:
    journal = [_entry("FCJ-1"), {**_entry("FCJ-2", fee="F2"), "reverses": "FCJ-1"}]
    result = _apply(
        {"journal": journal}, _normalize(references=[{"table": "journal", "field": "reverses"}])
    )
    assert result.state["journal"][1]["reverses"] == _A1_KEY


def test_the_id_field_comes_from_id_fields() -> None:
    journal = [
        {"entry_ref": "R1", "id": "keep-me", "account_id": "A1", "fee_id": "F1", "delta": -5}
    ]
    result = _apply({"journal": journal}, _normalize(), id_fields={"journal": ["entry_ref"]})
    assert result.state["journal"][0] == {**journal[0], "entry_ref": _A1_KEY}


def test_a_table_the_state_does_not_hold_is_left_alone() -> None:
    result = _apply({"notices": []}, _normalize())
    assert result.state == {"notices": []}
    assert result.record.applied == (
        RuleApplication(kind="normalize_ids", table="journal", rows_removed=0),
    )


def test_a_second_application_changes_nothing() -> None:
    state = {"journal": [_entry("FCJ-1")], "notices": [{"id": "N1", "entry": "FCJ-1"}]}
    rule = _normalize(references=[{"table": "notices", "field": "entry"}])
    once = _apply(state, rule)
    twice = _apply(once.state, rule)
    assert twice.state == once.state
    assert twice.record.applied[0].ids_rewritten == 0
    assert twice.record.applied[0].references_rewritten == 0


# ---------------------------------------------------------------------------
# A re-keyed id reaches the hash
# ---------------------------------------------------------------------------

_HOLDS_VIEW = {
    "kind": "normalize_ids",
    "table": "holds",
    "rank_by": ["created_at"],
    "references": [{"table": "disputes", "field": "hold_id"}],
}
_GOLDEN_HOLDS = {
    "holds": [
        {"id": "H1", "card": "A", "created_at": "10:00:01"},
        {"id": "H2", "card": "B", "created_at": "10:00:02"},
    ],
    "disputes": [{"id": "D1", "hold_id": "H1"}],
}
#: The dispute names the hold of card B, which ranks first because it was created first.
_TRIAL_HOLDS = {
    "holds": [
        {"id": "H7", "card": "A", "created_at": "10:05:09"},
        {"id": "H6", "card": "B", "created_at": "10:05:08"},
    ],
    "disputes": [{"id": "D1", "hold_id": "H6"}],
}


def test_a_re_keyed_id_reaches_the_digest_even_when_unstable_fields_names_it() -> None:
    initial = {"holds": [], "disputes": []}
    golden = _apply(_GOLDEN_HOLDS, _HOLDS_VIEW, initial=initial)
    trial = _apply(_TRIAL_HOLDS, _HOLDS_VIEW, initial=initial)
    assert (
        golden.rekeyed_fields == trial.rekeyed_fields == (RekeyedField(table="holds", field="id"),)
    )
    unstable = ["holds.id", "holds.created_at"]
    after_the_view = [
        name for name in unstable if name not in {f.dotted for f in golden.rekeyed_fields}
    ]
    assert after_the_view == ["holds.created_at"]
    assert compute_stable_hash(filter_unstable_fields(golden.state, after_the_view)) != (
        compute_stable_hash(filter_unstable_fields(trial.state, after_the_view))
    )
    assert state_digest(golden.state, unstable_fields=after_the_view) != state_digest(
        trial.state, unstable_fields=after_the_view
    )
    # Why the masks must not drop it: with the id masked the wrong dispute passes.
    assert state_digest(golden.state, unstable_fields=unstable) == state_digest(
        trial.state, unstable_fields=unstable
    )


def test_the_re_keyed_fields_follow_the_declaration_not_the_records_in_scope() -> None:
    absent = _apply({"notices": []}, _normalize(), id_fields={"journal": "entry_ref"})
    assert absent.rekeyed_fields == (RekeyedField(table="journal", field="entry_ref"),)
    nothing_new = _apply(
        {"journal": [_entry("J0")]}, _normalize(), initial={"journal": [_entry("J0")]}
    )
    assert nothing_new.rekeyed_fields == (RekeyedField(table="journal", field="id"),)
    exclude_only = _apply(
        {"journal": []}, {"kind": "exclude_tables", "tables": ["journal"], "reason": "r"}
    )
    assert exclude_only.rekeyed_fields == ()


# ---------------------------------------------------------------------------
# The ordinal form
# ---------------------------------------------------------------------------


def _posting(entry_id: str, account: str, posted_at: Any) -> dict[str, Any]:
    return {"id": entry_id, "account_id": account, "posted_at": posted_at}


def test_the_ordinal_form_numbers_records_within_their_group_by_rank() -> None:
    journal = [
        _posting("FCJ-9", "A1", "2026-09-02"),
        _posting("FCJ-3", "A2", "2026-09-01"),
        _posting("FCJ-5", "A1", "2026-09-01"),
    ]
    result = _apply(
        {"journal": journal},
        {
            "kind": "normalize_ids",
            "table": "journal",
            "ordinal_by": ["account_id"],
            "rank_by": ["posted_at"],
        },
    )
    assert [row["id"] for row in result.state["journal"]] == [
        'journal:{"account_id":"A1"}#2',
        'journal:{"account_id":"A2"}#1',
        'journal:{"account_id":"A1"}#1',
    ]


def test_without_ordinal_by_the_whole_scope_is_one_group() -> None:
    journal = [_posting("FCJ-2", "A1", 20), _posting("FCJ-1", "A2", 10)]
    result = _apply(
        {"journal": journal},
        {"kind": "normalize_ids", "table": "journal", "rank_by": ["posted_at"]},
    )
    assert [row["id"] for row in result.state["journal"]] == ["journal:{}#2", "journal:{}#1"]


def test_the_rank_orders_null_bools_numbers_then_strings() -> None:
    ranks = ["b", 2.5, None, True, "a", 1, False]
    journal = [_posting(f"FCJ-{n}", "A1", rank) for n, rank in enumerate(ranks)]
    result = _apply(
        {"journal": journal},
        {"kind": "normalize_ids", "table": "journal", "rank_by": ["posted_at"]},
    )
    ordinals = [int(row["id"].rpartition("#")[2]) for row in result.state["journal"]]
    assert ordinals == [7, 5, 1, 3, 6, 4, 2]


def test_ordinals_count_only_the_records_in_scope() -> None:
    journal = [_posting("J0", "A1", 1), _posting("FCJ-1", "A1", 2)]
    result = _apply(
        {"journal": journal},
        {
            "kind": "normalize_ids",
            "table": "journal",
            "ordinal_by": ["account_id"],
            "rank_by": ["posted_at"],
        },
        initial={"journal": [_posting("J0", "A1", 1)]},
    )
    assert [row["id"] for row in result.state["journal"]] == ["J0", 'journal:{"account_id":"A1"}#1']


@pytest.mark.parametrize(("first", "second"), [(3, 3), (3, 3.0)], ids=["equal", "int-float"])
def test_a_rank_tie_is_refused_naming_both_records(first: Any, second: Any) -> None:
    journal = [_posting("FCJ-1", "A1", first), _posting("FCJ-2", "A1", second)]
    with _raises("'FCJ-1' and 'FCJ-2' of table 'journal' tie"):
        _apply(
            {"journal": journal},
            {"kind": "normalize_ids", "table": "journal", "rank_by": ["posted_at"]},
        )


# ---------------------------------------------------------------------------
# Collisions
# ---------------------------------------------------------------------------


def test_two_records_with_one_key_are_refused_naming_both() -> None:
    with _raises("record 'FCJ-2' of table 'journal' gets the key"):
        _apply({"journal": [_entry("FCJ-1"), _entry("FCJ-2")]}, _normalize())
    with _raises("which record 'FCJ-1' already holds"):
        _apply({"journal": [_entry("FCJ-1"), _entry("FCJ-2")]}, _normalize())


def test_a_key_a_kept_record_already_holds_is_refused() -> None:
    journal = [_entry(_A1_KEY, account="A0"), _entry("FCJ-1")]
    with _raises(f"gets the key {_A1_KEY!r}, which record {_A1_KEY!r} already holds"):
        _apply({"journal": journal}, _normalize(), initial={"journal": [journal[0]]})


def test_a_reference_already_holding_a_new_key_is_refused() -> None:
    state = {"journal": [_entry("FCJ-1")], "notices": [{"id": "N1", "entry": _A1_KEY}]}
    with _raises("the new key of another record"):
        _apply(state, _normalize(references=[{"table": "notices", "field": "entry"}]))


def test_two_records_sharing_an_id_are_refused() -> None:
    with _raises("share the id 'FCJ-1'"):
        _apply({"journal": [_entry("FCJ-1"), _entry("FCJ-1", fee="F2")]}, _normalize())


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entry", "fragment"),
    [
        ({"kind": "normalize_ids", "table": "journal"}, "exactly one of the two"),
        ({"kind": "normalize_ids", "table": "journal", "key": []}, "exactly one of the two"),
        (_normalize(rank_by=["posted_at"]), "exactly one of the two"),
        (
            {"kind": "normalize_ids", "table": "journal", "ordinal_by": ["account_id"]},
            "exactly one of the two",
        ),
        (_normalize(ordinal_by=["account_id"]), "it does not combine with key"),
        (
            {
                "kind": "normalize_ids",
                "table": "journal",
                "ordinal_by": ["account_id"],
                "rank_by": ["account_id", "posted_at"],
            },
            "ordinal_by and rank_by both list ['account_id']",
        ),
        (_normalize(key=["fee_id", "fee_id"]), "lists a field more than once"),
        (_normalize(key=["payload.fee_id"]), "'payload.fee_id' names a field of the record"),
        (_normalize(scope="initial_records"), "Input should be 'new_records' or 'all'"),
        (_normalize(references=[{"table": "notices"}]), "field: Field required"),
        (_normalize(references=[{"table": "notices", "field": "a..b"}]), "empty segment"),
        (_normalize(reason=" "), "reason: must not be blank"),
        (_normalize(table=""), "table: must not be blank"),
        (_normalize(order="unordered"), "order: Extra inputs are not permitted"),
    ],
)
def test_a_malformed_entry_is_refused(entry: dict[str, Any], fragment: str) -> None:
    assert fragment in _refusal(entry)


def test_a_table_is_normalized_by_one_rule() -> None:
    message = _refusal(_normalize(), _normalize(key=["fee_id"]))
    assert "rules[0] and rules[1] both normalize the ids of table 'journal'" in message


@pytest.mark.parametrize("dropped", ["journal", "notices"])
def test_exclude_tables_refuses_a_table_normalize_ids_names(dropped: str) -> None:
    message = _refusal(
        _normalize(references=[{"table": "notices", "field": "entry"}]),
        {"kind": "exclude_tables", "tables": [dropped], "reason": "r"},
    )
    assert f"drops table(s) ['{dropped}'] that rules[0] (normalize_ids) also names" in message


@pytest.mark.parametrize(
    ("rule", "id_fields", "fragment"),
    [
        (_normalize(), {"journal": ["account_id", "id"]}, "normalize_ids needs one id field"),
        (_normalize(key=["id", "fee_id"]), {}, "key names journal.id, the id field it replaces"),
        (
            {
                "kind": "normalize_ids",
                "table": "journal",
                "ordinal_by": ["id"],
                "rank_by": ["delta"],
            },
            {},
            "ordinal_by names journal.id",
        ),
        (
            {"kind": "normalize_ids", "table": "journal", "rank_by": ["entry_ref"]},
            {"journal": "entry_ref"},
            "rank_by names journal.entry_ref",
        ),
        (
            _normalize(references=[{"table": "journal", "field": "id"}]),
            {},
            "references names journal.id, the id field of the rule's own table",
        ),
    ],
    ids=["composite-key", "id-in-key", "id-in-ordinal_by", "id-in-rank_by", "own-id-reference"],
)
def test_a_declaration_the_id_field_contradicts_is_refused(
    rule: dict[str, Any], id_fields: dict[str, Any], fragment: str
) -> None:
    with _raises(fragment):
        _apply({"journal": [_entry("FCJ-1")]}, rule, id_fields=id_fields)


@pytest.mark.parametrize(
    ("journal", "fragment"),
    [
        (
            [{"account_id": "A1", "fee_id": "F1", "delta": 1}],
            "its id field 'id' is missing or null",
        ),
        ([_entry(None)], "its id field 'id' is missing or null"),
        ([_entry({"n": 1})], "holds a dict in its id field 'id'"),
        ([_entry(float("-inf"))], "holds -inf in its id field 'id'"),
        ([{"id": "FCJ-1", "account_id": "A1", "delta": 1}], "lacks the key field(s) ['fee_id']"),
        ([_entry("FCJ-1", delta=[1])], "holds a list in key field 'delta'"),
        ([_entry("FCJ-1", delta=date(2026, 1, 1))], "holds a date in key field 'delta'"),
        ([_entry("FCJ-1", delta=float("nan"))], "holds nan in key field 'delta'"),
        ({"FCJ-1": _entry("FCJ-1")}, "normalize_ids: table 'journal' holds a dict"),
    ],
    ids=[
        "missing-id",
        "null-id",
        "mapping-id",
        "infinite-id",
        "missing-key-field",
        "list-key-value",
        "date-key-value",
        "nan-key-value",
        "table-not-a-list",
    ],
)
def test_a_record_that_cannot_be_re_keyed_is_refused(journal: Any, fragment: str) -> None:
    with _raises(fragment):
        _apply({"journal": journal}, _normalize())


@pytest.mark.parametrize(
    ("notices", "fragment"),
    [
        (
            [{"id": "N1", "entry": {"id": "FCJ-1"}}],
            "references: 'notices.entry' holds a dict, not an id",
        ),
        ([{"id": "N1", "entry": {"FCJ-1"}}], "holds a set, not an id"),
        ({"N1": {"entry": "FCJ-1"}}, "references: table 'notices' holds a dict"),
        ([{"id": "N1", "entry": "x", "lines": "none"}], "field 'entry' is read from a str"),
    ],
    ids=["mapping", "set", "table-not-a-list", "scalar-on-the-way"],
)
def test_a_reference_that_is_not_an_id_is_refused(notices: Any, fragment: str) -> None:
    rule = _normalize(
        references=[
            {"table": "notices", "field": "entry"},
            {"table": "notices", "field": "lines.entry"},
        ]
    )
    with _raises(fragment):
        _apply({"journal": [_entry("FCJ-1")], "notices": notices}, rule)


def test_new_records_scope_needs_the_initial_state() -> None:
    view = ComparisonViewConfig.model_validate({"version": 1, "rules": [_normalize()]})
    with _raises("scope new_records, which reads the initial state"):
        apply_comparison_view({"journal": [_entry("FCJ-1")]}, initial=None, view=view, id_fields={})


def test_an_initial_table_that_is_not_a_list_is_refused() -> None:
    with _raises("normalize_ids: initial table 'journal' holds a dict"):
        _apply({"journal": [_entry("FCJ-1")]}, _normalize(), initial={"journal": {}})
