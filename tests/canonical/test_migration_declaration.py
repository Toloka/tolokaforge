"""The shipped declarations, read off disk, and the refusals stated against real packs.

Every ``migration.yaml`` under ``examples/`` is enumerated from the filesystem and put
through ``inspect_migration_declaration`` against the ``grading.yaml`` beside it, so a
shipped declaration the pack contradicts — a renamed criterion, a dropped constraint id, a
mode the pack's rules refuse — reds here rather than reaching whoever reads the migration
report. The enumeration is pinned, because a guard over a subset says nothing about the
declarations it skipped.

The two candidacies are then compared entry by entry against the entry written out here:
the file on disk is one source, this module is the other, so drift on either side reds
instead of the two agreeing on a criterion the rubric no longer holds.

``cache_debug`` carries a ``required`` criterion beside a shared gate, a shared scored
constraint and two routes' worth of route-scoped ones, so the veto rule's refusals are
stated against a real block rather than a fixture. What a hand-built pack adds — the
route-scoped *gate*, which no shipped pack declares — is in
``tests/unit/grading/test_migration_declaration.py``. The narrowed arms' own entries are
locked, with the corpus they rest on, in ``tests/canonical/test_rubric_migration.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, get_args

import pytest

from tests.utils.migration_packs import write_migration_pack
from tolokaforge.core.grading.migration_declaration import (
    EVERY_DECLARED_FIELD,
    MIGRATION_FILENAME,
    MigratedCriterion,
    MigrationEntry,
    criterion_shape_disagreement,
    inspect_migration_declaration,
)
from tolokaforge.core.models import Criterion

pytestmark = pytest.mark.canonical

_REPO = Path(__file__).resolve().parents[2]
_EXAMPLES = _REPO / "examples"

_LOT_OPS = _REPO / "examples/native/multi_service_lot_ops/dataset/tasks/lot_ops_01/grading.yaml"
_CACHE_DEBUG = (
    _REPO / "examples/native/multi_service_cache_debug/dataset/tasks/cache_debug/grading.yaml"
)
_NOTES_TESTCASES = _EXAMPLES / "native/native_shared_domain/dataset/notes/testcases"

# Every criterion the shipped declarations claim, by the task directory declaring it. The
# criterion is the join between a declaration and the rubric it migrates, so an entry
# re-pointed at another criterion the pack happens to hold reds here and nowhere else.
_SHIPPED_CRITERIA = {
    _LOT_OPS.parent: ("names_lot",),
    _CACHE_DEBUG.parent: ("explains_mechanism",),
    _NOTES_TESTCASES / "add_note_duplicate_check_gated": ("checked_duplicates_first",),
    _NOTES_TESTCASES / "add_note_duplicate_check_policy": ("checked_duplicates_first",),
}

_NAMES_LOT = "The report identifies the affected lot (LOT-1007 or lot 7)."
_EXPLAINS_MECHANISM = (
    "Explains that the write updates the database but does not invalidate the cached order "
    "key, so cache-first reads serve the stale value."
)
_IDENTIFIES_BUG_ID = "identifies_bug"


_SHARED_CRITERION_FIELDS = ("kind", "required", "weight")


def test_was_carries_the_rubric_criterion_shape_field_for_field(tmp_path: Path) -> None:
    """``was`` describes a rubric criterion, so the two must agree on the value set and on
    every default — read off the two models rather than written out here.

    The failure mode if they drift is not a confusing message but a *wrong verdict*: the
    ``candidate`` rule compares ``was`` against the criterion field by field, so a default
    that differs on one side refuses a declaration in which the author wrote nothing wrong.
    """
    declared = MigratedCriterion.model_fields
    current = Criterion.model_fields

    assert set(_SHARED_CRITERION_FIELDS) <= set(declared) & set(current)
    assert get_args(declared["kind"].annotation) == get_args(current["kind"].annotation)
    assert get_args(declared["kind"].annotation) == ("binary", "graded")
    for name in _SHARED_CRITERION_FIELDS:
        assert declared[name].default == current[name].default, name


def _inspect(tmp_path: Path, pack: Path, *entries: dict[str, Any]):
    return inspect_migration_declaration(
        write_migration_pack(
            tmp_path / "case",
            grading_text=pack.read_text(),
            migration={"migrations": list(entries)},
        )
    )


def _lot_ops_candidacy(**overrides: Any) -> dict[str, Any]:
    """``lot_ops_01``'s shipped candidacy, as the entry its header points at."""
    return {
        "criterion": "names_lot",
        "mode": "candidate",
        "by": [
            "the_reason_code_posted_was_read_from_the_catalog",
            "the_lot_was_read_before_the_action_was_opened",
        ],
        "was": {
            "description": _NAMES_LOT,
            "kind": "binary",
            "required": True,
            "weight": 1.0,
        },
    } | overrides


