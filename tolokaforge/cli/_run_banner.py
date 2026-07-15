"""Two-line start / three-line end banners framing every ``tolokaforge run``.

The banners bookend a run on stderr: the start banner announces the run-id
and the ``file://`` URL of the (about-to-be-populated) results directory;
the end banner announces outcome + duration + the same URL + the follow-up
``tolokaforge browse <run-id>`` command. URLs are wrapped in Rich
``[link=URL]…[/link]`` markup, so OSC 8-capable terminals render them
clickable.

Both helpers accept the shared ``console`` from :mod:`tolokaforge.cli._display`
as an argument — they never construct their own ``Console``. Under
``--display=none``, :func:`tolokaforge.cli._display.silence_console` has
already set ``console.quiet = True`` and the writes short-circuit; the
stdout artifact-path emission is unaffected.
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from tolokaforge.cli._display import format_duration


def _report_url(run_dir: Path) -> str:
    """Return the absolute ``file:///`` URL for ``run_dir``, trailing-slashed."""
    return Path(run_dir).resolve().as_uri() + "/"


def print_run_start_banner(*, run_id: str, run_dir: Path, console: Console) -> None:
    """Emit the two-line start banner on ``console``.

    Layout:

        → Run: <run-id>
        → Report: file:///<abs-path>/<run-dir>/
    """
    url = _report_url(run_dir)
    console.print(f"[muted]→[/muted] Run: {run_id}")
    console.print(f"[muted]→[/muted] Report: [link={url}]{url}[/link]")


def print_run_end_banner(
    *,
    run_id: str,
    run_dir: Path,
    duration_seconds: float,
    success: bool,
    console: Console,
) -> None:
    """Emit the three-line end banner on ``console``.

    Layout on success:

        ✓ Run complete in <duration>
        → Report: file:///<abs-path>/<run-dir>/
        → Browse: tolokaforge browse <run-id>

    Layout on failure (any exception propagated to the caller):

        ✗ Run failed in <duration>
        → Report: file:///<abs-path>/<run-dir>/
        → Browse: tolokaforge browse <run-id>
    """
    url = _report_url(run_dir)
    duration = format_duration(duration_seconds)
    if success:
        console.print(f"[success]✓[/success] Run complete in {duration}")
    else:
        console.print(f"[error]✗[/error] Run failed in {duration}")
    console.print(f"[muted]→[/muted] Report: [link={url}]{url}[/link]")
    console.print(f"[muted]→[/muted] Browse: tolokaforge browse {run_id}")


__all__ = [
    "print_run_end_banner",
    "print_run_start_banner",
]
