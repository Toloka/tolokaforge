"""The first committed corpus, and what the bar says about it.

Five recorded trials of ``notes_add_note_duplicate_check_gated`` live under
``tests/data/migration_corpora/notes_duplicate_check/not_met/``, carrying only the files the
differential reads. Every one has the judge's ``checked_duplicates_first`` verdict as
**not met**, and the candidate constraint fails on all five, so the corpus reads as *perfect
accuracy and no evidence at all*: κ is undefined, and the entry resting on it is
``insufficient_evidence`` rather than a pass. That is the whole point of the committed
half — it is the falsifier for the corpus's other half, which #683's Stage 5 buys.

The pack is a fixture under ``tests/data/migration_packs/``, not the shipped one: the shipped
pack has not taken the migration yet, and reaching it is what ``--packs`` is for.

**No number here is re-derived from the maths.** ``tests/unit/grading/test_agreement.py``
already locks that a one-sided corpus gives accuracy ``1.0`` and κ ``None``; what this module
locks is the *verdict* the command reaches over the real bundles, the table it reports, and
the exit code that follows.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner, Result
from pydantic import BaseModel

from tolokaforge.core.grading.rubric_migration import (
    MigrationCounterfactual,
    ReconciledEntry,
    ReconcileReport,
    ReconcileVerdict,
    TrialCounterfactual,
    reconcile_corpus,
)
from tolokaforge.dx.cli.main import cli

pytestmark = [pytest.mark.canonical, pytest.mark.grading]

_REPO = Path(__file__).resolve().parents[2]
_CORPUS = _REPO / "tests/data/migration_corpora/notes_duplicate_check"
_NOT_MET = _CORPUS / "not_met"
_PACKS = _REPO / "tests/data/migration_packs"
_DECLARATION = _PACKS / "notes_duplicate_check_narrowed/migration.yaml"

#: The bundle directories the committed half holds, written out so a bundle added to or
#: dropped from the corpus reds here rather than silently moving every number below.
_COMMITTED_BUNDLES = {
    "native_shared_domain_example_20260629_133126",
    "native_shared_domain_example_20260702_140836",
    "native_shared_domain_gate_demo_20260625_184817",
    "native_shared_domain_gate_demo_20260626_101928",
    "native_shared_domain_gate_demo_20260626_102829",
}

#: The files a bundle is trimmed to: what the differential reads, and ``task.yaml`` for the
#: provenance and model attribution that travel in its ``model_config``.
_RETAINED_FILES = {
    "task.yaml",
    "trajectory.yaml",
    "grade.yaml",
    "tools_schemas.yaml",
    "metrics.yaml",
}


def _reconcile_cli(*args: str) -> Result:
    return CliRunner().invoke(cli, ["reconcile", *args], env={"COLUMNS": "200"})


def _copy(tmp_path: Path) -> Path:
    """A writable copy of the committed half.

    Every reconciliation in this module runs over a copy, never over ``tests/data`` itself.
    Not caution: an implementation that wrote into the corpus would otherwise dirty the
    committed tree *and* mask the read-only lock below, whose ``before`` snapshot would
    already carry the stray file. Measured — that is exactly what a probe writing per-bundle
    artifacts did, and the lock stayed green through it.
    """
    destination = tmp_path / "corpus"
    shutil.copytree(_NOT_MET, destination)
    return destination


def _report_over_the_committed_half(tmp_path: Path) -> ReconcileReport:
    return reconcile_corpus(_copy(tmp_path), replay_id="canon", packs=[_PACKS], dry_run=True)


def _entry_over_the_committed_half(tmp_path: Path) -> ReconciledEntry:
    (entry,) = _report_over_the_committed_half(tmp_path).entries
    return entry


def test_the_committed_half_is_exactly_the_five_trimmed_bundles() -> None:
    """Two sources for the corpus's membership and shape: the tree, and this literal."""
    assert {bundle.name for bundle in _NOT_MET.iterdir()} == _COMMITTED_BUNDLES
    for bundle in _NOT_MET.iterdir():
        assert {path.name for path in bundle.iterdir()} == _RETAINED_FILES


