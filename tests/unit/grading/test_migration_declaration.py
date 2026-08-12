"""What a ``migration.yaml`` may claim about its pack's rubric, and what it is refused for.

Every rejection here is an authoring defect that no later stage recovers from: nothing
about grading reads the file, so a claim the pack contradicts is never discovered by a
trial and would instead reach ``tolokaforge reconcile`` as evidence gathered against text
the pack no longer has.

The rules below are the reason the sidecar's shape is what it is:

* the **freed-share rule** — a scored criterion in ``narrowed`` / ``retired`` mode must
  declare ``combine_weights``, unconditionally. What the declared map *does* to any trial is
  not checkable here — ``validate`` sees a pack and no corpus — so every assertion below is
  about the declaration, and the standing case for the author who shifts nothing is that an
  identity map loads.
* the **veto rule** — a ``required: true`` criterion may only be replaced by a **shared**
  ``severity: gate`` constraint. Both halves of that carry weight independently, so the
  four cells of {shared, route-scoped} × {gate, scored} are each a standing case: the one
  the shipped corpus cannot supply is a route-scoped *gate*, which is escapable by a
  trajectory taking another route and is therefore the cell that proves "shared" is doing
  work of its own.
* the **narrowed cross-check** — the veto rule reads the author's ``was``, so a ``narrowed``
  entry's ``was.required`` and ``was.kind`` must equal the criterion the pack still holds.
  ``was.weight`` is deliberately free, and the standing case below that keeps the rule from
  over-reaching is a narrow reducing it.
* the **one-route rule** — the route-scoped ids one entry is ``by`` must sit in one route,
  since a trial's recomputed verdicts carry the shared constraints and the winning route's
  alone. The two standing cases are a same-route pair and a shared id beside a route-scoped
  one, which keep the rule off a conjunction a corpus can decide.

What a shipped pack's candidacy looks like, and the rejections stated against a real
rubric, are in ``tests/canonical/test_migration_declaration.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from tests.utils.migration_packs import write_migration_pack
from tolokaforge.core.grading.migration_declaration import (
    MigrationMode,
    inspect_migration_declaration,
)

pytestmark = pytest.mark.unit

_VETOING_DESCRIPTION = "The assistant listed the notes before adding one."
_SCORED_DESCRIPTION = "The saved note captures what the user asked for."


def _present(tool: str) -> dict[str, Any]:
    return {"present": {"match": {"kind": "tool_call", "tool": {"equals": tool}}}}


def _grading_text() -> str:
    """One pack carrying every constraint placement the veto rule distinguishes."""
    return yaml.safe_dump(
        {
            "combine": {"method": "weighted", "weights": {"llm_judge": 1.0}},
            "llm_judge": {
                "rubric": {
                    "criteria": [
                        {
                            "id": "checked_first",
                            "description": _VETOING_DESCRIPTION,
                            "kind": "binary",
                            "required": True,
                            "weight": 1.0,
                        },
                        {
                            "id": "note_saved",
                            "description": _SCORED_DESCRIPTION,
                            "kind": "graded",
                            "weight": 1.0,
                        },
                    ]
                }
            },
            "trace_checks": {
                "constraints": [
                    {
                        "id": "shared_gate",
                        "description": "list_notes was called",
                        "severity": "gate",
                        "require": _present("list_notes"),
                    },
                    {
                        "id": "shared_scored",
                        "description": "add_note was called",
                        "require": _present("add_note"),
                    },
                ],
                "alternatives": [
                    {
                        "id": "by_search",
                        "description": "the notes were found by searching",
                        "constraints": [
                            {
                                "id": "route_gate",
                                "description": "search_notes was called",
                                "severity": "gate",
                                "require": _present("search_notes"),
                            },
                            {
                                "id": "route_scored",
                                "description": "get_note was called",
                                "require": _present("get_note"),
                            },
                        ],
                    },
                    {
                        "id": "by_listing",
                        "description": "the notes were found by listing",
                        "constraints": [
                            {
                                "id": "route_scored_other",
                                "description": "list_notes was called",
                                "require": _present("list_notes"),
                            }
                        ],
                    },
                ],
            },
        }
    )


def _entry(**overrides: Any) -> dict[str, Any]:
    """A well-formed ``narrowed`` entry on the vetoing criterion, so a rejection below is
    the one under test."""
    return {
        "criterion": "checked_first",
        "mode": "narrowed",
        "by": ["shared_gate"],
        "was": {
            "description": _VETOING_DESCRIPTION,
            "kind": "binary",
            "required": True,
            "weight": 1.0,
        },
        "residual": {"kind": "text", "reason": "the warning to the user is still judged"},
        "evidence": {"corpus": "tests/data/corpora/notes", "observations": 11, "kappa": 1.0},
    } | overrides


def _narrowing(**overrides: Any) -> dict[str, Any]:
    """The entry with the narrowed text a ``narrowed`` mode needs, since ``_entry`` quotes
    the description the rubric still declares."""
    was = _entry()["was"] | {"description": f"{_VETOING_DESCRIPTION} It also warned them."}
    return _entry(**({"was": was} | overrides))


def _narrowed_was(**fields: Any) -> dict[str, Any]:
    """The narrowed entry's ``was`` with ``fields`` written over it."""
    return _narrowing()["was"] | fields


