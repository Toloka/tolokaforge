"""``tolokaforge reconcile``'s console — how it accounts for the bundles it read.

``trials_read`` counts every bundle discovered under ``--source``, and a trial the
substrate killed is discovered now. "Reconciled 6 trials" over a corpus that
reconciled five of them is a number a reader would take for the denominator, so
the header names the excluded count beside it — and says nothing where there is
nothing to say.
"""

from __future__ import annotations

import pytest
from rich.console import Console

from tolokaforge.core.grading.rubric_migration import CorpusExclusion, ReconcileReport
from tolokaforge.dx.rubric_migration_render import render_reconcile_report

pytestmark = pytest.mark.unit

_EXCLUSION = CorpusExclusion(
    bundle="/runs/r1/trials/billing/0",
    reason=(
        "the trial was aborted before it was measured (termination_reason: provision_error), "
        "so it recorded no task.yaml and speaks to no pack's migration"
    ),
)


def _rendered(*excluded: CorpusExclusion) -> str:
    report = ReconcileReport(
        source="/runs/r1",
        replay_id="r1",
        packs_searched=["/packs"],
        reference_labeller="judge",
        candidate_labeller="constraint",
        trials_read=6,
        entries=[],
        unreadable_trials=[],
        excluded_bundles=list(excluded),
    )
    console = Console(record=True, width=200)
    render_reconcile_report(report, artifacts_dir=None, console=console)
    return " ".join(console.export_text().split())


def test_the_header_names_the_excluded_count_beside_the_trials_it_read() -> None:
    """Both numbers, so the denominator a reader takes away is the right one."""
    printed = _rendered(_EXCLUSION)

    assert "Reconciled 6 trials" in printed
    assert "(1 excluded: no task snapshot)" in printed


def test_an_excluded_bundle_is_named_with_its_reason() -> None:
    """Excluded, not unreadable: the corpus is smaller and nothing is broken."""
    printed = _rendered(_EXCLUSION)

    assert f"excluded · {_EXCLUSION.bundle}" in printed
    assert "aborted before it was measured" in printed
    assert "unreadable" not in printed


def test_a_corpus_that_excluded_nothing_says_nothing_about_exclusions() -> None:
    """The parenthetical is a fact about this corpus, not decoration on every run."""
    printed = _rendered()

    assert "Reconciled 6 trials" in printed
    assert "excluded" not in printed