def test_no_committed_bundle_declares_a_constraint_block() -> None:
    """The premise that makes resolving the block from the *pack* load-bearing.

    None of these trials ran with trace constraints, so nothing in the recomputation reads a
    constraint unless the resolution goes to the pack — which is what makes editing the
    shipped constraint red the lock over this frozen corpus. A ``--constraints``-style flag
    would pin a fixture instead and make the guard decorative.
    """
    for bundle in sorted(_NOT_MET.iterdir()):
        task = yaml.safe_load((bundle / "task.yaml").read_text())
        assert task["grading_config"].get("trace_checks") is None, bundle.name


def test_the_one_sided_corpus_is_insufficient_evidence_for_the_narrow_it_carries(
    tmp_path: Path,
) -> None:
    """Perfect accuracy, no κ, every observation in one cell — and therefore not a pass.

    The contingency table is what makes the corpus's shape visible: five observations in
    ``judge_not_met_constraint_failed`` and zero everywhere else is a designed experiment, and
    an accuracy of 1.0 read on its own would say the opposite.
    """
    entry = _entry_over_the_committed_half(tmp_path)

    assert entry.observations == 5
    assert entry.accuracy == 1.0
    assert entry.kappa is None
    assert entry.contingency.model_dump() == {
        "judge_met_constraint_passed": 0,
        "judge_met_constraint_failed": 0,
        "judge_not_met_constraint_passed": 0,
        "judge_not_met_constraint_failed": 5,
    }
    assert entry.verdict is ReconcileVerdict.INSUFFICIENT_EVIDENCE
    assert entry.excluded_trials == []
    assert entry.refusals == []


def test_the_command_exits_non_zero_for_a_narrow_resting_on_the_committed_half(
    tmp_path: Path,
) -> None:
    """``insufficient_evidence`` is not exit zero, and the reason is printed rather than implied."""
    result = _reconcile_cli("--source", str(_copy(tmp_path)), "--packs", str(_PACKS), "--dry-run")

    assert result.exit_code == 1, result.output
    assert "insufficient_evidence" in result.output
    assert "blocks the migration" in result.output
    assert "kappa undefined" in result.output


def test_the_report_states_the_mode_the_author_declared_and_quotes_its_residual(
    tmp_path: Path,
) -> None:
    """The declaration is the second source: the report renders it, never re-derives it."""
    declared = yaml.safe_load(_DECLARATION.read_text())["migrations"][0]
    entry = _entry_over_the_committed_half(tmp_path)

    assert entry.mode.value == declared["mode"]
    assert entry.residual_kind is not None
    assert entry.residual_kind.value == declared["residual"]["kind"]
    assert entry.residual_reason.split() == declared["residual"]["reason"].split()
    assert entry.by == declared["by"]


def test_the_report_names_which_side_of_the_pair_is_the_reference(tmp_path: Path) -> None:
    """The maths is symmetric and will not say which labeller is which; the report must."""
    report = _report_over_the_committed_half(tmp_path)

    assert "judge" in report.reference_labeller
    assert "constraint" in report.candidate_labeller


@pytest.mark.parametrize(
    ("model", "fields"),
    [
        (
            ReconcileReport,
            {
                "source",
                "replay_id",
                "packs_searched",
                "reference_labeller",
                "candidate_labeller",
                "trials_read",
                "entries",
                "unreadable_trials",
            },
        ),
        (
            ReconciledEntry,
            {
                "task_ids",
                "criterion",
                "mode",
                "residual_kind",
                "residual_reason",
                "by",
                "observations",
                "contingency",
                "accuracy",
                "kappa",
                "strict_disagreements",
                "permissive_disagreements",
                "excluded_trials",
                "counterfactual",
                "verdict",
                "refusals",
            },
        ),
        (
            MigrationCounterfactual,
            {"weights_declared", "trials", "unrecomputed_trials"},
        ),
        (
            TrialCounterfactual,
            {
                "trial",
                "weights_before",
                "weights_after",
                "vetoes_before",
                "vetoes_after",
                "judge_component_before",
                "judge_component_after",
                "score_before",
                "score_after",
                "binary_pass_before",
                "binary_pass_after",
            },
        ),
    ],
    ids=["report", "entry", "counterfactual", "trial-counterfactual"],
)
def test_the_report_model_carries_no_field_that_ranks_or_compares_modes(
    model: type[BaseModel], fields: set[str]
) -> None:
    """Set equality over the model's own field names against a set written out here.

    Structural rather than prose: an assertion that the report contains no *sentence*
    recommending a mode passes on every implementation and can never fire, whereas this reds
    the moment a ``recommended_mode`` or a ``retirement_score`` is added — which is the claim
    the evidence cannot make. On a corpus with no disagreements ``narrowed`` and ``retired``
    satisfy the same condition, so the mode is the author's recorded judgment and nothing
    here may read as the bar having chosen it.

    Every model the report is built from, because the entry is where such a field would go and
    locking the envelope alone would leave it unguarded — the counterfactual included, since a
    per-trial ``recommended_mode`` would rank modes exactly as an entry-level one would. The two
    sources of each set are the model and this literal, neither derived from the other.
    """
    assert set(model.model_fields) == fields
    assert model.model_config["extra"] == "forbid"