def _candidacy(**overrides: Any) -> dict[str, Any]:
    """The entry as a candidacy: a candidate has measured nothing and concluded nothing, so
    it carries neither evidence nor a residual."""
    return _entry(**({"mode": "candidate", "evidence": None, "residual": None} | overrides))


def _inspect(tmp_path: Path, *entries: dict[str, Any]):
    return inspect_migration_declaration(
        write_migration_pack(
            tmp_path / "case",
            grading_text=_grading_text(),
            migration={"migrations": list(entries)},
        )
    )


def test_a_directory_without_a_sidecar_declares_no_migration(tmp_path: Path):
    """An unmigrated pack is unchanged: the file is optional and its absence is not a
    defect to report."""
    grading_path = write_migration_pack(tmp_path / "case", grading_text=_grading_text())
    assert inspect_migration_declaration(grading_path) is None


def test_a_well_formed_narrowed_entry_loads(tmp_path: Path):
    """The accepting half of every rejection below — one entry naming the shared gate that
    preserves the veto, against the text the rubric no longer declares."""
    declaration = _inspect(tmp_path, _narrowing())
    assert declaration is not None
    entry = declaration.migrations[0]
    assert (entry.criterion, entry.mode) == ("checked_first", MigrationMode.NARROWED)


# ---------------------------------------------------------------------------
# The veto rule: {shared, route-scoped} × {gate, scored}, one cell accepted.
# ---------------------------------------------------------------------------


def test_a_shared_gate_preserves_the_veto(tmp_path: Path):
    declaration = _inspect(tmp_path, _narrowing(by=["shared_gate"]))
    assert declaration is not None
    assert declaration.migrations[0].by == ["shared_gate"]


def test_a_shared_scored_constraint_does_not_preserve_the_veto(tmp_path: Path):
    with pytest.raises(ValueError, match="shared_scored is scored"):
        _inspect(tmp_path, _narrowing(by=["shared_scored"]))


def test_a_route_scoped_gate_does_not_preserve_the_veto(tmp_path: Path):
    """The cell the shipped corpus cannot supply. A gate inside ``alternatives`` fails only
    the trajectories that took that route, so a trajectory taking another route escapes the
    veto entirely — which is why the rule asks for shared *and* gate rather than either."""
    with pytest.raises(ValueError, match="route_gate is declared inside route 'by_search'"):
        _inspect(tmp_path, _narrowing(by=["route_gate"]))


def test_a_route_scoped_scored_constraint_does_not_preserve_the_veto(tmp_path: Path):
    with pytest.raises(ValueError, match="route_scored is declared inside route 'by_search'"):
        _inspect(tmp_path, _narrowing(by=["route_scored"]))


def test_a_candidate_naming_a_scored_constraint_keeps_its_veto_and_loads(tmp_path: Path):
    """A candidacy replaces nothing, so the criterion still vetoes whatever it names — which
    is what the shipped candidacy is, naming a scored constraint."""
    declaration = _inspect(tmp_path, _candidacy(by=["shared_scored"]))
    assert declaration is not None
    assert declaration.migrations[0].mode is MigrationMode.CANDIDATE


