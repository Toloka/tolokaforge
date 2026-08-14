"""Rendering for :command:`tolokaforge retrace` output.

Consumes the outcomes
:func:`~tolokaforge.core.grading.trace_replay.run_trace_replay_batch` returns and the
report built off them, and paints both on the shared stderr console. Every value
printed is read off those two objects — the replay engine computes, this module
reads.

The module never constructs its own :class:`rich.console.Console`: every call takes
the shared one as a keyword-only argument. Under ``console.quiet = True`` (wired to
``--display=none``) every write is a no-op.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console
from rich.markup import escape

from tolokaforge.core.grading.trace_replay import (
    ConstraintDiscrimination,
    TraceReplayOutcomeStatus,
)

if TYPE_CHECKING:
    from tolokaforge.core.grading.trace_replay import (
        ConstraintDiscriminationRow,
        TraceReplayReport,
        TrialTraceReplayOutcome,
    )

__all__ = [
    "render_trace_replay_dispositions",
    "render_trace_replay_report",
]

# A non-discriminating verdict is styled as a warning although the command exits zero:
# it is a finding an author has to act on, not an operational failure.
_VERDICT_STYLE = {
    ConstraintDiscrimination.DISCRIMINATING: "success",
    ConstraintDiscrimination.ALWAYS_TRUE: "warn",
    ConstraintDiscrimination.ALWAYS_FALSE: "warn",
    ConstraintDiscrimination.UNDECIDED_IN_PART: "warn",
    ConstraintDiscrimination.NEVER_DECIDED: "muted",
    ConstraintDiscrimination.NOT_MEASURED: "muted",
}


def _relative(bundle: Path, source: Path) -> str:
    try:
        return str(bundle.relative_to(source))
    except ValueError:
        return str(bundle)


def _count(outcomes: Sequence[TrialTraceReplayOutcome], *statuses: TraceReplayOutcomeStatus) -> int:
    return sum(outcome.status in statuses for outcome in outcomes)


def _gate_notes(outcome: TrialTraceReplayOutcome) -> str:
    """What the authoring gate could not answer for *this* bundle, if anything.

    Attribution rather than reasons: the reasons are identical across bundles that
    recorded the same tool list and are printed once for the run, but *which* bundles
    the gate could not answer for is per-bundle and is lost there.
    """
    report = outcome.override_authoring
    if report is None:
        return ""
    notes = ["override unchecked"] if report.unchecked else []
    if report.advisories:
        notes.append(f"override advisories: {len(report.advisories)}")
    return f" [warn]({', '.join(notes)})[/warn]" if notes else ""


def _disposition(outcome: TrialTraceReplayOutcome, bundle: str) -> str:
    if outcome.status is TraceReplayOutcomeStatus.SKIPPED_NOT_APPLICABLE:
        return f"[warn]skip (not applicable)[/warn] {bundle}"
    if outcome.status is TraceReplayOutcomeStatus.SKIPPED_NO_TASK:
        # The reason embeds recorded trajectory text; escape it so a bracketed
        # fragment prints rather than vanishing as console markup.
        return f"[warn]skip (no task)[/warn] {bundle} — {escape(outcome.reason or '')}"
    if outcome.status is TraceReplayOutcomeStatus.FAILED:
        return f"[error]failed[/error] {bundle} — {outcome.reason}"
    evidence = outcome.evidence
    record = "record present" if evidence and evidence.tool_log_present else "no record"
    result = outcome.result
    if result is None:
        return f"[success]would re-check[/success] {bundle} — {record}"
    facts = [f"score {result.score:.2f}"]
    if result.winning_path:
        facts.append(f"route {result.winning_path}")
    facts += ["gate failed" if result.gate_failed else "gate ok", record]
    return f"[success]re-checked[/success] {bundle} — {', '.join(facts)}"


def render_trace_replay_dispositions(
    outcomes: Sequence[TrialTraceReplayOutcome],
    *,
    source: Path,
    dry_run: bool,
    console: Console,
) -> None:
    """One line per discovered bundle: what happened to it, and why where it did not.

    A skip and a failure are both stated — a bundle that vanished from the output
    would leave the batch's size unaccountable — and a bundle whose supplied block
    the gate could not check carries that on its own line.
    """
    for outcome in outcomes:
        line = _disposition(outcome, _relative(outcome.bundle, source))
        console.print(line + _gate_notes(outcome))
    eligible = _count(
        outcomes, TraceReplayOutcomeStatus.REPLAYED, TraceReplayOutcomeStatus.WOULD_REPLAY
    )
    console.print(
        f"\n[bold]{'Would re-check' if dry_run else 'Re-checked'}:[/bold] {eligible} eligible, "
        f"{_count(outcomes, TraceReplayOutcomeStatus.SKIPPED_NOT_APPLICABLE)} "
        "skipped-not-applicable, "
        f"{_count(outcomes, TraceReplayOutcomeStatus.SKIPPED_NO_TASK)} skipped-no-task, "
        f"{_count(outcomes, TraceReplayOutcomeStatus.FAILED)} failed-with-reason"
    )


def _row_label(row: ConstraintDiscriminationRow) -> str:
    inside = f"{row.route}/{row.constraint_id}" if row.route else row.constraint_id
    return f"{row.task_id} · {inside}"


def _row_line(row: ConstraintDiscriminationRow) -> str:
    style = _VERDICT_STYLE[row.verdict]
    decided = "" if row.decided_verdict is None else f", decided {str(row.decided_verdict).lower()}"
    return (
        f"  {_row_label(row)}: [{style}]{row.verdict.value}[/{style}]{decided} — "
        f"evaluated {row.trials_evaluated}, decided {row.trials_decided} "
        f"({row.passed_trials} passed, {row.failed_trials} failed), "
        f"undecided {row.undecided_trials}, agrees with the recorded verdict on "
        f"{row.agreed_with_recorded_pass}/{row.trials_labelled}"
    )


def _render_gate_notes(report: TraceReplayReport, *, console: Console) -> None:
    """The supplied block's advisories and the rules no bundle's tool set could answer.

    Printed once for the run and deduplicated, because the gate's answer is a function
    of the recorded tool list and a run repeats one task's list across its trials. A
    gate that ran and found nothing prints nothing; one that could not run says so, so
    an operator never reads a clean bill of health off a check that never happened.
    """
    notes = report.override_authoring
    if notes is None:
        return
    for advisory in notes.advisories:
        console.print(f"  [warn]! {advisory}[/warn]")
    for skip in notes.unchecked:
        console.print(f"  [warn]? not checked — {skip}[/warn]")


def _evidence_line(report: TraceReplayReport) -> str:
    evidence = report.evidence
    stamps = ", ".join(
        f"{stamp} × {count}" for stamp, count in sorted(evidence.schema_versions.items())
    )
    return (
        f"[bold]Evidence:[/bold] {evidence.bundles_read} bundles read, "
        f"{evidence.bundles_with_tool_log} carried a tool-call record, "
        f"{evidence.bundles_skipped} skipped, "
        f"{evidence.bundles_no_task} with no task snapshot, "
        f"{evidence.bundles_failed} failed, "
        f"{evidence.bundles_predating_call_ids} predating call ids, "
        f"{evidence.bundles_redacted} redacted"
        + (f"; bundle schema versions {stamps}" if stamps else "")
    )


def render_trace_replay_report(
    report: TraceReplayReport,
    *,
    artifacts_dir: Path | None,
    console: Console,
) -> None:
    """The constraint table, what the corpus carried, and where the artifacts landed.

    An empty table is stated as one rather than drawn: over a dry run, or a corpus
    whose every bundle skipped or failed, nothing was measured, and a header with no
    rows under it reads as a corpus in which every constraint behaved.

    The route-scoping denominator prints only where a row is route-scoped, because
    that is where unanimity on a route otherwise reads as a corpus-wide claim.
    ``artifacts_dir`` is ``None`` for a dry run, which wrote nothing to point at.
    """
    console.print("\n[bold]Constraints[/bold] (per task, in declaration order):")
    if not report.discrimination:
        console.print("  [warn]no constraint was measured[/warn]")
    for row in report.discrimination:
        console.print(_row_line(row))
    if any(row.route for row in report.discrimination):
        console.print(f"  [muted]{report.route_scoping}[/muted]")

    _render_gate_notes(report, console=console)
    console.print(_evidence_line(report))
    if artifacts_dir is not None:
        console.print(f"Replay artifacts: {artifacts_dir}")
