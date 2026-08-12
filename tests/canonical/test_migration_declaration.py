"""The shipped declarations, read off disk, and the refusals stated against real packs.

Every ``migration.yaml`` under ``examples/`` is enumerated from the filesystem and put
through ``inspect_migration_declaration`` against the ``grading.yaml`` beside it, so a
shipped declaration the pack contradicts — a renamed criterion, a dropped constraint id, a
mode the pack's rules refuse — reds here rather than reaching whoever reads the migration
report. The enumeration is pinned, because a guard over a subset says nothing about the
declarations it skipped. The corpus each entry names is swept the same way and over the
fixture packs under ``tests/data/migration_packs`` too, against the repository root: the
value is read against a base the caller supplies, and the root is what the shipped values
are written from.

The shipped candidacy is then compared entry by entry against the entry written out here:
the file on disk is one source, this module is the other, so drift on either side reds
instead of the two agreeing on a criterion the rubric no longer holds.

``cache_debug`` carries a ``required`` criterion beside a shared gate, a shared scored
constraint and two routes' worth of route-scoped ones, so the veto rule's refusals are
stated against a real block rather than a fixture — and so is the one-route rule's, over
the pack whose two grounded-claim checks sit one per route. What a hand-built pack adds —
the route-scoped *gate*, which no shipped pack declares — is in
``tests/unit/grading/test_migration_declaration.py``, which also owns every rule stated over
the entry alone: the residual-kind directions and the freed-share rule need no real rubric to
be about, so re-stating them here would lock one rule twice and neither more firmly. The
narrowed arms' own entries are locked, with the corpus they rest on, in
``tests/canonical/test_rubric_migration.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, get_args

import pytest

from tests.utils.migration_packs import write_corpus_directory, write_migration_pack
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
    _NOTES_TESTCASES / "add_note_duplicate_check_gated": ("checked_duplicates_first",),
    _NOTES_TESTCASES / "add_note_duplicate_check_policy": ("checked_duplicates_first",),
}

_FIXTURE_PACKS = _REPO / "tests/data/migration_packs"
_NOTES_CORPUS = "tests/data/migration_corpora/notes_duplicate_check"
_LOT_OPS_CORPUS = "tests/data/migration_corpora/lot_ops_names_lot"

# The corpus every declaration in the tree names, by the task directory declaring it. Written
# from the repository root, which is what the sweep below reads them against — and written out
# here because a re-pointed corpus is a claim measured over other trials.
_DECLARED_CORPORA = {
    _LOT_OPS.parent: (_LOT_OPS_CORPUS,),
    _NOTES_TESTCASES / "add_note_duplicate_check_gated": (_NOTES_CORPUS,),
    _NOTES_TESTCASES / "add_note_duplicate_check_policy": (_NOTES_CORPUS,),
    _FIXTURE_PACKS / "notes_duplicate_check_narrowed": (_NOTES_CORPUS,),
    _FIXTURE_PACKS / "notes_duplicate_check_policy_narrowed": (_NOTES_CORPUS,),
}

_NAMES_LOT = "The report identifies the affected lot (LOT-1007 or lot 7)."
_EXPLAINS_MECHANISM = (
    "Explains that the write updates the database but does not invalidate the cached order "
    "key, so cache-first reads serve the stale value."
)
_IDENTIFIES_BUG_ID = "identifies_bug"

_CORPUS = "corpus"
"""What a hand-built entry names, written beside the copied pack so the pointer resolves."""


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


def test_the_compared_field_set_is_every_field_was_declares_and_no_other() -> None:
    """:data:`EVERY_DECLARED_FIELD` is *derived* from the model, so what needs locking is the
    derivation's two consequences rather than the list.

    The set is written out here because a field added to ``was`` silently joins two comparisons
    — the load-time cross-check and the recorded-rubric verification — and that is a contract
    change, not a model tidy-up: a reviewer should see this literal move with it.

    And every one of those fields must have a counterpart on the rubric criterion, because the
    comparison reads both sides by the same names. A field with no counterpart raises
    ``AttributeError`` at comparison time rather than dropping out of it, which is the intended
    loudness — this assertion is where it is caught before a pack ever meets it.
    """
    assert set(EVERY_DECLARED_FIELD) == {"description", "kind", "required", "weight"}
    assert set(EVERY_DECLARED_FIELD) <= set(Criterion.model_fields)


def _inspect(tmp_path: Path, pack: Path, *entries: dict[str, Any]):
    """The pack copied into ``tmp_path``, with the corpus its entry names written beside it.

    The corpus is written rather than nominal because the load resolves it: against the
    declaration's own directory, which is where a copied pack's is.
    """
    directory = tmp_path / "case"
    write_corpus_directory(directory / _CORPUS, criterion=entries[0]["criterion"])
    return inspect_migration_declaration(
        write_migration_pack(
            directory,
            grading_text=pack.read_text(),
            migration={"migrations": list(entries)},
        )
    )


def _lot_ops_candidacy(**overrides: Any) -> dict[str, Any]:
    """``lot_ops_01``'s shipped candidacy, as the entry its header points at."""
    return {
        "criterion": "names_lot",
        "mode": "candidate",
        # The lot correlation alone. `by` is a conjunction, and the pack's other correlation is
        # about the reason code — a different proposition, which a trial could get wrong while
        # grounding the lot correctly, so naming it would count that against the lot's claim.
        "by": ["the_lot_was_read_before_the_action_was_opened"],
        "corpus": _LOT_OPS_CORPUS,
        "was": {
            "description": _NAMES_LOT,
            "kind": "binary",
            "required": True,
            "weight": 1.0,
        },
    } | overrides


