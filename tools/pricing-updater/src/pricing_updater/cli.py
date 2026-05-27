"""CLI for pricing-updater."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console

from .fetcher import (
    DEFAULT_PRICING_PATH,
    SIGNIFICANT_PRICING_CHANGE_FACTOR,
    convert_pricing,
    fetch_openrouter_models,
    find_significant_pricing_changes,
    write_pricing_json,
)

app = typer.Typer(help="Fetch and update model pricing from OpenRouter API.")
console = Console()


@app.command()
def update(
    output: Path = typer.Option(
        DEFAULT_PRICING_PATH,
        "--output",
        "-o",
        help="Path to write pricing.json (default: tolokaforge/core/data/pricing.json)",
    ),
    no_merge: bool = typer.Option(
        False,
        "--no-merge",
        help="Replace all entries instead of merging with existing",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Fetch and display pricing without writing",
    ),
) -> None:
    """Fetch latest model pricing from OpenRouter and update pricing.json."""
    console.print("[bold]Fetching model pricing from OpenRouter …[/bold]")

    try:
        models = fetch_openrouter_models()
    except Exception as exc:
        console.print(f"[red]❌ Failed to fetch from OpenRouter: {exc}[/red]")
        raise typer.Exit(1)

    console.print(f"  → Received {len(models)} models from API")

    pricing = convert_pricing(models)
    console.print(f"  → {len(pricing)} models with non-zero pricing")

    existing_models: dict[str, dict[str, float]] = {}
    if output.exists():
        try:
            with open(output) as fh:
                data = json.load(fh)
            existing_models = data.get("models", {}) or {}
        except (json.JSONDecodeError, OSError):
            existing_models = {}

    significant_changes = find_significant_pricing_changes(
        existing_models, pricing, threshold=SIGNIFICANT_PRICING_CHANGE_FACTOR
    )
    if significant_changes:
        console.print(
            f"\n[yellow]⚠️  {len(significant_changes)} model(s) shifted "
            f"≥{SIGNIFICANT_PRICING_CHANGE_FACTOR:g}× vs the previous snapshot:[/yellow]"
        )
        for model_id, old_values, new_values, ratios in significant_changes[:20]:
            console.print(
                f"  {model_id}: "
                f"input {old_values.get('input')}→{new_values.get('input')} "
                f"(×{ratios['input_ratio']:.2f}), "
                f"output {old_values.get('output')}→{new_values.get('output')} "
                f"(×{ratios['output_ratio']:.2f})"
            )
        if len(significant_changes) > 20:
            console.print(f"  … and {len(significant_changes) - 20} more")

    if dry_run:
        console.print("\n[yellow]Dry run — not writing.[/yellow]")
        # Show a sample
        for model_id in sorted(pricing)[:20]:
            p = pricing[model_id]
            console.print(f"  {model_id}: input=${p['input']}/M  output=${p['output']}/M")
        if len(pricing) > 20:
            console.print(f"  … and {len(pricing) - 20} more")
        return

    total = write_pricing_json(pricing, output, merge=not no_merge)

    console.print(f"\n[green]✅ Wrote {total} models to {output}[/green]")
    if not no_merge:
        console.print("  (merged with existing entries)")


@app.command()
def show(
    pricing_file: Path = typer.Option(
        DEFAULT_PRICING_PATH,
        "--file",
        "-f",
        help="Path to pricing.json to inspect",
    ),
    model: str | None = typer.Argument(None, help="Filter by model ID substring"),
) -> None:
    """Show current pricing table."""
    if not pricing_file.exists():
        console.print(f"[red]❌ File not found: {pricing_file}[/red]")
        raise typer.Exit(1)

    with open(pricing_file) as fh:
        data = json.load(fh)

    models = data.get("models", {})
    meta = data.get("_meta", {})

    if meta:
        console.print(f"[dim]Updated: {meta.get('updated_at', 'unknown')}[/dim]")
        console.print()

    filtered = models
    if model:
        filtered = {k: v for k, v in models.items() if model.lower() in k.lower()}

    if not filtered:
        console.print("[yellow]No models found matching filter.[/yellow]")
        return

    console.print(f"[bold]{len(filtered)} models[/bold]\n")
    for model_id in sorted(filtered):
        p = filtered[model_id]
        console.print(
            f"  {model_id:50s}  input=${p['input']:>8.3f}/M  output=${p['output']:>8.3f}/M"
        )


def main() -> None:
    """Entrypoint."""
    app()