def _recorded_grade(bundle: Path) -> dict[str, Any]:
    return yaml.safe_load((bundle / "grade.yaml").read_text())


def test_the_extraction_reproduces_every_recorded_verdict_in_the_committed_corpus(
    tmp_path: Path,
) -> None:
    """The runner's verdict composition, moved to a function and driven offline, reaches the
    verdict the runner reached — on all five bundles.

    This is the equivalence claim the extraction rests on, and it is not a tautology: the
    *before* column is recomposed by :func:`compose_runner_trial_verdict` over each bundle's
    recorded components and rubric, while the expected values are read straight out of that
    bundle's ``grade.yaml``. Two sources, neither derived from the other. Drop the judge-gate
    zeroing from the extracted function and every bundle reports a judge component of ``1.0``
    against a recorded ``0.0`` — required criteria are excluded from the weighted average, so the
    aggregate alone says the trial aced a rubric it failed.
    """
    counterfactual = _entry_over_the_committed_half(tmp_path).counterfactual

    assert counterfactual.unrecomputed_trials == []
    assert len(counterfactual.trials) == 5
    for row in counterfactual.trials:
        grade = _recorded_grade(Path(row.trial))
        assert row.judge_component_before == pytest.approx(grade["components"]["llm_judge"])
        assert row.score_before == pytest.approx(grade["score"])
        assert row.binary_pass_before is grade["binary_pass"]


def test_the_counterfactual_reports_what_the_narrow_does_to_every_committed_trial(
    tmp_path: Path,
) -> None:
    """The judge component rises, the trial score rises, and the verdict does not move.

    All five: the judge component goes ``0.0 → 1.0`` because the required criterion leaves the
    judge's gate, the trial score goes ``0.0 → 0.5`` as the recomputed trace component folds in
    beside it, and the pass rate is **0/5 → 0/5** because the veto the judge held is now held by
    the trace gate, which fails on all five. That last equality is the acceptance criterion —
    unchanged pass rates, or the difference reported — and it holds here for a stated reason
    rather than by luck.
    """
    counterfactual = _entry_over_the_committed_half(tmp_path).counterfactual

    assert [row.judge_component_before for row in counterfactual.trials] == [0.0] * 5
    assert [row.judge_component_after for row in counterfactual.trials] == [1.0] * 5
    assert [row.score_before for row in counterfactual.trials] == [0.0] * 5
    assert [row.score_after for row in counterfactual.trials] == [0.5] * 5
    assert [row.binary_pass_before for row in counterfactual.trials] == [False] * 5
    assert [row.binary_pass_after for row in counterfactual.trials] == [False] * 5


def test_the_veto_transfers_from_the_criterion_to_the_trace_gate_on_every_trial(
    tmp_path: Path,
) -> None:
    """For a ``required`` criterion the two weight maps are *identical*, so a weight-shift report
    would be silent about the only thing the migration changes. The veto sets are what says it."""
    counterfactual = _entry_over_the_committed_half(tmp_path).counterfactual
    declared = yaml.safe_load(_DECLARATION.read_text())["migrations"][0]

    assert declared["was"]["required"] is True
    assert "combine_weights" not in declared
    assert len(counterfactual.trials) == 5
    for row in counterfactual.trials:
        assert row.weights_before == row.weights_after
        assert row.vetoes_before == ["checked_duplicates_first"]
        assert row.vetoes_after == ["the_notes_were_listed_before_the_note_was_added"]


