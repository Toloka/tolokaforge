"""The committed corpus, and what the bar says about each half of it and about the union.

``tests/data/migration_corpora/notes_duplicate_check/`` holds seventeen recorded trials in two
halves, each written by its own ``tolokaforge curate`` invocation and carrying the manifest
that names what it admitted and what it refused. A bundle holds the files the differential
reads, plus the tool-call record wherever its source run had one.

``not_met/`` — five trials of ``notes_add_note_duplicate_check_gated``, whose prompt omits the
check-first policy. The judge's ``checked_duplicates_first`` verdict is **not met** on every
one and the candidate constraint fails on every one, so that half alone reads as *perfect
accuracy and no evidence at all*: κ is undefined and an entry resting on it is
``insufficient_evidence`` rather than a pass.

``met/`` — twelve trials of ``notes_add_note_duplicate_check_policy``, the arm whose own prompt
states the policy. The judge's verdict is **met** on every one and the constraint passes on
every one.

Each half is therefore the other's falsifier, and κ is defined only over the union. The
label-invariance of either half taken alone is what the bar's evidence condition exists to
catch, so both the one-sided verdict and the union verdict are locked here.

Two pack roots reconcile this corpus and both are locked here. The **shipped** arms under
``examples/`` declare the migration they took, and the default ``--packs`` root resolves them,
which is what makes the re-verification bite on the pack a reviewer reads: editing the shipped
constraint changes what is recomputed over the frozen corpus. The **fixtures** under
``tests/data/migration_packs/`` declare the same criterion under a weight map differing from
both the pack's current one and the map the corpus recorded, which is the only reason the
counterfactual's *source* is measurable — they share the shipped ``task_id``s, so ``tests/data``
is deliberately not a default root. Either root holds one pack
per arm, because a bundle resolves its constraints through its own ``task_id``, and each root's
two declarations are identical because pooling keys on the criterion text and the resolved
constraints — a drift between them is a pooling refusal, not a quietly merged pair of claims.

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

from tolokaforge.core.grading.corpus_curation import CorpusManifest
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
_MET = _CORPUS / "met"
_PACKS = _REPO / "tests/data/migration_packs"
_DECLARATION = _PACKS / "notes_duplicate_check_narrowed/migration.yaml"
_POLICY_DECLARATION = _PACKS / "notes_duplicate_check_policy_narrowed/migration.yaml"

_NOTES = _REPO / "examples/native/native_shared_domain/dataset/notes"
_SHARED_PROMPT = _NOTES / "_shared/system_prompt.md"
_GATED_ARM = _NOTES / "testcases/add_note_duplicate_check_gated"
_POLICY_ARM = _NOTES / "testcases/add_note_duplicate_check_policy"

#: The bundle directories the not-met half holds, written out so a bundle added to or
#: dropped from the corpus reds here rather than silently moving every number below.
_NOT_MET_BUNDLES = {
    "native_shared_domain_example_20260629_133126_trial0",
    "native_shared_domain_example_20260702_140836_trial0",
    "native_shared_domain_gate_demo_20260625_184817_trial0",
    "native_shared_domain_gate_demo_20260626_101928_trial0",
    "native_shared_domain_gate_demo_20260626_102829_trial0",
}

#: The met half, bought under the policy arm's prompt. Written out for the same reason, and
#: separately from the half above because each half is the other's falsifier: the not-met
#: half alone gives κ ``None``, and κ is defined only over the union.
_MET_BUNDLES = {
    "native_shared_domain_policy_demo_20260803_063010_trial0",
    "native_shared_domain_policy_demo_20260803_063010_trial1",
    "native_shared_domain_policy_demo_20260803_063010_trial2",
    "native_shared_domain_policy_demo_20260803_063150_trial0",
    "native_shared_domain_policy_demo_20260803_063150_trial1",
    "native_shared_domain_policy_demo_20260803_063150_trial2",
    "native_shared_domain_policy_demo_20260803_063150_trial3",
    "native_shared_domain_policy_demo_20260803_063150_trial4",
    "native_shared_domain_policy_demo_20260803_063150_trial5",
    "native_shared_domain_policy_demo_20260803_063150_trial6",
    "native_shared_domain_policy_demo_20260803_063150_trial7",
    "native_shared_domain_policy_demo_20260803_063150_trial8",
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

#: Carried on top of those where the source run recorded one. ``TraceEvent.status``,
#: ``executor`` and ``latency_seconds`` come from this file alone, so a half's bundles
#: carrying it is what decides whether a constraint reading them can be recomputed here.
_TOOL_LOG = "tool_log.yaml"


def _reconcile_cli(*args: str) -> Result:
    return CliRunner().invoke(cli, ["reconcile", *args], env={"COLUMNS": "200"})


def _bundles(half: Path) -> list[Path]:
    """The trial bundles a half holds — every directory beside its manifest."""
    return sorted(path for path in half.iterdir() if path.is_dir())


def _manifest(half: Path) -> CorpusManifest:
    return CorpusManifest.model_validate(yaml.safe_load((half / "corpus.yaml").read_text()))


def _copied(root: Path, tmp_path: Path) -> Path:
    """A writable copy of ``root``.

    Every reconciliation in this module runs over a copy, never over ``tests/data`` itself.
    Not caution: an implementation that wrote into the corpus would otherwise dirty the
    committed tree *and* mask the read-only lock below, whose ``before`` snapshot would
    already carry the stray file. Measured — that is exactly what a probe writing per-bundle
    artifacts did, and the lock stayed green through it.
    """
    destination = tmp_path / "corpus"
    shutil.copytree(root, destination)
    return destination


def _copy(tmp_path: Path) -> Path:
    return _copied(_NOT_MET, tmp_path)


def _report_over_the_not_met_half(tmp_path: Path) -> ReconcileReport:
    return reconcile_corpus(_copy(tmp_path), replay_id="canon", packs=[_PACKS], dry_run=True)


def _entry_over_the_not_met_half(tmp_path: Path) -> ReconciledEntry:
    (entry,) = _report_over_the_not_met_half(tmp_path).entries
    return entry


def test_the_not_met_half_is_exactly_the_five_trimmed_bundles() -> None:
    """Two sources for the corpus's membership and shape: the tree, and this literal.

    None of the five carries a tool-call record: they are old runs recorded before the
    harness persisted one, which is why a constraint reading ``status`` cannot be
    recomputed over this half and why the manifest says so per bundle.
    """
    assert {bundle.name for bundle in _bundles(_NOT_MET)} == _NOT_MET_BUNDLES
    for bundle in _bundles(_NOT_MET):
        assert {path.name for path in bundle.iterdir()} == _RETAINED_FILES


def test_no_bundle_in_either_half_declares_a_constraint_block() -> None:
    """The premise that makes resolving the block from the *pack* load-bearing.

    None of these trials ran with trace constraints, so nothing in the recomputation reads a
    constraint unless the resolution goes to the pack — which is what makes editing the
    shipped constraint red the lock over this frozen corpus. A ``--constraints``-style flag
    would pin a fixture instead and make the guard decorative.
    """
    bundles = [*_bundles(_NOT_MET), *_bundles(_MET)]
    assert len(bundles) == len(_NOT_MET_BUNDLES) + len(_MET_BUNDLES)
    for bundle in bundles:
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
    entry = _entry_over_the_not_met_half(tmp_path)

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


def test_the_command_exits_non_zero_for_a_narrow_resting_on_the_not_met_half(
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
    entry = _entry_over_the_not_met_half(tmp_path)

    assert entry.mode.value == declared["mode"]
    assert entry.residual is not None
    assert entry.residual.kind.value == declared["residual"]["kind"]
    assert entry.residual.reason.split() == declared["residual"]["reason"].split()
    assert entry.by == declared["by"]


def test_the_report_names_which_side_of_the_pair_is_the_reference(tmp_path: Path) -> None:
    """The maths is symmetric and will not say which labeller is which; the report must."""
    report = _report_over_the_not_met_half(tmp_path)

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
                "excluded_bundles",
            },
        ),
        (
            ReconciledEntry,
            {
                "task_ids",
                "criterion",
                "mode",
                "residual",
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


def test_the_extraction_reproduces_every_recorded_verdict_in_the_not_met_half(
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
    counterfactual = _entry_over_the_not_met_half(tmp_path).counterfactual

    assert counterfactual.unrecomputed_trials == []
    assert len(counterfactual.trials) == 5
    for row in counterfactual.trials:
        grade = _recorded_grade(Path(row.trial))
        assert row.judge_component_before == pytest.approx(grade["components"]["llm_judge"])
        assert row.score_before == pytest.approx(grade["score"])
        assert row.binary_pass_before is grade["binary_pass"]


def test_the_counterfactual_reports_what_the_narrow_does_to_every_not_met_trial(
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
    counterfactual = _entry_over_the_not_met_half(tmp_path).counterfactual

    assert [row.judge_component_before for row in counterfactual.trials] == [0.0] * 5
    assert [row.judge_component_after for row in counterfactual.trials] == [1.0] * 5
    assert [row.score_before for row in counterfactual.trials] == [0.0] * 5
    assert [row.score_after for row in counterfactual.trials] == [0.5] * 5
    assert [row.binary_pass_before for row in counterfactual.trials] == [False] * 5
    assert [row.binary_pass_after for row in counterfactual.trials] == [False] * 5


def test_the_narrow_leaves_two_vetoes_over_one_policy_on_every_trial(
    tmp_path: Path,
) -> None:
    """A ``narrowed`` criterion stays in the rubric and stays ``required: true``, so the *after*
    veto set carries **both** it and the trace gate — the shipped narrow's whole shape, and the
    fact an after-set built by dropping the criterion would misreport as a veto lost.

    Read beside the weight columns, which move for a different reason: the criterion frees no
    score share, so every share the declared map shifts is the trace check the migration
    installs being given one, not the conversion's arithmetic. The veto set is what the
    conversion itself moves. That the criterion's own id survives the migration is asserted from
    the declaration's ``mode`` rather than from the report, so a counterfactual that dropped it
    reds against ``narrowed`` written on disk.
    """
    counterfactual = _entry_over_the_not_met_half(tmp_path).counterfactual
    declared = yaml.safe_load(_DECLARATION.read_text())["migrations"][0]

    assert declared["mode"] == "narrowed"
    assert declared["was"]["required"] is True
    assert len(counterfactual.trials) == 5
    for row in counterfactual.trials:
        assert row.weights_after == declared["combine_weights"]
        assert row.vetoes_before == ["checked_duplicates_first"]
        assert row.vetoes_after == [
            "checked_duplicates_first",
            "the_notes_were_listed_before_the_note_was_added",
        ]


def test_no_reported_weight_map_is_the_map_the_pack_holds_today(tmp_path: Path) -> None:
    """The counterfactual answers "what does the map *this entry* declares do", so the pack's
    current ``combine.weights`` — the post-migration state a reviewer is being asked to judge —
    must not be what the report folds under. Measurable here because the fixture's three maps all
    differ: the pack holds ``{llm_judge: 0.7, trace_checks: 0.3}``, the corpus recorded
    ``{llm_judge: 1.0}``, and this entry declares ``{llm_judge: 0.5, trace_checks: 0.5}``.
    """
    current = yaml.safe_load((_PACKS / "notes_duplicate_check_narrowed/grading.yaml").read_text())[
        "combine"
    ]["weights"]
    counterfactual = _entry_over_the_not_met_half(tmp_path).counterfactual

    assert current == {"llm_judge": 0.7, "trace_checks": 0.3}
    assert counterfactual.weights_declared == {"llm_judge": 0.5, "trace_checks": 0.5}
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


# ---------------------------------------------------------------------------
# The union: the second label, and the pair of arms that produced it
# ---------------------------------------------------------------------------


def _report_over_the_union(tmp_path: Path) -> ReconcileReport:
    return reconcile_corpus(
        _copied(_CORPUS, tmp_path), replay_id="canon", packs=[_PACKS], dry_run=True
    )


def _entry_over_the_union(tmp_path: Path) -> ReconciledEntry:
    (entry,) = _report_over_the_union(tmp_path).entries
    return entry


def test_the_met_half_is_exactly_the_twelve_trimmed_bundles() -> None:
    """Two sources for the bought half's membership and shape: the tree, and this literal.

    Every one of the twelve carries its tool-call record, because every source run did.
    The asymmetry with the not-met half is the corpus's, not the curation's: a corpus is
    record-carrying exactly where its sources were.
    """
    assert {bundle.name for bundle in _bundles(_MET)} == _MET_BUNDLES
    for bundle in _bundles(_MET):
        assert {path.name for path in bundle.iterdir()} == _RETAINED_FILES | {_TOOL_LOG}


def test_the_policy_arm_ships_the_shared_prompt_verbatim_with_the_policy_appended() -> None:
    """The containment guard, and it has to be ``startswith``.

    A task-level ``system_prompt`` is a path resolved against the task directory and
    ``deep_merge`` lets a scalar from the delta *replace* the base, so the policy arm's own file
    replaces the shared one outright rather than extending it — measured, and the reason the
    file exists at all. Forking the shared text is what makes drift possible, so the shared
    bytes are asserted to be a *prefix*: the policy paragraph is appended, never interleaved
    under one of the shared file's headings, because an interleaved paragraph breaks containment
    on its first commit and the likely reaction to that red is to weaken this guard.

    The gated arm's equality is the other half of the pair. Without it a shared file rewritten
    to state the policy itself would leave both arms passing while the experiment lost its
    independent variable.
    """
    shared = _SHARED_PROMPT.read_bytes()
    policy = (_POLICY_ARM / "system_prompt.md").read_bytes()

    assert not (_GATED_ARM / "system_prompt.md").exists()
    assert policy.startswith(shared)
    assert len(policy) > len(shared)
    assert b"list_notes" in policy[len(shared) :]


def _grading_of(arm: Path) -> dict[str, Any]:
    return yaml.safe_load((arm / "grading.yaml").read_text())


def _behavioural_fields(arm: Path) -> dict[str, Any]:
    """Everything about an arm's task that decides what the agent is asked to do.

    ``name`` and ``description`` are deliberately absent: they are the author's prose about
    which arm this is, and requiring them to match would forbid saying so.
    """
    task = yaml.safe_load((arm / "task.yaml").read_text())
    return {
        "domain": task["domain"],
        "max_turns": task["max_turns"],
        "initial_user_message": task["initial_user_message"].split(),
        "backstory": task["actors"]["user"]["backstory"].split(),
        "json_db": (arm / task["initial_state"]["json_db"]).read_bytes(),
    }


def test_the_two_arms_differ_in_the_agent_prompt_and_in_nothing_a_grade_reads() -> None:
    """The independent variable, locked: one paragraph of prompt and nothing else.

    The corpus pools the two arms' trials into one measurement, and pooling keys on the
    criterion text and the resolved constraints — so a rubric that drifted between the arms
    would be two claims counted as one, and a different initial state or user message would make
    the met labels answer a different question than the not-met ones. Both arms' rubrics are read
    here rather than compared byte for byte, because the two files' leading comments say which
    arm they are and must be allowed to.
    """
    assert _grading_of(_GATED_ARM) == _grading_of(_POLICY_ARM)
    assert _behavioural_fields(_GATED_ARM) == _behavioural_fields(_POLICY_ARM)


def test_the_union_corpus_reaches_a_defined_kappa_and_no_counter_evidence(
    tmp_path: Path,
) -> None:
    """What the bought half buys: κ defined, so the bar can reach a verdict at all.

    Seventeen observations in two cells on the agreeing diagonal — twelve met/passed from the
    policy arm and five not-met/failed from the gated one — with nothing off it, so there is no
    disagreement in either direction to acknowledge and the entry reaches
    ``no_counter_evidence``. Read beside the one-sided lock above, which is this lock's
    falsifier: the same declaration over ``not_met/`` alone is ``insufficient_evidence``.

    The verdict is what is asserted, not the maths — ``no_counter_evidence`` says no trial in
    this corpus contradicted the claim over seventeen observations, and κ ``1.0`` is reported
    beside it rather than thresholded.
    """
    entry = _entry_over_the_union(tmp_path)

    assert entry.task_ids == [
        "notes_add_note_duplicate_check_gated",
        "notes_add_note_duplicate_check_policy",
    ]
    assert entry.observations == 17
    assert entry.accuracy == 1.0
    assert entry.kappa == 1.0
    assert entry.contingency.model_dump() == {
        "judge_met_constraint_passed": 12,
        "judge_met_constraint_failed": 0,
        "judge_not_met_constraint_passed": 0,
        "judge_not_met_constraint_failed": 5,
    }
    assert entry.strict_disagreements == []
    assert entry.permissive_disagreements == []
    assert entry.excluded_trials == []
    assert entry.refusals == []
    assert entry.verdict is ReconcileVerdict.NO_COUNTER_EVIDENCE


def test_the_narrow_moves_no_trial_verdict_across_the_union(tmp_path: Path) -> None:
    """The acceptance criterion — unchanged pass rates, or the difference reported.

    Twelve of seventeen pass before and the same twelve after, for a reason rather than by
    luck: the veto the judge held passes to the trace gate, which fails on exactly the five
    trials whose criterion the judge failed. The met arm's rows are what make this more than a
    restatement of the not-met lock, where every trial failed both before and after and a
    counterfactual that moved nothing would have looked identical.
    """
    counterfactual = _entry_over_the_union(tmp_path).counterfactual

    assert counterfactual.unrecomputed_trials == []
    assert len(counterfactual.trials) == 17
    passed_before = [row.trial for row in counterfactual.trials if row.binary_pass_before]
    passed_after = [row.trial for row in counterfactual.trials if row.binary_pass_after]
    assert passed_before == passed_after
    assert len(passed_before) == 12
    assert {Path(trial).name for trial in passed_before} == _MET_BUNDLES


def test_the_declared_evidence_is_the_measurement_over_the_corpus_it_names(
    tmp_path: Path,
) -> None:
    """The numbers the two declarations quote are the numbers a reconciliation reaches.

    Three sources, none derived from another: the declarations' own ``evidence`` block, the
    bundle directories on disk under the corpus root that block names, and the report the
    command produces. A declaration is a claim — nothing in the product cross-checks it, which
    is deliberate, because the report renders what the author declared rather than re-deriving
    it — so this is where a stale claim is caught.

    Both arms' declarations are byte-identical, which is what keeps the pooled measurement one
    measurement quoted twice rather than two claims the pooling rule would then refuse.
    """
    assert _DECLARATION.read_bytes() == _POLICY_DECLARATION.read_bytes()
    declared = yaml.safe_load(_DECLARATION.read_text())["migrations"][0]["evidence"]
    named = _REPO / declared["corpus"]

    assert named == _CORPUS
    assert {bundle.name for half in named.iterdir() for bundle in _bundles(half)} == (
        _NOT_MET_BUNDLES | _MET_BUNDLES
    )
    entry = _entry_over_the_union(tmp_path)
    assert declared["observations"] == entry.observations
    assert declared["kappa"] == entry.kappa


def test_the_command_exits_zero_over_the_union_and_says_what_that_means(
    tmp_path: Path,
) -> None:
    """``no_counter_evidence``, and no other verdict, is what exit ``0`` means."""
    corpus = _copied(_CORPUS, tmp_path)

    result = _reconcile_cli("--source", str(corpus), "--packs", str(_PACKS), "--dry-run")

    assert result.exit_code == 0, result.output
    assert "no_counter_evidence over 17 observations" in result.output
    assert "kappa 1.000" in result.output
    assert "pass rate 12/17 → 12/17" in result.output


# ---------------------------------------------------------------------------
# The manifests: the corpus's own account of what it admitted and what it refused
# ---------------------------------------------------------------------------

#: The trials ``tolokaforge curate`` refused, keyed by the path it read them from, with
#: the observation behind each refusal. Their run directories are gitignored, so this
#: claim is re-checkable only because the manifests carry it: the task's MCP server never
#: started, every recorded call errored, and the judge scored a transcript of failures.
_REFUSED_TRIALS = {
    "results/native_shared_domain_policy_demo_20260803_062316/trials/"
    "notes_add_note_duplicate_check_policy/0": "tool_calls: 6, succeeded: 0",
    "results/native_shared_domain_policy_demo_20260803_062316/trials/"
    "notes_add_note_duplicate_check_policy/1": "tool_calls: 6, succeeded: 0",
    "results/native_shared_domain_policy_demo_20260803_062316/trials/"
    "notes_add_note_duplicate_check_policy/2": "tool_calls: 5, succeeded: 0",
    "results/native_shared_domain_gate_demo_20260804_122027/trials/"
    "notes_add_note_duplicate_check_gated/0": "tool_calls: 4, succeeded: 0",
}


@pytest.mark.parametrize(
    ("half", "expected"),
    [(_NOT_MET, _NOT_MET_BUNDLES), (_MET, _MET_BUNDLES)],
    ids=["not_met", "met"],
)
def test_each_manifest_names_exactly_the_bundles_its_half_holds(
    half: Path, expected: set[str]
) -> None:
    """Both directions, because either one alone admits a corpus that lies about itself.

    A bundle on disk the manifest does not name is a trial that entered the evidence
    unrecorded; a manifest entry with no directory is a corpus claiming a trial it lost.
    ``record_carried`` is asserted against the tree for the same reason — it is the
    manifest's claim about what a constraint reading ``status`` could be recomputed over.
    """
    manifest = _manifest(half)

    assert {entry.directory for entry in manifest.bundles} == expected
    assert {bundle.name for bundle in _bundles(half)} == expected
    for entry in manifest.bundles:
        carried = (half / entry.directory / _TOOL_LOG).exists()
        assert entry.record_carried is carried, entry.directory


@pytest.mark.parametrize("half", [_NOT_MET, _MET], ids=["not_met", "met"])
def test_every_manifest_label_is_the_one_its_own_bundle_recorded(half: Path) -> None:
    """The manifest is a reading of the bundles, and each bundle is the second source.

    ``met`` is the label the corpus exists to carry — the judge's verdict for the
    criterion — and ``agent_model`` the attribution a reader needs to know what produced
    the transcript that was labelled. Both are re-read here off the bundle's own files,
    so a manifest transcribed from the invocation rather than from the trials reds.
    """
    manifest = _manifest(half)
    criterion = yaml.safe_load(_DECLARATION.read_text())["migrations"][0]["criterion"]

    assert manifest.criterion == criterion
    for entry in manifest.bundles:
        bundle = half / entry.directory
        grade = yaml.safe_load((bundle / "grade.yaml").read_text())
        (recorded,) = [result for result in grade["criterion_results"] if result["id"] == criterion]
        task = yaml.safe_load((bundle / "task.yaml").read_text())
        assert entry.met is recorded["met"], entry.directory
        assert entry.task_id == task["task_id"], entry.directory
        assert entry.agent_model == task["model_config"]["agent"]["name"], entry.directory


def test_the_manifests_name_every_trial_the_curation_refused_and_what_it_observed() -> None:
    """The composition's other half: the graded trials that did *not* enter, by rule.

    Twenty-one graded ``checked_duplicates_first`` trials exist; seventeen are committed.
    Which four were left out, and on what evidence, was a human judgment nobody recorded
    until the manifests did — and the runs behind it are gitignored, so the observation
    travels with the corpus or not at all.
    """
    refused = {
        entry.source: entry
        for half in (_NOT_MET, _MET)
        for entry in _manifest(half).excluded
        if entry.reason.value == "environment_dead"
    }

    assert {source: entry.observation for source, entry in refused.items()} == _REFUSED_TRIALS
    assert {entry.by.value for entry in refused.values()} == {"rule"}


# ---------------------------------------------------------------------------
# The shipped arms, which took the migration this corpus is the evidence for
# ---------------------------------------------------------------------------

_SHIPPED_PACKS = _REPO / "examples"
_SHIPPED_DECLARATION = _GATED_ARM / "migration.yaml"
_SHIPPED_POLICY_DECLARATION = _POLICY_ARM / "migration.yaml"

#: The post-migration map both shipped arms hold and both shipped declarations claim. A
#: trial's score on this pack is not comparable across a change to it, so it is written
#: out here rather than read from either source.
_SHIPPED_WEIGHTS = {"llm_judge": 0.7, "trace_checks": 0.3}


def test_the_shipped_arms_reconcile_clean_through_the_default_pack_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The migration's evidence, re-verified against the packs that actually ship.

    Driven through the **default** ``--packs`` root, which is the whole re-verification
    claim: the constraint block comes from the shipped pack, so editing it changes what is
    recomputed over this frozen corpus and reds here. Every reconciliation above resolves
    the fixtures instead and would notice nothing.

    The root is relative, so the working directory is pinned to the repository rather than
    inherited — "the default resolves the shipped packs" is a claim about running from the
    repository root, and it is stated rather than assumed.
    """
    monkeypatch.chdir(_REPO)

    result = _reconcile_cli("--source", str(_copied(_CORPUS, tmp_path)), "--dry-run")

    assert result.exit_code == 0, result.output
    assert "no_counter_evidence over 17 observations" in result.output
    assert "kappa 1.000" in result.output
    assert "pass rate 12/17 → 12/17" in result.output
    assert _SHIPPED_DECLARATION.read_bytes() == _SHIPPED_POLICY_DECLARATION.read_bytes()