# ---------------------------------------------------------------------------
# The one-route rule: what a conjunction may name across `alternatives`.
# ---------------------------------------------------------------------------


def test_a_by_naming_a_constraint_from_each_route_is_refused(tmp_path: Path):
    """A trial is scored on the route it took, so the recomputed verdicts hold the shared
    constraints and one route's — an entry naming ``by_search``'s check beside
    ``by_listing``'s has no verdict for one of them on every trial it could ever be measured
    over. The refusal names both ids with the route each sits in, because which pair spans
    the routes is the whole of what the author has to fix."""
    with pytest.raises(ValueError) as refused:
        _inspect(tmp_path, _candidacy(by=["route_scored", "route_scored_other"]))

    written = str(refused.value)
    assert "route_scored in route 'by_search'" in written, written
    assert "route_scored_other in route 'by_listing'" in written, written


def test_a_by_naming_two_constraints_of_one_route_loads(tmp_path: Path):
    """The accepting direction: both ids are decided together on every trial that route won,
    so their conjunction is a label a corpus can disagree with."""
    declaration = _inspect(tmp_path, _candidacy(by=["route_gate", "route_scored"]))
    assert declaration is not None
    assert declaration.migrations[0].by == ["route_gate", "route_scored"]


def test_a_by_mixing_a_shared_id_with_a_route_scoped_one_loads(tmp_path: Path):
    """A shared constraint is decided whichever route won, so it rules no route out and
    accompanies any route's ids."""
    declaration = _inspect(tmp_path, _candidacy(by=["shared_scored", "route_scored"]))
    assert declaration is not None
    assert declaration.migrations[0].by == ["shared_scored", "route_scored"]


# ---------------------------------------------------------------------------
# The freed-share rule, whose one requirement is the declaration.
# ---------------------------------------------------------------------------


def _scored_criterion_entry(*, description: str, **overrides: Any) -> dict[str, Any]:
    """An entry on the pack's graded criterion, whose weight sits in the judge denominator."""
    return _entry(
        criterion="note_saved",
        by=["shared_scored"],
        was={"description": description, "kind": "graded", "required": False, "weight": 1.0},
        **overrides,
    )


def _narrowing_a_scored_criterion(**overrides: Any) -> dict[str, Any]:
    return _scored_criterion_entry(
        description=f"{_SCORED_DESCRIPTION} It also read well.", **overrides
    )


def test_a_narrowed_scored_criterion_without_combine_weights_is_refused(tmp_path: Path):
    """The freed weight has nowhere declared to go, so the judge component rises on a trial
    that scored the criterion badly — measured at +0.667 on cache_debug."""
    with pytest.raises(ValueError, match="no combine_weights"):
        _inspect(tmp_path, _narrowing_a_scored_criterion())


def test_the_refusal_names_the_identity_map_as_what_shifting_nothing_writes(tmp_path: Path):
    """The rule has one requirement, so the refusal cannot send the author anywhere else: it
    names the line an author who shifts nothing writes instead."""
    with pytest.raises(ValueError, match="identity map"):
        _inspect(tmp_path, _narrowing_a_scored_criterion())


def test_a_narrowed_scored_criterion_with_combine_weights_loads(tmp_path: Path):
    """A declared map satisfies the rule outright. This asserts the declaration and nothing
    about what the map does to a trial, which needs the corpus this gate does not have."""
    declaration = _inspect(
        tmp_path,
        _narrowing_a_scored_criterion(combine_weights={"llm_judge": 0.7, "trace_checks": 0.3}),
    )
    assert declaration is not None
    assert declaration.migrations[0].combine_weights == {"llm_judge": 0.7, "trace_checks": 0.3}


def test_a_narrowed_scored_criterion_declaring_the_identity_map_loads(tmp_path: Path):
    """The standing case that keeps the unconditional rule from being unsatisfiable for an
    author who shifts nothing: the pack's own map, redeclared, absorbs no freed share and is
    accepted — the residual the rule leaves is surfaced by ``reconcile``, not gated here."""
    declaration = _inspect(
        tmp_path, _narrowing_a_scored_criterion(combine_weights={"llm_judge": 1.0})
    )
    assert declaration is not None
    assert declaration.migrations[0].combine_weights == {"llm_judge": 1.0}


