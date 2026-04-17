"""CLI for eval-orchestrator: split configs and merge results."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from .merger import merge as run_merge
from .splitter import split as run_split

app = typer.Typer(
    help="Split tolokaforge run configs into shards for parallel CI, and merge results.",
)


@app.command()
def split(
    config: Path = typer.Option(
        ...,
        "--config",
        exists=True,
        help="Path to the tolokaforge run config YAML.",
    ),
    shards: int = typer.Option(
        4,
        "--shards",
        min=1,
        help="Number of parallel shards to create.",
    ),
    workers_per_shard: int = typer.Option(
        5,
        "--workers-per-shard",
        min=1,
        help="Number of workers (concurrency) inside each shard.",
    ),
    output_dir: Path = typer.Option(
        Path(".ci/shards"),
        "--output-dir",
        help="Directory to write shard configs and symlinks.",
    ),
) -> None:
    """Split a run config into N shard configs for parallel CI execution.

    Resolves tasks_glob from the config, distributes tasks across shards
    using round-robin, writes per-shard config YAMLs with symlinked task
    directories, and outputs a matrix.json for GitHub Actions.
    """
    typer.echo(f"Splitting {config} into {shards} shard(s) with {workers_per_shard} worker(s) each")

    try:
        matrix = run_split(
            config_path=config,
            num_shards=shards,
            workers_per_shard=workers_per_shard,
            output_dir=output_dir,
        )
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Wrote {len(matrix['include'])} shard config(s) to {output_dir}/")
    typer.echo(f"Matrix JSON: {output_dir / 'matrix.json'}")

    # Print matrix to stdout for capture in $GITHUB_OUTPUT
    typer.echo(json.dumps(matrix))


@app.command()
def merge(
    input_dirs: str = typer.Option(
        ...,
        "--input-dirs",
        help="Comma-separated list of shard output directories to merge.",
    ),
    output_dir: Path = typer.Option(
        Path("output/merged"),
        "--output-dir",
        help="Directory for the merged result.",
    ),
) -> None:
    """Merge shard output directories into a single combined result.

    Copies all trial data, merges run states, re-aggregates metrics,
    and writes a summary suitable for GitHub Actions step summary.
    """
    dirs = [Path(d.strip()) for d in input_dirs.split(",") if d.strip()]

    if not dirs:
        typer.echo("Error: --input-dirs must contain at least one directory.", err=True)
        raise typer.Exit(1)

    typer.echo(f"Merging {len(dirs)} shard output(s) into {output_dir}/")

    try:
        aggregate = run_merge(input_dirs=dirs, output_dir=output_dir)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    total = aggregate.get("total_trials", 0)
    passed = aggregate.get("passed", 0)
    rate = aggregate.get("success_rate_micro", 0)

    typer.echo(f"Merged: {passed}/{total} passed ({rate:.1%})")
    typer.echo(f"Results: {output_dir}/")
    typer.echo(f"Aggregate: {output_dir / 'aggregate.json'}")
    typer.echo(f"Summary: {output_dir / 'summary.md'}")


def main() -> None:
    """Entry point for the eval-orchestrator CLI."""
    app()