def _cache_debug_candidacy(**overrides: Any) -> dict[str, Any]:
    """``cache_debug``'s shipped candidacy, as the entry its header points at."""
    return {
        "criterion": "explains_mechanism",
        "mode": "candidate",
        "by": [
            "the_note_quotes_the_value_the_served_read_returned",
            "the_note_quotes_the_value_the_cache_held",
        ],
        "was": {
            "description": _EXPLAINS_MECHANISM,
            "kind": "graded",
            "required": False,
            "weight": 1.0,
        },
    } | overrides


def test_every_shipped_declaration_is_one_the_pack_beside_it_honours() -> None:
    """Enumerated from disk rather than from a list of packs that declare something, so a
    fifth sidecar cannot ship without a criterion written out here — and the load is the
    check: ``inspect_migration_declaration`` raises on a criterion the rubric does not hold,
    a ``by`` id the ``trace_checks`` block does not declare, or a mode the pack's own shape
    refuses."""
    declaring = {path.parent for path in _EXAMPLES.rglob(MIGRATION_FILENAME)}
    assert declaring == set(_SHIPPED_CRITERIA), (
        "the shipped declarations are not the ones written out here, so a subset of them was "
        "loaded and the rest went unchecked"
    )

    declared = {}
    for task_dir in sorted(declaring):
        declaration = inspect_migration_declaration(task_dir / "grading.yaml")
        assert declaration is not None, task_dir
        declared[task_dir] = tuple(entry.criterion for entry in declaration.migrations)

    assert declared == _SHIPPED_CRITERIA


@pytest.mark.parametrize(
    ("task_dir", "written"),
    [
        (_LOT_OPS.parent, _lot_ops_candidacy()),
        (_CACHE_DEBUG.parent, _cache_debug_candidacy()),
    ],
    ids=["lot_ops_01", "cache_debug"],
)
def test_a_shipped_candidacy_declares_the_entry_its_header_points_at(
    task_dir: Path, written: dict[str, Any]
) -> None:
    """Both packs' headers point at a sidecar rather than restating the claim, so what the
    sidecar says is what a reader gets — and neither is charged the rule its shape would
    otherwise trip. ``lot_ops_01``'s ``names_lot`` is ``required: true`` while both its
    correlations are *scored*, which a narrow or a retirement is refused for; ``cache_debug``'s
    ``explains_mechanism`` is *graded*, so a conversion would owe a ``combine_weights`` map.
    A candidate replaces nothing, so it owes neither.

    ``was.description`` is compared through the same normalisation the load rule uses, the
    YAML block folding a newline onto the text the rubric declares.
    """
    declaration = inspect_migration_declaration(task_dir / "grading.yaml")
    assert declaration is not None, f"{task_dir} ships no {MIGRATION_FILENAME}"
    (entry,) = declaration.migrations
    expected = MigrationEntry(**written)

    assert entry.model_dump(exclude={"was"}) == expected.model_dump(exclude={"was"})
    assert (
        criterion_shape_disagreement(entry.was, expected.was, over=EVERY_DECLARED_FIELD) is None
    ), entry.was.description


def test_a_candidate_carrying_a_residual_is_refused(tmp_path: Path):
    """The shipped candidacy plus one line, which is the whole of the defect: a residual is
    the author's judgment about a migration that happened, so on a candidate it parks a claim
    no bar ever checks. Refusing it makes ``residual``'s presence and kind a total function of
    ``mode`` — absent, ``text``, ``none`` — so a reader can tell what an entry claims from its
    mode alone.
    """
    with pytest.raises(ValueError, match="mode: candidate declares a residual"):
        _inspect(
            tmp_path,
            _LOT_OPS,
            _lot_ops_candidacy(residual={"kind": "none", "reason": "the bindings are the whole"}),
        )