def test_a_candidate_on_a_scored_criterion_needs_no_weight_map(tmp_path: Path):
    """A candidacy frees no share, having removed nothing."""
    declaration = _inspect(
        tmp_path,
        _scored_criterion_entry(
            description=_SCORED_DESCRIPTION, mode="candidate", evidence=None, residual=None
        ),
    )
    assert declaration is not None
    assert declaration.migrations[0].combine_weights is None


# ---------------------------------------------------------------------------
# What ``was`` claims against what the rubric declares.
# ---------------------------------------------------------------------------


def test_a_candidate_whose_was_is_stale_is_refused(tmp_path: Path):
    """A candidacy is against the criterion as it stands, so a ``was`` the rubric
    contradicts means whatever evidence is later quoted was gathered against other text."""
    stale = _entry()["was"] | {"description": "The assistant did something else entirely."}
    with pytest.raises(ValueError, match="was does not match"):
        _inspect(tmp_path, _candidacy(was=stale))


def test_a_candidate_whose_was_drifts_only_in_requiredness_is_refused(tmp_path: Path):
    """Every field of ``was`` is compared, not the text alone: requiredness is what the veto
    rule reads, so a stale one is the field that matters most."""
    with pytest.raises(ValueError, match="required: declared False, the rubric has True"):
        _inspect(tmp_path, _candidacy(was=_entry()["was"] | {"required": False}))


def test_a_candidate_whose_was_only_rewraps_the_text_loads(tmp_path: Path):
    """A folded YAML block appends a newline of its own and turns each of its line breaks
    into a space, so whether two authors wrote the same criterion is a question about the
    words rather than about where either one wrapped."""
    rewrapped = _entry()["was"] | {"description": f"  {_VETOING_DESCRIPTION.replace(' ', '  ')}\n"}
    declaration = _inspect(tmp_path, _candidacy(was=rewrapped))
    assert declaration is not None


def test_a_narrowed_entry_that_changed_no_text_is_refused(tmp_path: Path):
    """``_entry`` quotes the description the rubric still declares, which is exactly the
    mis-declaration: a narrow that shortened nothing narrowed nothing."""
    with pytest.raises(ValueError, match="nothing was narrowed"):
        _inspect(tmp_path, _entry())


def test_a_narrowed_entry_declaring_a_requiredness_the_rubric_does_not_have_is_refused(
    tmp_path: Path,
):
    """The bypass the cross-check closes, reproduced whole: the veto rule reads the author's
    ``was.required``, so a narrow declaring ``false`` on the pack's ``required: true``
    criterion escapes it and hands the veto to a *scored* constraint. The entry declares
    ``combine_weights``, which is what satisfies the freed-share rule ``was.required: false``
    otherwise charges — so the refusal here is the cross-check's and no other rule's.

    Asserted on the rule's own wording beside the field detail: the drift fragment alone
    appears in the *candidate* rule's message too, and this entry is ``narrowed``.
    """
    with pytest.raises(ValueError) as refused:
        _inspect(
            tmp_path,
            _narrowing(
                was=_narrowed_was(required=False),
                by=["shared_scored"],
                combine_weights={"llm_judge": 1.0},
            ),
        )

    written = str(refused.value)
    assert "was contradicts the criterion the pack still holds" in written, written
    assert "required: declared False, the rubric has True" in written, written
    assert "the veto rule keys on was.required" in written, written


def test_a_narrowed_entry_declaring_a_kind_the_rubric_does_not_have_is_refused(tmp_path: Path):
    """A criterion scores differently as ``graded`` than as ``binary``, so a declared kind
    the pack contradicts makes the recorded evidence incomparable with what the judge scores
    after the migration."""
    with pytest.raises(ValueError) as refused:
        _inspect(tmp_path, _narrowing(was=_narrowed_was(kind="graded")))

    written = str(refused.value)
    assert "was contradicts the criterion the pack still holds" in written, written
    assert "kind: declared 'graded', the rubric has 'binary'" in written, written


