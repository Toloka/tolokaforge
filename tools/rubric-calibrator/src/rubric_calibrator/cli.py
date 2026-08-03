"""CLI for the rubric-judge calibrator — the trust gate.

Stage 6 of ``docs/RUBRIC_GRADING_DESIGN.md``. Runs the real judge over golden
fixtures with a (cheap) judge model, prints the per-criterion agreement report,
and applies the trust gate: a rubric judge is **not shippable** until its
agreement clears the threshold. Exits non-zero when the gate fails so CI can
block shipping an untrustworthy rubric.

Why a ``tools/`` workspace member (not a ``tolokaforge`` CLI subcommand):
calibration is complex, self-contained Python with its own deps (fixture schema,
real-judge plumbing, report rendering) and runs real inference offline from the
runner stack — exactly the "complex Python logic → tools/" case in AGENTS.md.
The bundled ``scripts/analysis/calibrate_rubric.sh`` wraps it with the repo's
``.env`` loader.

Secrets: this is the process entrypoint that spends money, so — like
``tolokaforge/cli/main.py`` — it initialises the ``SecretManager`` singleton and
mirrors provider keys into ``os.environ`` (litellm reads them there). All actual
key reads still go through ``SecretManager`` inside ``LLMClient``; this module
never reads a credential directly.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from tolokaforge.core.grading.agreement import decide_gate

from .fixture import load_fixtures
from .report import render
from .runner import run_calibration

app = typer.Typer(help="Calibrate rubric judges against golden fixtures and gate trust.")
console = Console()

#: Cheap, tool-calling-capable default judge model (the live test's choice).
DEFAULT_MODEL_REF = "openrouter/openai/gpt-4.1-mini"

#: Provider keys litellm looks up via os.environ; mirrored once at startup.
_PROVIDER_KEYS = (
    "OPENROUTER_API_KEY",
    "OPENROUTER_API_KEYS",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
)


def _init_secrets() -> None:
    """Initialise the SecretManager singleton and mirror provider keys to environ.

    Mirrors ``tolokaforge/cli/main.py`` startup: all reads still go through
    ``SecretManager``; this only makes litellm's ``os.environ`` lookups resolve.
    """
    from tolokaforge.secrets import init_default

    secrets = init_default()
    secrets.export_to_environ(list(_PROVIDER_KEYS))


@app.command()
def calibrate(
    fixtures: list[Path] = typer.Argument(
        ...,
        help="Fixture files or directories (expanded to *.yaml / *.yml).",
    ),
    model_ref: str = typer.Option(
        DEFAULT_MODEL_REF,
        "--model-ref",
        "-m",
        help="Judge model ref '<provider>/<model>'. Default is a cheap small model.",
    ),
    threshold: float = typer.Option(
        0.6,
        "--threshold",
        "-t",
        help="Minimum agreement to be shippable. κ≥0.6 is 'substantial' agreement.",
    ),
    metric: str = typer.Option(
        "kappa",
        "--metric",
        help="Which overall agreement metric the gate uses: 'kappa' or 'accuracy'.",
    ),
    max_turns: int = typer.Option(
        None,
        "--max-turns",
        help="Override the judge's per-fixture turn cap.",
    ),
) -> None:
    """Run the judge over fixtures, report agreement, and apply the trust gate."""
    _init_secrets()

    try:
        loaded = load_fixtures(fixtures)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]❌ Failed to load fixtures: {exc}[/red]")
        raise typer.Exit(2)

    console.print(
        f"[bold]Calibrating[/bold] {len(loaded)} fixture(s) with judge [cyan]{model_ref}[/cyan] …"
    )

    def _progress(outcome) -> None:
        mark = "[red]errored[/red]" if outcome.errored else "[green]graded[/green]"
        console.print(f"  → {outcome.fixture_id}: {mark}")

    run = run_calibration(
        loaded,
        model_ref=model_ref,
        max_turns=max_turns,
        on_fixture_done=_progress,
    )

    try:
        gate = decide_gate(run.report, threshold=threshold, metric=metric)
    except ValueError as exc:
        console.print(f"[red]❌ {exc}[/red]")
        raise typer.Exit(2)

    console.print()
    render(console, run, gate)

    if not gate.shippable:
        raise typer.Exit(1)


def main() -> None:
    """Entrypoint."""
    app()