def _narrowing_a_veto(**overrides: Any) -> dict[str, Any]:
    """A ``narrowed`` entry on ``cache_debug``'s ``required`` criterion, so a refusal below
    is the veto rule's rather than another's."""
    return {
        "criterion": _IDENTIFIES_BUG_ID,
        "mode": "narrowed",
        "by": ["no_status_was_written"],
        "was": {
            "description": "Identifies the stale cached order status as the bug, and why.",
            "kind": "binary",
            "required": True,
            "weight": 1.0,
        },
        "residual": {"kind": "text", "reason": "the causal account is still judged"},
        "evidence": {"corpus": "tests/data/corpora/cache", "observations": 9, "kappa": 0.8},
    } | overrides


def test_a_narrowed_veto_claimed_by_a_shared_gate_loads(tmp_path: Path):
    """The one placement that preserves a veto: shared, so no route escapes it, and a gate,
    so it fails the trial rather than shading a component."""
    declaration = _inspect(tmp_path, _CACHE_DEBUG, _narrowing_a_veto())
    assert declaration is not None
    assert declaration.migrations[0].by == ["no_status_was_written"]


def test_a_narrowed_veto_claimed_by_a_shared_scored_constraint_is_refused(tmp_path: Path):
    with pytest.raises(ValueError, match="the_note_was_written is scored"):
        _inspect(tmp_path, _CACHE_DEBUG, _narrowing_a_veto(by=["the_note_was_written"]))


def test_a_narrowed_veto_claimed_by_a_route_scoped_constraint_is_refused(tmp_path: Path):
    """A check inside ``alternatives`` is escaped by a trajectory taking the other route, so
    it cannot carry a veto that held whatever the agent did."""
    with pytest.raises(ValueError, match="inside route 'divergence_between_the_api_layers'"):
        _inspect(
            tmp_path,
            _CACHE_DEBUG,
            _narrowing_a_veto(by=["the_note_quotes_the_value_the_served_read_returned"]),
        )


def test_the_veto_refusal_names_the_shape_to_write_instead(tmp_path: Path):
    with pytest.raises(ValueError, match="shared 'constraints:' with severity: gate"):
        _inspect(tmp_path, _CACHE_DEBUG, _narrowing_a_veto(by=["the_note_was_written"]))


def test_a_retired_criterion_the_pack_still_scores_is_refused(tmp_path: Path):
    """A retirement the rubric did not follow leaves the judge scoring what the constraints
    are said to have replaced."""
    with pytest.raises(ValueError, match="still declared in llm_judge.rubric.criteria"):
        _inspect(
            tmp_path,
            _CACHE_DEBUG,
            _narrowing_a_veto(
                mode="retired", residual={"kind": "none", "reason": "the gate is the whole of it"}
            ),
        )


def test_a_retired_entry_claiming_a_textual_residual_is_refused(tmp_path: Path):
    """One of the two directions no spelling of a bare ``residual`` string could tell apart:
    ``residual: none`` parses to the non-empty string ``'none'``, so a sentinel would satisfy
    the requirement that something remains."""
    with pytest.raises(ValueError, match="mode: retired carries residual.kind: text"):
        _inspect(
            tmp_path,
            _CACHE_DEBUG,
            _narrowing_a_veto(mode="retired", residual={"kind": "text", "reason": "the account"}),
        )


def test_a_narrowed_entry_claiming_no_residual_is_refused(tmp_path: Path):
    """The other direction: ``residual: null`` parses to ``None``, so a retirement meaning
    exactly "nothing remains" would fail a non-empty check written over one string."""
    with pytest.raises(ValueError, match="mode: narrowed carries residual.kind: none"):
        _inspect(
            tmp_path,
            _CACHE_DEBUG,
            _narrowing_a_veto(residual={"kind": "none", "reason": "nothing remains"}),
        )
