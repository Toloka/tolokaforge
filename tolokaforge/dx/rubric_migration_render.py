"""Rendering for :command:`tolokaforge reconcile` output.

Consumes the report :func:`~tolokaforge.core.grading.rubric_migration.reconcile_corpus`
returns and paints it on the shared stderr console. Every value printed is read off that
report — the bar decides, this module reads.

The 2×2 contingency table is printed for every entry, never only where it is interesting: a
corpus whose mass sits in one cell is a designed experiment, and the table is what makes that
visible where κ and accuracy alone would not. The declared mode and its residual claim are
printed as declared and never compared with the other mode — on a corpus with no
disagreements the evidence cannot choose between narrowing and retiring, so a line
suggesting it could would read as a recommendation the evidence does not support.

The counterfactual is painted per trial and labelled as evidence rather than as a gate: no
verdict, exit code or refusal reads it, and a reader who mistook it for one would take a finite
corpus as licence for an unbounded claim.

The module never constructs its own :class:`rich.console.Console`: every call takes the
shared one as a keyword-only argument. Under ``console.quiet = True`` (wired to
``--display=none``) every write is a no-op.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console

from tolokaforge.core.grading.rubric_migration import ReconcileVerdict

if TYPE_CHECKING:
    from tolokaforge.core.grading.rubric_migration import (
        DisagreementRow,
        ReconciledEntry,
        ReconcileReport,
    )

__all__ = ["render_reconcile_report"]

_VERDICT_STYLE = {
    ReconcileVerdict.NO_COUNTER_EVIDENCE: "success",
    ReconcileVerdict.INSUFFICIENT_EVIDENCE: "warn",
    ReconcileVerdict.REFUSED: "error",
}


def _number(value: float | None) -> str:
    return "undefined" if value is None else f"{value:.3f}"


def _headline(entry: ReconciledEntry) -> str:
    style = _VERDICT_STYLE[entry.verdict]
    tasks = ", ".join(entry.task_ids)
    return (
        f"\n[bold]{entry.criterion}[/bold] ({tasks}) — declared [bold]{entry.mode.value}[/bold]"
        f", [{style}]{entry.verdict.value}[/{style}] over {entry.observations} observations"
    )


def _contingency_lines(entry: ReconciledEntry) -> list[str]:
    table = entry.contingency
    return [
        "  judge \\ constraint      passed  failed",
        f"  met                    {table.judge_met_constraint_passed:>6}"
        f"  {table.judge_met_constraint_failed:>6}",
        f"  not met                {table.judge_not_met_constraint_passed:>6}"
        f"  {table.judge_not_met_constraint_failed:>6}",
        f"  accuracy {_number(entry.accuracy)}, kappa {_number(entry.kappa)}",
    ]


def _disagreement_lines(rows: Sequence[DisagreementRow], *, direction: str) -> list[str]:
    lines = []
    for row in rows:
        waived = (
            "[muted]acknowledged[/muted]"
            if row.acknowledged_reason is not None
            else "[error]unacknowledged[/error]"
        )
        lines.append(f"  {direction} · {row.trial} ({waived})")
        lines.append(f"    judge: {row.justification}")
        if row.acknowledged_reason is not None:
            lines.append(f"    waived because: {row.acknowledged_reason}")
    return lines


def _weights(weights: Mapping[str, float]) -> str:
    return "{" + ", ".join(f"{name}: {value:g}" for name, value in sorted(weights.items())) + "}"


def _counterfactual_lines(entry: ReconciledEntry) -> list[str]:
    """The declared map's per-trial effect, printed as evidence and never as a verdict.

    The declared map is named on its own line even where it moves nothing, because that is the
    reviewable fact: the freed-share rule is satisfied by declaring a map, and an author who
    shifts nothing declares the identity map. The veto columns are printed beside the weight
    columns for the measured reason that a ``required`` criterion frees no share at all — the
    two maps are then identical and the vetoes are the only thing that moved.
    """
    counterfactual = entry.counterfactual
    declared = counterfactual.weights_declared
    lines = [
        "  counterfactual under the map this entry declares"
        f" ({'none declared' if declared is None else _weights(declared)}):"
    ]
    for row in counterfactual.trials:
        lines.append(f"    {row.trial}")
        lines.append(
            f"      judge {row.judge_component_before:.3f} → {row.judge_component_after:.3f}"
            f" · trial {row.score_before:.3f}/{row.binary_pass_before}"
            f" → {row.score_after:.3f}/{row.binary_pass_after}"
        )
        lines.append(
            f"      weights {_weights(row.weights_before)} → {_weights(row.weights_after)}"
            f" · vetoes {row.vetoes_before} → {row.vetoes_after}"
        )
    for gap in counterfactual.unrecomputed_trials:
        lines.append(
            f"    [warn]not recomputed[/warn] · {gap.trial} ({gap.gap.value}): {gap.reason}"
        )
    if counterfactual.trials:
        passed_before = sum(row.binary_pass_before for row in counterfactual.trials)
        passed_after = sum(row.binary_pass_after for row in counterfactual.trials)
        total = len(counterfactual.trials)
        lines.append(
            f"    pass rate {passed_before}/{total} → {passed_after}/{total}"
            " [muted](evidence, not a gate — nothing here decides the verdict)[/muted]"
        )
    return lines


def _render_entry(entry: ReconciledEntry, *, console: Console) -> None:
    console.print(_headline(entry))
    if entry.residual_kind is not None:
        console.print(
            f"  residual ({entry.residual_kind.value}), as the author declared it: "
            f"{entry.residual_reason}"
        )
    for line in _contingency_lines(entry):
        console.print(line)
    for line in _disagreement_lines(entry.strict_disagreements, direction="strict"):
        console.print(line)
    for line in _disagreement_lines(entry.permissive_disagreements, direction="permissive"):
        console.print(line)
    for line in _counterfactual_lines(entry):
        console.print(line)
    for excluded in entry.excluded_trials:
        console.print(
            f"  [warn]no observation[/warn] · {excluded.trial} "
            f"({excluded.exclusion.value}): {excluded.reason}"
        )
    for refusal in entry.refusals:
        console.print(f"  [error]refused[/error] ({refusal.kind.value}): {refusal.message}")
    if not entry.gates_the_exit_code:
        console.print("  [muted]a candidate converts nothing, so its verdict gates nothing[/muted]")


def render_reconcile_report(
    report: ReconcileReport, *, artifacts_dir: Path | None, console: Console
) -> None:
    """Every reconciled entry, what the corpus carried, and where the report landed.

    ``artifacts_dir`` is ``None`` for a dry run, which wrote nothing to point at.
    """
    console.print(
        f"[bold]Reconciled[/bold] {report.trials_read} trials under {report.source} against "
        f"{', '.join(report.packs_searched)}"
    )
    console.print(f"  [muted]reference: {report.reference_labeller}[/muted]")
    console.print(f"  [muted]candidate: {report.candidate_labeller}[/muted]")
    for entry in report.entries:
        _render_entry(entry, console=console)
    for unreadable in report.unreadable_trials:
        console.print(f"\n[error]unreadable[/error] · {unreadable.trial}: {unreadable.reason}")
    if artifacts_dir is not None:
        console.print(f"\nReconcile report: {artifacts_dir}")
