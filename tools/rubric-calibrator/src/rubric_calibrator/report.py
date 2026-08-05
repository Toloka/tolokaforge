"""Render a :class:`CalibrationRun` to a rich console report.

Kept separate from the CLI so the formatting is testable in isolation and the
CLI stays a thin entrypoint.
"""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from tolokaforge.core.grading.agreement import GateDecision

from .runner import CalibrationRun


def _fmt_kappa(kappa: float | None) -> str:
    return "n/a" if kappa is None else f"{kappa:.3f}"


def render(console: Console, run: CalibrationRun, gate: GateDecision) -> None:
    """Print the per-criterion agreement table, disagreements, usage, and gate."""
    report = run.report

    table = Table(title="Per-criterion agreement", show_lines=False)
    table.add_column("criterion")
    table.add_column("n", justify="right")
    table.add_column("accuracy", justify="right")
    table.add_column("kappa", justify="right")
    for crit in report.per_criterion:
        table.add_row(
            crit.criterion_id,
            str(crit.n),
            f"{crit.accuracy:.3f}",
            _fmt_kappa(crit.kappa),
        )
    table.add_row(
        "[bold]OVERALL[/bold]",
        str(report.total_observations),
        f"[bold]{report.overall_accuracy:.3f}[/bold]",
        f"[bold]{_fmt_kappa(report.overall_kappa)}[/bold]",
    )
    console.print(table)

    if report.errored_fixture_ids:
        console.print(
            f"\n[red]Errored fixtures ({len(report.errored_fixture_ids)}):[/red] "
            f"{', '.join(report.errored_fixture_ids)}"
        )
        for outcome in run.outcomes:
            if outcome.errored:
                console.print(
                    f"  [red]✗ {outcome.fixture_id}[/red]: {outcome.judge_result.reasons}"
                )

    if report.disagreements:
        console.print(f"\n[yellow]Disagreements ({len(report.disagreements)}):[/yellow]")
        for d in report.disagreements:
            console.print(
                f"  [yellow]{d.observation_id} / {d.criterion_id}[/yellow]: "
                f"expected={d.reference_raw!r} judged={d.candidate_raw!r}"
            )
            if d.justification:
                console.print(f"      judge: {d.justification}")
    else:
        console.print("\n[green]No disagreements.[/green]")

    usage = run.total_usage
    console.print(
        f"\n[dim]Judge usage: {usage.calls} calls, "
        f"{usage.prompt_tokens} prompt + {usage.completion_tokens} completion tokens, "
        f"{usage.tool_calls} tool calls, ${usage.cost_usd:.4f}[/dim]"
    )

    observed = "n/a" if gate.observed is None else f"{gate.observed:.3f}"
    if gate.shippable:
        console.print(
            f"\n[bold green]✅ TRUST GATE PASSED[/bold green] — "
            f"{gate.metric}={observed} ≥ threshold {gate.threshold:.3f}. Rubric is shippable."
        )
    else:
        console.print(
            f"\n[bold red]❌ TRUST GATE FAILED[/bold red] — NOT shippable "
            f"({gate.metric}={observed}, threshold {gate.threshold:.3f}):"
        )
        for reason in gate.reasons:
            console.print(f"  [red]• {reason}[/red]")
