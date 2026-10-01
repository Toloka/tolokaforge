"""Core drops ``unstable_fields`` before the ``compare_columns`` pipeline, on every path.

The runner's db-service drops the unstable columns in ``get_stable_state``, before its
pipeline runs, so ``order: unordered`` sorts rows without them. Core ran the pipeline
first: the sort then ordered rows by a generated id that sorts before the columns
that count, and a trial whose rows were re-created under new ids in another order
missed in core only. ``tests/canonical/test_hash_unstable_fields_before_compare_columns.py``
holds the two substrates together; this module covers core's three entry points.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.core.grading.golden_replay import GoldenReplayRecord
from tolokaforge.core.grading.state_checks import StateChecker, state_digest
from tolokaforge.core.hash import (
    ColumnCompareRule,
    apply_compare_columns_pipeline,
    filter_unstable_fields,
)

pytestmark = pytest.mark.unit

_UNSTABLE = ["rows.id"]
_RULES = {"rows": {"label": ColumnCompareRule(order="unordered")}}
_EXPECTED = {"rows": [{"id": "R1", "label": "A"}, {"id": "R2", "label": "B"}]}
#: The expected rows re-created under new ids, in the other order.
_REORDERED = {"rows": [{"id": "R8", "label": "B"}, {"id": "R9", "label": "A"}]}
#: The same, with one label that is really different.
_CHANGED = {"rows": [{"id": "R8", "label": "B"}, {"id": "R9", "label": "C"}]}


@pytest.mark.parametrize(("state", "score"), [(_REORDERED, 1.0), (_CHANGED, 0.0)])
def test_check_hash_against_an_expected_state(state, score) -> None:
    got, reason, _ = StateChecker().check_hash(
        state, expected_state=_EXPECTED, compare_columns=_RULES, unstable_fields=_UNSTABLE
    )
    assert got == score, reason


@pytest.mark.parametrize(("state", "score"), [(_REORDERED, 1.0), (_CHANGED, 0.0)])
def test_check_hash_against_a_stored_digest(state, score) -> None:
    """The legacy shape: the caller's digest of the expected side, filtered first."""
    filtered = filter_unstable_fields(_EXPECTED, _UNSTABLE)
    _, expected_processed = apply_compare_columns_pipeline(filtered, filtered, _RULES)
    got, reason, _ = StateChecker().check_hash(
        state,
        expected_hash=state_digest(expected_processed),
        compare_columns=_RULES,
        expected_state_for_pipeline=_EXPECTED,
        unstable_fields=_UNSTABLE,
    )
    assert got == score, reason


@pytest.mark.parametrize(("state", "score"), [(_REORDERED, 1.0), (_CHANGED, 0.0)])
def test_check_hash_against_a_golden_replay(
    state, score, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checker = StateChecker()
    monkeypatch.setattr(
        checker,
        "_execute_golden_actions",
        lambda *args, **kwargs: (
            {"rows": [dict(row) for row in _EXPECTED["rows"]]},
            GoldenReplayRecord(authored=0),
        ),
    )
    got, reason, diff, _, _ = checker.check_hash_against_golden_replay(
        db_state=state,
        golden_actions=[],
        task_dir=tmp_path,
        initial_state_path="initial_state.json",
        mcp_server_path="mcp_server.py",
        task_domain="test",
        compare_columns=_RULES,
        unstable_fields=_UNSTABLE,
    )
    assert got == score, reason
    assert (diff is None) is (score == 1.0)
