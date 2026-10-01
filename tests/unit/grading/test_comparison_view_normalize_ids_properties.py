"""Property tests of ``normalize_ids`` (ADR-0053 § Tests), for both key forms.

A journal of records with generated ids, some of them in the initial state, and
notices that reference them at the top level, in a list and at a dotted path, or
reference nothing (a dangling id). Generated ids, dangling references and the
ids a renamed trial uses come from disjoint alphabets, so every property is
exact.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tolokaforge.core.grading.comparison_view import (
    ComparisonViewConfig,
    ComparisonViewError,
    apply_comparison_view,
)
from tolokaforge.core.hash import compute_stable_hash, filter_unstable_fields

pytestmark = pytest.mark.unit

_PROPERTY = settings(max_examples=200, deadline=None)

_GENERATED_ID = st.text(alphabet="FCJ-0123456789", min_size=1, max_size=5)
_DANGLING_ID = st.text(alphabet="XYZ", min_size=1, max_size=3)
_CONTENT = st.tuples(
    st.sampled_from(["A1", "A2", "A3"]), st.sampled_from(["F1", "F2"]), st.integers(-2, 2)
)
_KEY = ["account_id", "fee_id", "delta"]
_REFERENCES = [
    {"table": "notices", "field": "entry"},
    {"table": "notices", "field": "entries"},
    {"table": "notices", "field": "lines.entry"},
]


def _view(**form: Any) -> ComparisonViewConfig:
    rule = {"kind": "normalize_ids", "table": "journal", "references": _REFERENCES, **form}
    return ComparisonViewConfig.model_validate({"version": 1, "rules": [rule]})


_KEY_VIEW = _view(key=_KEY)
_ORDINAL_VIEW = _view(ordinal_by=["account_id"], rank_by=["posted_at"])
_BOTH_FORMS = pytest.mark.parametrize("view", [_KEY_VIEW, _ORDINAL_VIEW], ids=["key", "ordinal"])


@dataclass(frozen=True)
class _Case:
    """A state, its initial state, and how many leading journal records the latter holds."""

    state: dict[str, Any]
    initial: dict[str, Any]
    kept: int

    @property
    def journal(self) -> list[dict[str, Any]]:
        return self.state["journal"]

    @property
    def new_ids(self) -> list[str]:
        return [row["id"] for row in self.journal[self.kept :]]


@st.composite
def _cases(draw: st.DrawFn, *, min_records: int = 0, min_new: int = 0) -> _Case:
    contents = draw(st.lists(_CONTENT, unique=True, min_size=max(min_records, min_new), max_size=8))
    size = len(contents)
    ids = draw(st.lists(_GENERATED_ID, unique=True, min_size=size, max_size=size))
    ranks = draw(st.permutations(range(size)))
    journal = [
        {"id": i, "account_id": a, "fee_id": f, "delta": d, "posted_at": r, "memo": f"m{n}"}
        for n, (i, (a, f, d), r) in enumerate(zip(ids, contents, ranks))
    ]
    kept = draw(st.integers(0, size - min_new))
    target = st.sampled_from(ids) | _DANGLING_ID if ids else _DANGLING_ID
    notice = st.fixed_dictionaries(
        {
            "entry": target | st.none(),
            "entries": st.lists(target, max_size=3),
            "lines": st.lists(st.fixed_dictionaries({"entry": target}), max_size=2),
        }
    )
    notices = [{"id": f"N{n}", **body} for n, body in enumerate(draw(st.lists(notice, max_size=4)))]
    initial = {"journal": copy.deepcopy(journal[:kept])}
    return _Case(state={"journal": journal, "notices": notices}, initial=initial, kept=kept)


def _viewed(state: dict[str, Any], case: _Case, view: ComparisonViewConfig) -> dict[str, Any]:
    return apply_comparison_view(state, initial=case.initial, view=view, id_fields={}).state


def _followed(notice: dict[str, Any], renamed: dict[Any, Any]) -> dict[str, Any]:
    """``notice`` with every reference value renamed through ``renamed``, the rest as is."""

    def follow(value: Any) -> Any:
        return value if value is None else renamed.get(value, value)

    return {
        **notice,
        "entry": follow(notice["entry"]),
        "entries": [follow(value) for value in notice["entries"]],
        "lines": [{**line, "entry": follow(line["entry"])} for line in notice["lines"]],
    }


def _renamed_trial(case: _Case, fresh: dict[str, str]) -> dict[str, Any]:
    """The golden with its generated ids, and every reference to them, renamed."""
    journal = [{**row, "id": fresh.get(row["id"], row["id"])} for row in case.journal]
    notices = [_followed(notice, fresh) for notice in case.state["notices"]]
    return {"journal": journal, "notices": notices}


def _fresh_ids(case: _Case, order: list[int]) -> dict[str, str]:
    return {old: f"T{position}" for old, position in zip(case.new_ids, order)}


# ---------------------------------------------------------------------------
# Bijective, references follow, dangling ones stay
# ---------------------------------------------------------------------------


@_BOTH_FORMS
@given(case=_cases())
@_PROPERTY
def test_distinct_records_stay_distinct(view: ComparisonViewConfig, case: _Case) -> None:
    ids = [row["id"] for row in _viewed(case.state, case, view)["journal"]]
    assert len(set(ids)) == len(ids) == len(case.journal)


@_BOTH_FORMS
@given(case=_cases())
@_PROPERTY
def test_every_listed_reference_follows_its_record(view: ComparisonViewConfig, case: _Case) -> None:
    viewed = _viewed(case.state, case, view)
    renamed = {old["id"]: new["id"] for old, new in zip(case.journal, viewed["journal"])}
    assert viewed["notices"] == [_followed(notice, renamed) for notice in case.state["notices"]]


@_BOTH_FORMS
@given(case=_cases())
@_PROPERTY
def test_a_dangling_reference_stays_as_it_is(view: ComparisonViewConfig, case: _Case) -> None:
    ids = {row["id"] for row in case.journal}
    viewed = _viewed(case.state, case, view)
    for before, after in zip(case.state["notices"], viewed["notices"]):
        if before["entry"] not in ids:
            assert after["entry"] == before["entry"]
        pairs = zip(before["entries"], after["entries"])
        assert all(new == old for old, new in pairs if old not in ids)


@given(case=_cases(min_new=2), data=st.data())
@_PROPERTY
def test_two_new_records_with_one_key_raise(case: _Case, data: st.DataObject) -> None:
    first, second = data.draw(
        st.lists(
            st.sampled_from(range(case.kept, len(case.journal))),
            min_size=2,
            max_size=2,
            unique=True,
        )
    )
    state = copy.deepcopy(case.state)
    state["journal"][second].update({field: state["journal"][first][field] for field in _KEY})
    with pytest.raises(ComparisonViewError, match="gets the key"):
        _viewed(state, case, _KEY_VIEW)


@given(case=_cases(min_new=2), data=st.data())
@_PROPERTY
def test_two_new_records_tied_on_their_rank_raise(case: _Case, data: st.DataObject) -> None:
    first, second = data.draw(
        st.lists(
            st.sampled_from(range(case.kept, len(case.journal))),
            min_size=2,
            max_size=2,
            unique=True,
        )
    )
    state = copy.deepcopy(case.state)
    tied = {field: state["journal"][first][field] for field in ("account_id", "posted_at")}
    state["journal"][second].update(tied)
    with pytest.raises(ComparisonViewError, match="tie on rank_by"):
        _viewed(state, case, _ORDINAL_VIEW)


# ---------------------------------------------------------------------------
# Idempotent, independent of row order, initial records keep their keys
# ---------------------------------------------------------------------------


@_BOTH_FORMS
@given(case=_cases())
@_PROPERTY
def test_the_view_is_idempotent(view: ComparisonViewConfig, case: _Case) -> None:
    once = _viewed(case.state, case, view)
    assert _viewed(once, case, view) == once


def _rows(state: dict[str, Any]) -> dict[str, list[str]]:
    return {
        table: sorted(json.dumps(row, sort_keys=True) for row in rows)
        for table, rows in state.items()
    }


@_BOTH_FORMS
@given(case=_cases(), data=st.data())
@_PROPERTY
def test_the_view_does_not_depend_on_row_order(
    view: ComparisonViewConfig, case: _Case, data: st.DataObject
) -> None:
    journal_order = data.draw(st.permutations(range(len(case.journal))))
    notice_order = data.draw(st.permutations(range(len(case.state["notices"]))))
    permuted = {
        "journal": [case.journal[i] for i in journal_order],
        "notices": [case.state["notices"][i] for i in notice_order],
    }
    assert _rows(_viewed(permuted, case, view)) == _rows(_viewed(case.state, case, view))


@_BOTH_FORMS
@given(case=_cases())
@_PROPERTY
def test_the_initial_states_records_keep_their_keys(
    view: ComparisonViewConfig, case: _Case
) -> None:
    viewed = _viewed(case.state, case, view)
    kept_before = [row["id"] for row in case.journal[: case.kept]]
    assert [row["id"] for row in viewed["journal"][: case.kept]] == kept_before


# ---------------------------------------------------------------------------
# The digest: generated ids do not count, everything else does
# ---------------------------------------------------------------------------


@_BOTH_FORMS
@given(case=_cases(), data=st.data())
@_PROPERTY
def test_a_trial_differing_only_in_generated_ids_gets_the_goldens_digest(
    view: ComparisonViewConfig, case: _Case, data: st.DataObject
) -> None:
    fresh = _fresh_ids(case, data.draw(st.permutations(range(len(case.new_ids)))))
    trial = _renamed_trial(case, fresh)
    golden_digest = compute_stable_hash(_viewed(case.state, case, view))
    assert compute_stable_hash(_viewed(trial, case, view)) == golden_digest


def _masked_digest(state: dict[str, Any], case: _Case, view: ComparisonViewConfig) -> str:
    """view → the unstable filter without the re-keyed id fields → the hash."""
    result = apply_comparison_view(state, initial=case.initial, view=view, id_fields={})
    rekeyed = {field.dotted for field in result.rekeyed_fields}
    unstable = [name for name in ("journal.id", "journal.posted_at") if name not in rekeyed]
    return compute_stable_hash(filter_unstable_fields(result.state, unstable))


@_BOTH_FORMS
@given(case=_cases(), data=st.data())
@_PROPERTY
def test_with_the_id_declared_unstable_a_renamed_trial_still_gets_the_goldens_digest(
    view: ComparisonViewConfig, case: _Case, data: st.DataObject
) -> None:
    fresh = _fresh_ids(case, data.draw(st.permutations(range(len(case.new_ids)))))
    trial = _renamed_trial(case, fresh)
    assert _masked_digest(trial, case, view) == (_masked_digest(case.state, case, view))


@given(case=_cases(min_new=2), data=st.data())
@_PROPERTY
def test_with_the_id_declared_unstable_a_reference_to_the_wrong_record_still_fails(
    case: _Case, data: st.DataObject
) -> None:
    """Two records of one group swap ranks and references: only the re-keyed id tells them apart.

    The rank field is masked too, so after the view nothing but the re-keyed id
    links a record's content to the key its references carry.
    """
    first, second = data.draw(
        st.lists(
            st.sampled_from(range(case.kept, len(case.journal))),
            min_size=2,
            max_size=2,
            unique=True,
        )
    )
    golden = copy.deepcopy(case.state)
    golden["journal"][second]["account_id"] = golden["journal"][first]["account_id"]
    golden["notices"].append(
        {"id": "N-wrong", "entry": golden["journal"][first]["id"], "entries": [], "lines": []}
    )
    trial = copy.deepcopy(golden)
    one, other = trial["journal"][first], trial["journal"][second]
    one["posted_at"], other["posted_at"] = other["posted_at"], one["posted_at"]
    swapped = {one["id"]: other["id"], other["id"]: one["id"]}
    trial["notices"] = [_followed(notice, swapped) for notice in trial["notices"]]
    assert _masked_digest(trial, case, _ORDINAL_VIEW) != _masked_digest(golden, case, _ORDINAL_VIEW)


def _changed_elsewhere(trial: dict[str, Any], change: str, index: int, ids: list[str]) -> None:
    """Change ``trial`` in one place that is not a generated id or a reference to one."""
    journal, notices = trial["journal"], trial["notices"]
    row = journal[index % len(journal)]
    if change == "memo":
        row["memo"] = "changed"
    elif change == "delta":
        row["delta"] = 99
    elif change == "dangling-reference" and notices:
        notices[index % len(notices)]["entry"] = "ZZZZ"
    elif change == "other-record" and notices and len(ids) > 1:
        notice = notices[index % len(notices)]
        notice["entries"] = [*notice["entries"], ids[index % len(ids)]]
    else:
        row["memo"] = "changed"


@_BOTH_FORMS
@given(
    case=_cases(min_records=1),
    data=st.data(),
    change=st.sampled_from(["memo", "delta", "dangling-reference", "other-record"]),
    index=st.integers(0, 100),
)
@_PROPERTY
def test_a_trial_differing_anywhere_else_does_not(
    view: ComparisonViewConfig, case: _Case, data: st.DataObject, change: str, index: int
) -> None:
    fresh = _fresh_ids(case, data.draw(st.permutations(range(len(case.new_ids)))))
    trial = _renamed_trial(case, fresh)
    _changed_elsewhere(trial, change, index, [row["id"] for row in trial["journal"]])
    golden_digest = compute_stable_hash(_viewed(case.state, case, view))
    assert compute_stable_hash(_viewed(trial, case, view)) != golden_digest
