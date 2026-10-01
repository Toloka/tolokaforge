"""Steps 1–3 of the pre-hash order, the one composition both substrates call.

``view_the_pair`` runs the view on each full state, golden first, then the unstable
filter (table names resolved as the db-service resolves them, re-keyed ids left in),
then the ``compare_columns`` pipeline. What the substrates hash afterwards is theirs.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from tolokaforge.core.grading.comparison_view import (
    ComparisonViewCollision,
    ComparisonViewConfig,
    ComparisonViewError,
)
from tolokaforge.core.grading.pre_hash import (
    PreHashDeclaration,
    TrialCollision,
    ViewedPair,
    comparison_view_grade_record,
    comparison_view_reason,
    resolve_unstable_fields,
    view_the_pair,
)
from tolokaforge.core.grading.state_checks import state_digest
from tolokaforge.core.hash import ColumnCompareRule, compute_stable_hash
from tolokaforge.runner.models import ComparisonViewGradeRecord

pytestmark = pytest.mark.unit

_DOCUMENTS_BY_SOURCE = {
    "kind": "normalize_ids",
    "table": "documents",
    "key": ["source_id"],
    "references": [{"table": "corrections", "field": "document_ref"}],
}
_LOOKUPS = {"kind": "exclude_tables", "tables": ["lookup_log"], "reason": "read tools write it"}

_INITIAL: dict[str, Any] = {
    "documents": [{"id": "D1", "source_id": "S1", "created_at": "t0"}],
    "corrections": [],
    "lookup_log": [],
}


def _declaration(*rules: dict[str, Any], **fields: Any) -> PreHashDeclaration:
    view = ComparisonViewConfig.model_validate({"version": 1, "rules": list(rules)})
    return PreHashDeclaration(view=view, **fields)


def _state(new_id: str, *, created_at: str, reason: str = "typo") -> dict[str, Any]:
    return {
        "documents": [
            {"id": "D1", "source_id": "S1", "created_at": "t0"},
            {"id": new_id, "source_id": "S2", "created_at": created_at},
        ],
        "corrections": [{"id": "C1", "document_ref": new_id, "reason": reason}],
        "lookup_log": [],
    }


def _viewed(trial: dict, golden: dict, declaration: PreHashDeclaration) -> ViewedPair:
    outcome = view_the_pair(trial, golden, initial=_INITIAL, declaration=declaration)
    assert isinstance(outcome, ViewedPair)
    return outcome


_UNSTABLE = ("documents.id", "documents.created_at", "corrections.id")


def test_a_generated_id_and_its_references_view_alike_and_the_rest_of_the_mask_applies() -> None:
    declaration = _declaration(_DOCUMENTS_BY_SOURCE, unstable_fields=_UNSTABLE)
    pair = _viewed(_state("D3", created_at="t9"), _state("D2", created_at="t5"), declaration)
    assert pair.trial == pair.golden
    new_key = 'documents:{"source_id":"S2"}'
    assert pair.trial["documents"][1] == {"id": new_key, "source_id": "S2"}
    assert pair.trial["corrections"] == [{"document_ref": new_key, "reason": "typo"}]


def test_a_re_keyed_id_reaches_the_hash_although_unstable_fields_names_it() -> None:
    """Dropping it would let a reference to the wrong record pass."""
    declaration = _declaration(_DOCUMENTS_BY_SOURCE, unstable_fields=_UNSTABLE)
    pair = _viewed(_state("D3", created_at="t9"), _state("D2", created_at="t5"), declaration)
    assert all("id" in row for row in pair.trial["documents"])
    assert pair.trial_record.rekeyed_fields == pair.golden_record.rekeyed_fields
    assert [field.dotted for field in pair.golden_record.rekeyed_fields] == ["documents.id"]


def test_a_difference_the_view_keeps_still_tells_the_pair_apart() -> None:
    declaration = _declaration(_DOCUMENTS_BY_SOURCE, unstable_fields=_UNSTABLE)
    pair = _viewed(
        _state("D3", created_at="t9", reason="wrong client"),
        _state("D2", created_at="t5"),
        declaration,
    )
    assert compute_stable_hash(pair.trial) != compute_stable_hash(pair.golden)


def test_an_unstable_table_name_resolves_as_the_db_service_resolves_it() -> None:
    declaration = _declaration(
        _DOCUMENTS_BY_SOURCE, unstable_fields=("document.created_at", "documents.id")
    )
    pair = _viewed(_state("D3", created_at="t9"), _state("D2", created_at="t5"), declaration)
    assert all("created_at" not in row for row in pair.trial["documents"])
    assert pair.trial == pair.golden


def test_unstable_names_resolve_against_the_full_states_before_the_view_drops_a_table() -> None:
    """``holds.note`` names the dropped ``holds``, never the ``transfer_holds`` left behind."""
    declaration = _declaration(
        {"kind": "exclude_tables", "tables": ["holds"], "reason": "dropped whole"},
        unstable_fields=("holds.note",),
    )
    state = {"holds": [{"id": 1, "note": "a"}], "transfer_holds": [{"id": 2, "note": "b"}]}
    pair = _viewed(state, copy.deepcopy(state), declaration)
    assert pair.trial == {"transfer_holds": [{"id": 2, "note": "b"}]}
    assert resolve_unstable_fields(["holds.note"], {"transfer_holds": []}) == (
        "transfer_holds.note",
    ), "the resolution the view must not see: after the drop, the suffix match moves"


def test_a_permutation_sorts_after_the_unstable_filter() -> None:
    """``order: unordered`` must not sort by a generated id (#1670), on the view path too."""
    declaration = _declaration(
        _LOOKUPS,
        unstable_fields=("rows.a_id",),
        compare_columns={"rows": {"x": ColumnCompareRule(order="unordered")}},
        id_fields={"rows": "a_id"},
    )
    trial = {"rows": [{"a_id": 1, "x": "B"}, {"a_id": 2, "x": "A"}], "lookup_log": []}
    golden = {"rows": [{"a_id": 5, "x": "A"}, {"a_id": 6, "x": "B"}], "lookup_log": []}
    pair = view_the_pair(
        trial, golden, initial={"rows": [], "lookup_log": []}, declaration=declaration
    )
    assert isinstance(pair, ViewedPair)
    assert pair.trial == pair.golden == {"rows": [{"x": "A"}, {"x": "B"}]}


def test_no_input_is_mutated() -> None:
    declaration = _declaration(_DOCUMENTS_BY_SOURCE, _LOOKUPS, unstable_fields=_UNSTABLE)
    trial, golden, initial = (
        _state("D3", created_at="t9"),
        _state("D2", created_at="t5"),
        copy.deepcopy(_INITIAL),
    )
    before = copy.deepcopy((trial, golden, initial))
    view_the_pair(trial, golden, initial=initial, declaration=declaration)
    assert (trial, golden, initial) == before


# ---------------------------------------------------------------------------
# Errors: the golden side first
# ---------------------------------------------------------------------------


def _two_documents_one_source(table: str = "documents") -> dict[str, Any]:
    return {
        "documents": [
            {"id": "D1", "source_id": "S1", "created_at": "t0"},
            {"id": "D2", "source_id": "S9", "created_at": "t1"},
            {"id": "D3", "source_id": "S9", "created_at": "t2"},
        ],
        "corrections": [],
        "lookup_log": [],
    }


def test_a_trial_collision_after_the_goldens_view_is_returned_for_the_caller_to_fail() -> None:
    declaration = _declaration(_DOCUMENTS_BY_SOURCE)
    outcome = view_the_pair(
        _two_documents_one_source(),
        _state("D2", created_at="t5"),
        initial=_INITIAL,
        declaration=declaration,
    )
    assert isinstance(outcome, TrialCollision)
    assert set(outcome.collision.ids) == {"D2", "D3"}
    record = comparison_view_grade_record(outcome, matched=False)
    assert record.trial is None and record.view_diff is None
    assert record.trial_collision is not None
    assert set(record.trial_collision.ids) == {"D2", "D3"}
    reason = comparison_view_reason(record)
    assert reason is not None and reason.startswith("Comparison view: the trial's state cannot")


def test_a_golden_collision_is_raised_whatever_the_trial_holds() -> None:
    with pytest.raises(ComparisonViewCollision):
        view_the_pair(
            _state("D2", created_at="t5"),
            _two_documents_one_source(),
            initial=_INITIAL,
            declaration=_declaration(_DOCUMENTS_BY_SOURCE),
        )


def test_a_golden_view_error_is_raised_before_the_trial_is_viewed() -> None:
    golden = _state("D2", created_at="t5")
    del golden["documents"][1]["source_id"]
    with pytest.raises(ComparisonViewError, match="lacks the key field"):
        view_the_pair(
            _two_documents_one_source(),
            golden,
            initial=_INITIAL,
            declaration=_declaration(_DOCUMENTS_BY_SOURCE),
        )


def test_a_trial_view_error_that_is_not_a_collision_is_raised() -> None:
    trial = _state("D3", created_at="t9")
    del trial["documents"][1]["source_id"]
    with pytest.raises(ComparisonViewError, match="lacks the key field") as raised:
        view_the_pair(
            trial,
            _state("D2", created_at="t5"),
            initial=_INITIAL,
            declaration=_declaration(_DOCUMENTS_BY_SOURCE),
        )
    assert not isinstance(raised.value, ComparisonViewCollision)


# ---------------------------------------------------------------------------
# What a grade records
# ---------------------------------------------------------------------------


def test_a_match_records_both_views_and_no_diff() -> None:
    declaration = _declaration(_DOCUMENTS_BY_SOURCE, unstable_fields=_UNSTABLE)
    pair = _viewed(_state("D3", created_at="t9"), _state("D2", created_at="t5"), declaration)
    record = comparison_view_grade_record(pair, matched=True)
    assert record.golden == pair.golden_record and record.trial == pair.trial_record
    assert record.view_diff is None and record.trial_collision is None
    assert comparison_view_reason(record) is None


def test_a_mismatch_records_the_diff_of_the_views_as_the_hash_reads_them() -> None:
    declaration = _declaration(_DOCUMENTS_BY_SOURCE, unstable_fields=_UNSTABLE)
    pair = _viewed(
        _state("D3", created_at="t9", reason="wrong client"),
        _state("D2", created_at="t5"),
        declaration,
    )
    record = comparison_view_grade_record(pair, matched=False)
    assert record.view_diff is not None
    assert set(record.view_diff.tables) == {"corrections"}, "generated values are not a diff"
    (different,) = record.view_diff.tables["corrections"].different
    assert {"field": "reason", "expected": "typo", "actual": "wrong client"} in different[
        "field_diffs"
    ]
    assert comparison_view_reason(record) == f"Comparison view: {record.view_diff.summary}"


def test_the_record_round_trips_through_its_json() -> None:
    declaration = _declaration(_DOCUMENTS_BY_SOURCE, unstable_fields=_UNSTABLE)
    pair = _viewed(_state("D3", created_at="t9"), _state("D2", created_at="t5"), declaration)
    record = comparison_view_grade_record(pair, matched=False)
    assert ComparisonViewGradeRecord.model_validate_json(record.model_dump_json()) == record


def test_a_record_carries_a_trial_view_or_a_collision_never_both() -> None:
    declaration = _declaration(_DOCUMENTS_BY_SOURCE)
    pair = _viewed(_state("D2", created_at="t5"), _state("D2", created_at="t5"), declaration)
    with pytest.raises(ValidationError, match="exactly one of the two"):
        ComparisonViewGradeRecord(golden=pair.golden_record)
    with pytest.raises(ValidationError, match="exactly one of the two"):
        ComparisonViewGradeRecord(
            golden=pair.golden_record,
            trial=pair.trial_record,
            trial_collision={"message": "m", "ids": [1, 2]},
        )


# ---------------------------------------------------------------------------
# #1444 on the view pair: a mismatched digest of the views has a non-identical diff
# ---------------------------------------------------------------------------

_SOURCES = st.sampled_from(["S1", "S2", "S3"])
_REASONS = st.sampled_from(["typo", "wrong client"])


@st.composite
def _filed_state(draw) -> dict[str, Any]:
    """The seeded document, new documents under drawn ids, corrections citing any of them."""
    sources = draw(st.lists(_SOURCES, max_size=3, unique=True))
    ids = draw(
        st.lists(
            st.integers(min_value=2, max_value=9),
            min_size=len(sources),
            max_size=len(sources),
            unique=True,
        )
    )
    documents = [{"id": "D1", "source_id": "S0"}] + [
        {"id": f"D{number}", "source_id": source} for number, source in zip(ids, sources)
    ]
    cited = st.sampled_from([document["id"] for document in documents] + ["D99"])
    corrections = [
        {"id": f"C{index}", "document_ref": draw(cited), "reason": draw(_REASONS)}
        for index in range(draw(st.integers(min_value=0, max_value=2)))
    ]
    return {"documents": documents, "corrections": corrections, "lookup_log": []}


@given(_filed_state(), _filed_state())
@settings(max_examples=200, deadline=None)
def test_a_mismatched_digest_of_the_views_comes_with_a_non_identical_view_diff(
    trial: dict[str, Any], golden: dict[str, Any]
) -> None:
    """On either substrate's algebra: the digest is of the views, so is the diff."""
    outcome = view_the_pair(
        trial,
        golden,
        initial={"documents": [{"id": "D1", "source_id": "S0"}], "corrections": []},
        declaration=_declaration(
            _DOCUMENTS_BY_SOURCE, unstable_fields=("documents.id", "corrections.id")
        ),
    )
    assert isinstance(outcome, ViewedPair)
    runner_mismatch = compute_stable_hash(outcome.trial) != compute_stable_hash(outcome.golden)
    core_mismatch = state_digest(outcome.trial) != state_digest(outcome.golden)
    assert runner_mismatch is core_mismatch, "the two algebras disagree on equality"
    if not runner_mismatch:
        return
    record = comparison_view_grade_record(outcome, matched=False)
    assert record.view_diff is not None and not record.view_diff.identical
    assert record.view_diff.summary != "States match"