def test_no_reported_weight_map_is_the_map_the_pack_holds_today(tmp_path: Path) -> None:
    """The counterfactual answers "what does the map *this entry* declares do", so the pack's
    current ``combine.weights`` — the post-migration state a reviewer is being asked to judge —
    must not be what the report folds under. Measurable here because the fixture's three maps all
    differ: the pack holds ``{llm_judge: 0.7, trace_checks: 0.3}``, the corpus recorded
    ``{llm_judge: 1.0}``, and this entry declares none at all.
    """
    current = yaml.safe_load((_PACKS / "notes_duplicate_check_narrowed/grading.yaml").read_text())[
        "combine"
    ]["weights"]
    counterfactual = _entry_over_the_committed_half(tmp_path).counterfactual

    assert current == {"llm_judge": 0.7, "trace_checks": 0.3}
    assert counterfactual.weights_declared is None
    assert len(counterfactual.trials) == 5
    for row in counterfactual.trials:
        assert row.weights_before != current
        assert row.weights_after != current


def test_the_counterfactual_reaches_the_reviewer_it_is_evidence_for(tmp_path: Path) -> None:
    """Evidence nobody reads gates nothing and informs nothing, so the rendered report carries
    it — and says what it is *not*, because a reader who took it for a gate would read a finite
    corpus as licence for an unbounded claim."""
    result = _reconcile_cli("--source", str(_copy(tmp_path)), "--packs", str(_PACKS), "--dry-run")

    assert "counterfactual under the map this entry declares" in result.output
    assert "pass rate 0/5 → 0/5" in result.output
    assert "nothing here decides the verdict" in result.output


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_reconciling_writes_its_report_and_touches_no_bundle_file(tmp_path: Path) -> None:
    """Report-only: a verdict never edits the corpus it was read from, or a pack.

    Driven over a copy so the assertion is about every file rather than about the ones a test
    thought to name — the committed tree itself is never written to by the suite.
    """
    corpus = _copy(tmp_path)
    before = _tree_digest(corpus)
    packs = tmp_path / "packs"
    shutil.copytree(_PACKS, packs)
    packs_before = _tree_digest(packs)

    reconcile_corpus(corpus, replay_id="written", packs=[packs], dry_run=False)

    after = _tree_digest(corpus)
    assert {path: digest for path, digest in after.items() if path in before} == before
    assert set(after) - set(before) == {"reconcile/written/reconcile_report.yaml"}
    assert _tree_digest(packs) == packs_before


def test_a_dry_run_reaches_the_verdict_and_writes_nothing(tmp_path: Path) -> None:
    corpus = _copy(tmp_path)
    before = _tree_digest(corpus)

    report = reconcile_corpus(corpus, replay_id="preview", packs=[_PACKS], dry_run=True)

    assert report.entries[0].verdict is ReconcileVerdict.INSUFFICIENT_EVIDENCE
    assert _tree_digest(corpus) == before


_IMPORT_PROBE = """
import importlib
import sys

importlib.import_module("tolokaforge.core.grading.rubric_migration")
print("\\n".join(sorted(m for m in sys.modules if m.split(".")[0] == "tolokaforge")))
"""


def test_the_differential_reaches_neither_an_llm_client_nor_the_judge() -> None:
    """ "Zero spend" is a property of the imports, measured in a clean interpreter.

    A clean subprocess is the only honest measurement: pytest has already imported most of
    the tree. The two names are the assertion rather than the ``core.llm`` prefix — the
    ``core.llm`` policy modules arrive transitively through ``core.models`` — and the
    evaluator's presence is asserted beside them so a probe that imported nothing fails
    instead of passing.

    This module reaches further than the replay it builds on: resolving a bundle's pack goes
    through the same task loader ``tolokaforge validate`` uses. That is what the measurement
    is for.
    """
    completed = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE], capture_output=True, text=True
    )

    assert completed.returncode == 0, completed.stderr
    footprint = set(completed.stdout.split())
    assert "tolokaforge.core.grading.trace_checks" in footprint
    assert "tolokaforge.core.llm.client" not in footprint
    assert "tolokaforge.core.grading.judge" not in footprint
