"""Typer CLI for demo-recorder.

Commands
--------
``demo-recorder record``
    Record a single trajectory into a split-screen demo video (or frame set).

``demo-recorder record-all``
    Batch-record every trajectory found under a run directory.
"""

from __future__ import annotations

from pathlib import Path

import typer

from demo_recorder.batch import record_all
from demo_recorder.recorder import (
    DEFAULT_FRAMES_DIR,
    DEFAULT_MOCK_WEB_URL,
    DEFAULT_OUTPUT_VIDEO,
    DEFAULT_TRAJECTORY_PATH,
    record_single,
)

app = typer.Typer(
    name="demo-recorder",
    help="Generate split-screen mobile demo videos from tolokaforge trajectories.",
    add_completion=False,
)


@app.command()
def record(
    trajectory: Path = typer.Option(
        DEFAULT_TRAJECTORY_PATH,
        help="Path to a trajectory.yaml file.",
    ),
    frames_dir: Path = typer.Option(
        DEFAULT_FRAMES_DIR,
        help="Directory to write individual PNG frames.",
    ),
    output: Path = typer.Option(
        DEFAULT_OUTPUT_VIDEO,
        help="Output MP4 file path.",
    ),
    mock_web_url: str = typer.Option(
        DEFAULT_MOCK_WEB_URL,
        help="Base URL of the mock-web service.",
    ),
    task_yaml: Path | None = typer.Option(
        None,
        help="Explicit path to task.yaml (auto-inferred when omitted).",
    ),
    frames_only: bool = typer.Option(
        False,
        help="Capture PNG frames only and skip MP4 stitching.",
    ),
    allow_default_start: bool = typer.Option(
        False,
        help="Allow fallback start URL when task initial_app URL cannot be inferred.",
    ),
) -> None:
    """Record a single trajectory into a split-screen demo video."""
    record_single(
        trajectory_path=trajectory,
        frames_dir=frames_dir,
        output=output,
        mock_web_url=mock_web_url,
        task_yaml=task_yaml,
        frames_only=frames_only,
        allow_default_start=allow_default_start,
    )


@app.command("record-all")
def record_all_cmd(
    run_dir: Path | None = typer.Option(
        None,
        help="Run directory containing trials/*/0/trajectory.yaml.",
    ),
    output_dir: Path = typer.Option(
        Path("demos"),
        help="Destination for rendered MP4 files.",
    ),
    workers: int = typer.Option(
        8,
        help="Number of parallel render workers.",
    ),
    prefix: str = typer.Option(
        "mobile_run_",
        help="Run prefix for auto-detection of latest run directory.",
    ),
    frames_root: Path = typer.Option(
        Path("scratchpad") / "demo_frames",
        help="Root directory to store per-task frame folders.",
    ),
    keep_frames: bool = typer.Option(
        False,
        help="Keep per-task frame folders under --frames-root.",
    ),
    frames_only: bool = typer.Option(
        False,
        help="Capture frames only; skip MP4 generation.",
    ),
) -> None:
    """Batch-record demos for every trajectory in a run directory."""
    exit_code = record_all(
        run_dir=run_dir,
        output_dir=output_dir,
        workers=workers,
        prefix=prefix,
        frames_root=frames_root,
        keep_frames=keep_frames,
        frames_only=frames_only,
    )
    raise SystemExit(exit_code)


if __name__ == "__main__":
    app()
