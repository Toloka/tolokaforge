"""Hash-verdict-parity contract for the runner-side state-check pair.

Locks the invariant: whenever ``compute_stable_hash(trial) !=
compute_stable_hash(golden)``, ``compute_state_diff(trial, golden)`` MUST
report ``identical is False`` and its ``summary`` MUST NOT be ``"States
match"``. Case (2) — same rows, different order — is the class the runner
substrate historically dropped: the hash captures list order, the set-based
diff does not, and an operator saw ``hash_score: 0.0`` beside a "States
match" summary with no way to tell which verdict to trust.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

pytestmark = pytest.mark.unit

from tolokaforge.core.hash import compute_stable_hash
from tolokaforge.runner.grading import compute_state_diff

# ---------------------------------------------------------------------------
# Parametrised equivalence-class cases
# ---------------------------------------------------------------------------

_ROW_A: dict[str, Any] = {"id": 1, "name": "alpha"}
_ROW_B: dict[str, Any] = {"id": 2, "name": "bravo"}


@pytest.mark.parametrize(
    "case_id, trial, golden, hash_should_match, summary_predicate, "
    "identical_expected, offending_table",
    [
        (
            "identical",
            {"users": [_ROW_A, _ROW_B]},
            {"users": [_ROW_A, _ROW_B]},
            True,
            lambda s: s == "States match",
            True,
            None,
        ),
        (
            "same_rows_different_order",
            {"users": [_ROW_A, _ROW_B]},
            {"users": [_ROW_B, _ROW_A]},
            False,
            lambda s: s != "States match",
            False,
            "users",
        ),
        (
            "genuinely_different_rows",
            {"users": [_ROW_A]},
            {"users": [_ROW_B]},
            False,
            lambda s: "1 missing" in s and "1 extra" in s,
            False,
            None,
        ),
        (
            "empty_trial_nonempty_golden",
            {"users": []},
            {"users": [_ROW_A]},
            False,
            lambda s: "1 missing" in s,
            False,
            None,
        ),
        (
            "nonempty_trial_empty_golden",
            {"users": [_ROW_A]},
            {"users": []},
            False,
            lambda s: "1 extra" in s,
            False,
            None,
        ),
    ],
)
def test_hash_verdict_parity_cases(
    case_id: str,
    trial: dict[str, Any],
    golden: dict[str, Any],
    hash_should_match: bool,
    summary_predicate,
    identical_expected: bool,
    offending_table: str | None,
) -> None:
    """Every equivalence class agrees on the hash-vs-diff verdict."""
    hash_match = compute_stable_hash(trial) == compute_stable_hash(golden)
    assert hash_match is hash_should_match, f"[{case_id}] hash disagrees"

    diff = compute_state_diff(trial, golden)
    msg_summary = f"[{case_id}] unexpected summary: {diff.summary!r}"
    assert summary_predicate(diff.summary), msg_summary
    msg_identical = f"[{case_id}] identical={diff.identical}, want {identical_expected}"
    assert diff.identical is identical_expected, msg_identical

    if offending_table is not None:
        table_diff = diff.tables.get(offending_table)
        assert table_diff is not None, (
            f"[{case_id}] expected offending table {offending_table!r} to be "
            f"reported in diff.tables, got {sorted(diff.tables)}"
        )
        assert getattr(table_diff, "order_mismatch", False) is True, (
            f"[{case_id}] expected TableDiff.order_mismatch to be True on the "
            f"same-rows-different-order class; got "
            f"{getattr(table_diff, 'order_mismatch', 'MISSING')!r}"
        )


# ---------------------------------------------------------------------------
# Property: hash mismatch implies diff-not-identical
# ---------------------------------------------------------------------------

_row_strategy = st.fixed_dictionaries(
    {
        "id": st.integers(min_value=0, max_value=8),
        "name": st.text(
            alphabet=st.characters(min_codepoint=97, max_codepoint=122),
            min_size=1,
            max_size=3,
        ),
    }
)


@st.composite
def _related_table_pair(
    draw,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """Generate a (trial, golden) pair related by shuffle-or-mutate.

    Rows are drawn unique-by-content so a shuffle produces an observably
    different list. Shuffle and mutate are drawn with equal probability; the
    strategy shrinks toward the small shuffled-rows case, which is the class
    the runner-side diff currently drops on.
    """
    base = draw(
        st.lists(
            _row_strategy,
            min_size=0,
            max_size=5,
            unique_by=lambda r: (r["id"], r["name"]),
        )
    )
    mutate = draw(st.booleans())
    if mutate:
        # Add or remove a single row.
        if base and draw(st.booleans()):
            idx = draw(st.integers(min_value=0, max_value=len(base) - 1))
            golden = base[:idx] + base[idx + 1 :]
        else:
            extra_row = draw(_row_strategy.filter(lambda r: r not in base))
            golden = base + [extra_row]
    else:
        # Shuffle the base list (a no-op permutation is fine — the property is
        # vacuous when the hashes agree).
        perm = draw(st.permutations(list(range(len(base)))))
        golden = [base[i] for i in perm]
    return {"users": base}, {"users": golden}


@given(_related_table_pair())
@settings(max_examples=200, deadline=None)
def test_hash_mismatch_implies_diff_not_identical(
    pair: tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]],
) -> None:
    """``hash(trial) != hash(golden)`` MUST imply ``not diff.identical``."""
    trial, golden = pair
    if compute_stable_hash(trial) == compute_stable_hash(golden):
        return  # implication is vacuously true
    diff = compute_state_diff(trial, golden)
    assert not diff.identical, (
        f"hash-verdict-parity violated: hashes differ but diff reports "
        f"identical=True with summary={diff.summary!r} for "
        f"trial={trial!r} golden={golden!r}"
    )
    assert diff.summary != "States match", (
        f"hash-verdict-parity violated: hashes differ but summary reads "
        f"'States match' for trial={trial!r} golden={golden!r}"
    )
