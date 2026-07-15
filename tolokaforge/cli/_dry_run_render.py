"""Rendering for :command:`tolokaforge run --dry-run` output.

Consumes :class:`tolokaforge.core.dry_run.DryRunSample` values and paints
one :class:`rich.panel.Panel` per sample on the shared stderr console.
The preamble is a single markup line naming how many samples render and
how many tasks the run config declared.

The module never constructs its own :class:`rich.console.Console` — every
call takes the shared console as a keyword-only argument. Under
``console.quiet = True`` (Rich's short-circuit contract, wired to
``--display=none`` via :func:`tolokaforge.cli._display.silence_console`)
every write is a no-op.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from rich.console import Console, Group
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

if TYPE_CHECKING:
    from tolokaforge.core.dry_run import DryRunSample

__all__ = [
    "render_dry_run",
    "render_dry_run_preamble",
    "render_dry_run_sample",
]


def render_dry_run_preamble(
    *,
    n_rendered: int,
    n_available: int,
    console: Console,
) -> None:
    """Emit the single-line preamble on *console*.

    Names how many samples render and how many tasks the run config
    resolved. Under ``console.quiet=True`` this is a no-op (Rich
    short-circuits every write).
    """
    console.print(
        f"[bold]Dry run:[/bold] rendering first {n_rendered} sample(s) "
        f"(of {n_available} task(s) available)"
    )


def _tools_block(tool_spec: list[dict[str, object]]) -> Syntax | Text:
    if not tool_spec:
        return Text("  (no agent tools declared)", style="muted")
    return Syntax(
        json.dumps(tool_spec, indent=2, sort_keys=False),
        "json",
        theme="ansi_dark",
        word_wrap=True,
        background_color="default",
    )


def _user_prompt_block(sample: DryRunSample) -> Text:
    if sample.user_prompt_is_literal:
        return Text(sample.user_prompt_text)
    return Text(sample.user_prompt_text, style="muted")


def render_dry_run_sample(*, sample: DryRunSample, console: Console) -> None:
    """Emit one :class:`rich.panel.Panel` describing *sample* on *console*.

    Panel body order: system prompt, blank, user prompt (literal or
    placeholder), blank, tool spec (JSON or ``(no agent tools declared)``),
    blank, model / judge / runtime one-liners. Under
    ``console.quiet=True`` this is a no-op.
    """
    tools_count = len(sample.tool_spec)
    body = Group(
        Text.from_markup("[muted]System prompt:[/muted]"),
        Text(sample.system_prompt),
        Text(""),
        Text.from_markup("[muted]User prompt:[/muted]"),
        _user_prompt_block(sample),
        Text(""),
        Text.from_markup(f"[muted]Tools ({tools_count}):[/muted]"),
        _tools_block(sample.tool_spec),
        Text(""),
        Text.from_markup(f"[muted]Model:[/muted] {sample.agent_model_line}"),
        Text.from_markup(f"[muted]Judge:[/muted] {sample.judge_model_line}"),
        Text.from_markup(f"[muted]Runtime:[/muted] {sample.runtime_line}"),
    )
    panel = Panel(
        body,
        title=f"Task {sample.task_id} · Trial {sample.trial_index}",
        title_align="left",
    )
    console.print(panel)


def render_dry_run(
    samples: list[DryRunSample],
    *,
    console: Console,
    n_available: int | None = None,
) -> None:
    """Render the preamble followed by one panel per sample.

    *n_available* defaults to ``len(samples)`` when the caller does not
    supply a separate count (i.e. every task was picked). Passes both
    numbers to :func:`render_dry_run_preamble` so the operator sees how
    the ``--dry-run-samples`` cap trimmed the render.
    """
    total = n_available if n_available is not None else len(samples)
    render_dry_run_preamble(n_rendered=len(samples), n_available=total, console=console)
    for sample in samples:
        render_dry_run_sample(sample=sample, console=console)