def _cache_debug_candidacy(**overrides: Any) -> dict[str, Any]:
    """A claim on ``explains_mechanism`` by one grounded-claim check from each of the pack's
    two routes — the shape the one-route rule refuses, written against the real block whose
    routes make it unmeasurable."""
    return {
        "criterion": "explains_mechanism",
        "mode": "candidate",
        "by": [
            "the_note_quotes_the_value_the_served_read_returned",
            "the_note_quotes_the_value_the_cache_held",
        ],
        "corpus": _CORPUS,
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
        declaration = inspect_migration_declaration(task_dir / "grading.yaml", corpus_base=_REPO)
        assert declaration is not None, task_dir
        declared[task_dir] = tuple(entry.criterion for entry in declaration.migrations)

    assert declared == _SHIPPED_CRITERIA


def test_every_declaration_in_the_tree_names_a_corpus_that_resolves() -> None:
    """The load *is* the check: an entry whose corpus resolves to no corpus directory is
    refused, so a sidecar that loads here is one whose evidence a reader can go and read.

    Both roots, with no exceptions list — the fixture packs under ``tests/data`` declare the
    same criterion under their own weight map and are as capable of pointing at a corpus
    nobody wrote. The base is the repository root because that is where the shipped values
    are written from, and it is passed rather than inherited from the working directory.
    """
    named = {}
    for root in (_EXAMPLES, _FIXTURE_PACKS):
        for sidecar in sorted(root.rglob(MIGRATION_FILENAME)):
            declaration = inspect_migration_declaration(
                sidecar.parent / "grading.yaml", corpus_base=_REPO
            )
            assert declaration is not None, sidecar
            named[sidecar.parent] = tuple(entry.corpus for entry in declaration.migrations)

    assert named == _DECLARED_CORPORA


@pytest.mark.parametrize(
    ("task_dir", "written"),
    [(_LOT_OPS.parent, _lot_ops_candidacy())],
    ids=["lot_ops_01"],
)
def test_a_shipped_candidacy_declares_the_entry_its_header_points_at(
    task_dir: Path, written: dict[str, Any]
) -> None:
    """The pack's header points at a sidecar rather than restating the claim, so what the
    sidecar says is what a reader gets — and it is not charged the rule its shape would
    otherwise trip: ``names_lot`` is ``required: true`` while both the pack's correlations are
    *scored*, which a narrow or a retirement is refused for. A candidate replaces nothing, so
    it owes nothing.

    ``was.description`` is compared through the same normalisation the load rule uses, the
    YAML block folding a newline onto the text the rubric declares.
    """
    declaration = inspect_migration_declaration(task_dir / "grading.yaml", corpus_base=_REPO)
    assert declaration is not None, f"{task_dir} ships no {MIGRATION_FILENAME}"
    (entry,) = declaration.migrations
    expected = MigrationEntry(**written)

    assert entry.model_dump(exclude={"was"}) == expected.model_dump(exclude={"was"})
    assert (
        criterion_shape_disagreement(entry.was, expected.was, over=EVERY_DECLARED_FIELD) is None
    ), entry.was.description


def test_a_by_spanning_the_packs_two_routes_is_refused(tmp_path: Path):
    """``cache_debug`` is the pack the rule is about: its two grounded-claim checks sit one
    per route, because no single read is common to both diagnostic routes. A trial is scored
    on the route it took, so a conjunction over the pair has no verdict for one of its ids on
    every trial and could never be measured — which is what the refusal names, both ids with
    the route each sits in."""
    with pytest.raises(ValueError) as refused:
        _inspect(tmp_path, _CACHE_DEBUG, _cache_debug_candidacy())

    written = str(refused.value)
    assert (
        "the_note_quotes_the_value_the_served_read_returned in route "
        "'divergence_between_the_api_layers'" in written
    ), written
    assert (
        "the_note_quotes_the_value_the_cache_held in route 'divergence_against_the_cache'"
        in written
    ), written


def test_a_by_naming_one_routes_grounded_claim_check_loads(tmp_path: Path):
    """The accepting direction against the same block: one route's check is decided on every
    trial that route won, so the rule refuses the span rather than the route scope."""
    declaration = _inspect(
        tmp_path,
        _CACHE_DEBUG,
        _cache_debug_candidacy(by=["the_note_quotes_the_value_the_cache_held"]),
    )
    assert declaration is not None
    assert declaration.migrations[0].by == ["the_note_quotes_the_value_the_cache_held"]


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
        "corpus": _CORPUS,
        "was": {
            "description": "Identifies the stale cached order status as the bug, and why.",
            "kind": "binary",
            "required": True,
            "weight": 1.0,
        },
        "residual": {"kind": "text", "reason": "the causal account is still judged"},
        "evidence": {"observations": 9, "kappa": 0.8},
    } | overrides