def test_a_narrowed_entry_whose_was_weight_differs_from_the_rubric_loads(tmp_path: Path):
    """The rule's scope, and the reason it is three fields rather than four: a narrowed
    criterion asks less, so reducing its weight is a correct migration. ``was`` describes the
    pre-migration state and the pack holds the post-migration one, and requiring those to
    agree on weight would refuse this while adding nothing against the bypass, which turns
    entirely on ``required``.
    """
    declaration = _inspect(tmp_path, _narrowing(was=_narrowed_was(weight=0.5)))
    assert declaration is not None
    assert (declaration.migrations[0].was.weight, declaration.migrations[0].was.required) == (
        0.5,
        True,
    )


def test_a_blank_was_description_is_refused(tmp_path: Path):
    """Whitespace-only rather than absent, because a folded empty block is what an author
    writing no text produces, and it would otherwise 'differ' from every criterion."""
    with pytest.raises(ValidationError, match="was.description is empty"):
        _inspect(tmp_path, _narrowing(was=_entry()["was"] | {"description": "  \n "}))


# ---------------------------------------------------------------------------
# Which criterion the pack has, and which constraints.
# ---------------------------------------------------------------------------


def test_an_entry_naming_no_criterion_in_the_rubric_is_refused(tmp_path: Path):
    with pytest.raises(ValueError, match="names no criterion in the pack's rubric"):
        _inspect(tmp_path, _narrowing(criterion="never_declared"))


def test_a_retired_criterion_still_in_the_rubric_is_refused(tmp_path: Path):
    with pytest.raises(ValueError, match="still declared in llm_judge.rubric.criteria"):
        _inspect(tmp_path, _entry(mode="retired", residual={"kind": "none", "reason": "nothing"}))


def test_a_retired_criterion_absent_from_the_rubric_loads(tmp_path: Path):
    """The accepting direction of the same rule: the rubric no longer scores what the
    constraints replaced."""
    declaration = _inspect(
        tmp_path,
        _entry(
            criterion="withdrawn",
            mode="retired",
            residual={"kind": "none", "reason": "the ordering is the whole of it"},
        ),
    )
    assert declaration is not None
    assert declaration.migrations[0].mode is MigrationMode.RETIRED


def test_a_by_id_the_pack_does_not_declare_is_refused(tmp_path: Path):
    with pytest.raises(ValueError, match=r"'by' names \['never_declared'\]"):
        _inspect(tmp_path, _narrowing(by=["never_declared"]))


def test_every_rejection_is_reported_rather_than_the_first(tmp_path: Path):
    """An author fixing a pack wants the list, which is why the pack-side rules accumulate
    instead of raising where they are found."""
    with pytest.raises(ValueError) as refused:
        _inspect(tmp_path, _narrowing(criterion="never_declared", by=["also_never_declared"]))
    written = str(refused.value)
    assert "names no criterion in the pack's rubric" in written
    assert "also_never_declared" in written


# ---------------------------------------------------------------------------
# The residual claim, and the evidence each mode rests on.
# ---------------------------------------------------------------------------


def test_a_retired_entry_carrying_a_text_residual_is_refused(tmp_path: Path):
    with pytest.raises(ValidationError, match="mode: retired carries residual.kind: text"):
        _inspect(
            tmp_path,
            _entry(criterion="withdrawn", mode="retired", residual={"kind": "text", "reason": "x"}),
        )


def test_a_narrowed_entry_carrying_an_empty_residual_is_refused(tmp_path: Path):
    with pytest.raises(ValidationError, match="mode: narrowed carries residual.kind: none"):
        _inspect(tmp_path, _narrowing(residual={"kind": "none", "reason": "nothing remains"}))


def test_a_narrowed_entry_with_no_residual_at_all_is_refused(tmp_path: Path):
    with pytest.raises(ValidationError, match="declares no residual"):
        _inspect(tmp_path, _narrowing(residual=None))


def test_a_blank_residual_reason_is_refused(tmp_path: Path):
    with pytest.raises(ValidationError, match="residual.reason is empty"):
        _inspect(tmp_path, _narrowing(residual={"kind": "text", "reason": "   "}))