def test_the_shipped_narrow_folds_under_the_map_its_own_declaration_carries(
    tmp_path: Path,
) -> None:
    """Three sources for the post-migration weight map, none derived from another.

    The shipped ``grading.yaml`` holds it, the shipped ``migration.yaml`` declares it, and
    the report folds the *after* columns under it. That the first two agree is the
    reviewable fact the freed-share rule is about; that the third reads the *declaration*
    is what the fixture entry above measures from the other side, declaring no map at all
    and reporting ``0.500`` where these rows report ``0.700``.

    The pass rate is unchanged for a stated reason rather than by luck: the veto the judge
    held passes to the trace gate, which fails on exactly the five trials whose criterion
    the judge failed.
    """
    declared = yaml.safe_load(_SHIPPED_DECLARATION.read_text())["migrations"][0]
    current = yaml.safe_load((_GATED_ARM / "grading.yaml").read_text())["combine"]["weights"]
    (entry,) = reconcile_corpus(
        _copied(_CORPUS, tmp_path), replay_id="canon", packs=[_SHIPPED_PACKS], dry_run=True
    ).entries

    assert declared["combine_weights"] == _SHIPPED_WEIGHTS
    assert current == _SHIPPED_WEIGHTS
    assert entry.verdict is ReconcileVerdict.NO_COUNTER_EVIDENCE
    counterfactual = entry.counterfactual
    assert counterfactual.weights_declared == _SHIPPED_WEIGHTS
    assert len(counterfactual.trials) == 17
    for row in counterfactual.trials:
        from_the_met_arm = Path(row.trial).parent.name == "met"
        assert row.weights_after == _SHIPPED_WEIGHTS
        assert row.score_after == pytest.approx(1.0 if from_the_met_arm else 0.7)
        assert row.binary_pass_after is from_the_met_arm
        assert row.binary_pass_after is row.binary_pass_before
    assert sum(row.binary_pass_after for row in counterfactual.trials) == 12


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
