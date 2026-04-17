"""CLI commands for configuration management.

Provides ``tolokaforge config validate`` to check run-configuration files
*before* launching a benchmark.
"""

from __future__ import annotations

import glob
from pathlib import Path

import click
import yaml
from rich.console import Console

from tolokaforge.core.config_validator import Severity, validate_run_config

console = Console()


@click.group()
def config():
    """Configuration management commands."""


@config.command()
@click.option(
    "--config",
    "config_path",
    required=True,
    help="Path to a YAML config file or a directory containing YAML configs.",
)
@click.option(
    "--strict",
    is_flag=True,
    help="Exit with non-zero status on warnings (not just errors).",
)
def validate(config_path: str, strict: bool) -> None:
    """Validate run configuration files.

    Checks schema, model parameter compatibility, API key presence,
    and orchestrator settings.

    Examples::

        tolokaforge config validate --config config/tau_manufacturing/minimax_27.yaml
        tolokaforge config validate --config config/tau_manufacturing/
        tolokaforge config validate --config "config/**/*.yaml"
    """
    paths = _resolve_paths(config_path)

    if not paths:
        console.print(f"[red]No YAML files found at {config_path!r}[/red]")
        raise SystemExit(1)

    total_errors = 0
    total_warnings = 0
    total_valid = 0

    for p in sorted(paths):
        console.print(f"\n[bold]Validating:[/bold] {p}")
        try:
            with open(p) as f:
                raw = yaml.safe_load(f)
        except Exception as exc:
            console.print(f"  [red]✗ Failed to parse YAML: {exc}[/red]")
            total_errors += 1
            continue

        if not isinstance(raw, dict):
            console.print("  [red]✗ YAML root must be a mapping[/red]")
            total_errors += 1
            continue

        result = validate_run_config(raw)

        if not result.issues:
            console.print("  [green]✓ No issues found[/green]")
            total_valid += 1
            continue

        for issue in result.issues:
            if issue.severity == Severity.ERROR:
                console.print(f"  [red]✗ {issue}[/red]")
                total_errors += 1
            elif issue.severity == Severity.WARNING:
                console.print(f"  [yellow]⚠ {issue}[/yellow]")
                total_warnings += 1
            else:
                console.print(f"  [dim]ℹ {issue}[/dim]")

        if result.ok:
            total_valid += 1

    # Summary
    console.print(f"\n[bold]Summary:[/bold] {len(paths)} file(s) checked")
    if total_errors:
        console.print(f"  [red]{total_errors} error(s)[/red]")
    if total_warnings:
        console.print(f"  [yellow]{total_warnings} warning(s)[/yellow]")
    if total_valid:
        console.print(f"  [green]{total_valid} valid[/green]")

    if total_errors:
        raise SystemExit(1)
    if strict and total_warnings:
        raise SystemExit(1)


def _resolve_paths(config_path: str) -> list[Path]:
    """Turn *config_path* into a list of concrete file paths."""
    p = Path(config_path)

    if p.is_file():
        return [p]

    if p.is_dir():
        return list(p.glob("*.yaml")) + list(p.glob("*.yml"))

    # Treat as glob pattern
    matches = glob.glob(config_path, recursive=True)
    return [Path(m) for m in matches if Path(m).is_file()]