def test_a_candidate_declaring_evidence_is_refused(tmp_path: Path):
    """A candidate has measured nothing against the criterion, so there is no corpus to
    point at."""
    with pytest.raises(ValidationError, match="mode: candidate declares evidence"):
        _inspect(tmp_path, _entry(mode="candidate", residual=None))


def test_a_narrowed_entry_without_evidence_is_refused(tmp_path: Path):
    with pytest.raises(ValidationError, match="declares no evidence"):
        _inspect(tmp_path, _narrowing(evidence=None))


# ---------------------------------------------------------------------------
# Acknowledgements.
# ---------------------------------------------------------------------------


def test_an_acknowledged_trial_under_the_corpus_loads(tmp_path: Path):
    declaration = _inspect(
        tmp_path,
        _narrowing(
            acknowledged=[
                {"trial": "tests/data/corpora/notes/met/trial_04", "reason": "the judge misread"}
            ]
        ),
    )
    assert declaration is not None
    assert declaration.migrations[0].acknowledged[0].trial.endswith("trial_04")


def test_an_acknowledged_trial_outside_the_corpus_is_refused(tmp_path: Path):
    """A waiver addresses a disagreement the verdict measured, so a trial the verdict never
    read waives nothing."""
    with pytest.raises(ValidationError, match="are not bundles under evidence.corpus"):
        _inspect(
            tmp_path,
            _narrowing(
                acknowledged=[{"trial": "tests/data/corpora/other/trial_01", "reason": "wrong"}]
            ),
        )


def test_an_acknowledged_trial_that_is_the_corpus_root_is_refused(tmp_path: Path):
    """The degenerate boundary: the root holds the bundles and is not one of them."""
    with pytest.raises(ValidationError, match="are not bundles under evidence.corpus"):
        _inspect(
            tmp_path,
            _narrowing(acknowledged=[{"trial": "tests/data/corpora/notes", "reason": "wrong"}]),
        )


def test_an_acknowledgement_without_evidence_is_refused(tmp_path: Path):
    """A candidate cannot carry evidence, so it cannot waive a disagreement either — there
    is no verdict for one to have disagreed with."""
    with pytest.raises(ValidationError, match="without evidence to have disagreed with"):
        _inspect(
            tmp_path, _candidacy(acknowledged=[{"trial": "x/y", "reason": "the judge misread"}])
        )


def test_a_blank_acknowledgement_reason_is_refused(tmp_path: Path):
    with pytest.raises(ValidationError, match="acknowledged.reason is empty"):
        _inspect(
            tmp_path,
            _narrowing(
                acknowledged=[{"trial": "tests/data/corpora/notes/met/trial_04", "reason": ""}]
            ),
        )


# ---------------------------------------------------------------------------
# The file's own shape.
# ---------------------------------------------------------------------------


def test_an_unknown_entry_key_is_refused(tmp_path: Path):
    """The sidecar crosses the YAML boundary, so a misspelled key is a claim that is read by
    nothing rather than a key that is ignored."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        _inspect(tmp_path, _narrowing(acknowledgements=[]))


def test_a_declaration_with_no_entries_is_refused(tmp_path: Path):
    with pytest.raises(ValidationError, match="at least 1 item"):
        _inspect(tmp_path)


def test_an_entry_claiming_no_constraint_is_refused(tmp_path: Path):
    with pytest.raises(ValidationError, match="at least 1 item"):
        _inspect(tmp_path, _narrowing(by=[]))


def test_a_sidecar_that_is_not_a_mapping_is_refused_naming_the_file(tmp_path: Path):
    """The shape an author produces by writing an entry directly under the top level."""
    grading_path = write_migration_pack(
        tmp_path / "case", grading_text=_grading_text(), migration=[_narrowing()]
    )
    with pytest.raises(RuntimeError, match="is not a YAML mapping"):
        inspect_migration_declaration(grading_path)


def test_a_sidecar_beside_no_grading_file_is_refused(tmp_path: Path):
    """Every rule reads the pack, so a declaration with no rubric to resolve against is not
    a migration that happens to check out."""
    grading_path = write_migration_pack(
        tmp_path / "case", grading_text=_grading_text(), migration={"migrations": [_narrowing()]}
    )
    grading_path.unlink()
    with pytest.raises(ValueError, match="which does not exist"):
        inspect_migration_declaration(grading_path)