def test_a_narrowed_veto_claimed_by_a_shared_gate_loads(tmp_path: Path):
    """The one placement that preserves a veto: shared, so no route escapes it, and a gate,
    so it fails the trial rather than shading a component."""
    declaration = _inspect(tmp_path, _CACHE_DEBUG, _narrowing_a_veto())
    assert declaration is not None
    assert declaration.migrations[0].by == ["no_status_was_written"]


def test_a_narrowed_veto_claimed_by_a_shared_scored_constraint_is_refused(tmp_path: Path):
    """One refusal, and both halves of what it has to say: which constraint is too weak and why,
    and the shape to write instead. Split across two tests these built the identical pack twice
    and asserted over the identical message."""
    with pytest.raises(ValueError) as raised:
        _inspect(tmp_path, _CACHE_DEBUG, _narrowing_a_veto(by=["the_note_was_written"]))

    assert "the_note_was_written is scored" in str(raised.value)
    assert "shared 'constraints:' with severity: gate" in str(raised.value)


def test_a_narrowed_veto_claimed_by_a_route_scoped_constraint_is_refused(tmp_path: Path):
    """A check inside ``alternatives`` is escaped by a trajectory taking the other route, so
    it cannot carry a veto that held whatever the agent did."""
    with pytest.raises(ValueError, match="inside route 'divergence_between_the_api_layers'"):
        _inspect(
            tmp_path,
            _CACHE_DEBUG,
            _narrowing_a_veto(by=["the_note_quotes_the_value_the_served_read_returned"]),
        )


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
