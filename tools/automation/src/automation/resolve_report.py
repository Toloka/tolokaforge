"""Compose the "why the resolve loop did not converge" report for the needs-human PR comment.

The workflow appends a one-line outcome per iteration to ``loop_summary.md`` (did the agent write
an overlay, and what was the reprobe verdict). This reads that plus the agent's last
``decision.json`` (its stated fix-targets + notes) and turns them into a markdown block that
explains WHY convergence failed - distinguishing the two very different causes:

  * the agent produced NO overlay in the iterations (it stalled / hit its per-iteration turn
    budget - usually an upstream throttle or error on the agent's model calls, which now route
    through the LiteLLM -> OpenRouter gateway; not a model or observe problem), vs
  * the agent produced fixes but the reprobe stayed RED (the proposed policy did not make the
    fix-targets pass - a genuinely hard quirk or an over-narrow fix).

Deterministic + file-only, so it is unit-testable with fixtures.
"""

from __future__ import annotations

import json
import pathlib
import re

import typer


def counts(summary_text: str) -> tuple[int, int, int]:
    """(total, no_overlay, red) iteration counts from the appended summary lines.

    A stalled iteration is logged as "no decision produced" (nothing at all) or "no
    overlay was produced" (a decision that named fix-targets but no policy); both are
    the same stalled class for the diagnosis.
    """
    total = no_overlay = red = 0
    for line in summary_text.splitlines():
        if "Iter " not in line:
            continue
        total += 1
        low = line.lower()
        if "no overlay" in low or "no decision" in low:
            no_overlay += 1
        elif "red" in low:
            red += 1
    return total, no_overlay, red


def diagnose(total: int, no_overlay: int, red: int, max_iter: int) -> str:
    """One-paragraph plain-language cause, keyed on the stalled-vs-red split."""
    if total == 0:
        return (
            "No per-iteration outcome was recorded - the resolve loop did not run or crashed "
            "before its first iteration. Check the run log for a setup/infra failure."
        )
    if no_overlay == total:
        return (
            f"The agent produced NO overlay in any of the {total} iteration(s): it stalled or "
            "exhausted its per-iteration turn budget every time. That is almost always an "
            "upstream THROTTLE or error on the agent's model calls, which now route through the "
            "LiteLLM -> OpenRouter gateway (HTTP 429 / gateway down / bad model slug) - not a model "
            "or observe problem, since observe was clean and there was never a candidate fix to "
            "verify. Check the gateway startup + OpenRouter status in the run log; re-running when "
            "load eases usually converges."
        )
    if red and no_overlay:
        return (
            f"Mixed: the agent produced a fix in {red} iteration(s) but the reprobe stayed RED "
            "(the proposed policy did not make the fix-targets pass), and the other "
            f"{no_overlay} iteration(s) produced no overlay at all (stall / turn-limit / throttle). "
            "Both a hard-to-fix quirk and API throttling may be in play - see the per-iteration "
            "list and the run artifacts."
        )
    if red:
        return (
            f"The agent produced a fix in every iteration but the reprobe never went green across "
            f"all {total} attempt(s): the proposed policy did not resolve the fix-targets. This is "
            "a genuinely hard quirk (or the fix was too narrow / over-reaching) - a human should "
            "inspect the latest overlay + reprobe in the run artifacts."
        )
    return (
        f"Ran {total} iteration(s) without converging; see the per-iteration outcomes below and "
        "the overlay + reprobe in the run artifacts."
    )


def build_report(summary_text: str, decision: dict | None, max_iter: int) -> str:
    """Markdown block: header + per-iteration list + diagnosis + the agent's last decision."""
    total, no_overlay, red = counts(summary_text)
    body = summary_text.strip() or "- (no per-iteration outcome was recorded)"
    out = [
        f"### Why the resolve loop did not converge (ran {total}/{max_iter} iterations)",
        "",
        "**Per-iteration outcome:**",
        "",
        body,
        "",
        f"**Diagnosis.** {diagnose(total, no_overlay, red, max_iter)}",
    ]
    if decision:
        targets = decision.get("fix_targets") or []
        targets_str = ", ".join(f"`{t}`" for t in targets) if targets else "none named"
        out += ["", f"**Agent's last decision.** Fix-targets: {targets_str}."]
        notes = (decision.get("notes") or "").strip()
        if notes:
            out.append(f" Notes: {notes}")
    return "\n".join(out)


def _load_decision(path: pathlib.Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def latest_decision(resolve_dir: pathlib.Path) -> dict | None:
    """The agent's most recent decision, surviving a stalled final iteration.

    ``decision.json`` is rm'd at the top of every loop iteration, so when the LAST
    iteration stalls (no overlay, no decision) the live file is gone and the report
    would say nothing about what the agent last tried. The workflow archives each
    produced attempt as ``decision_iter<N>.json``; fall back to the highest N.
    """
    live = _load_decision(resolve_dir / "decision.json")
    if live is not None:
        return live

    def iter_no(path: pathlib.Path) -> int:
        match = re.search(r"decision_iter(\d+)", path.stem)
        return int(match.group(1)) if match else -1

    for path in sorted(resolve_dir.glob("decision_iter*.json"), key=iter_no, reverse=True):
        archived = _load_decision(path)
        if archived is not None:
            return archived
    return None


def run(resolve_dir: str, max_iter: int) -> int:
    d = pathlib.Path(resolve_dir)
    summary = ""
    sp = d / "loop_summary.md"
    if sp.exists():
        try:
            summary = sp.read_text()
        except OSError:
            summary = ""
    decision = latest_decision(d)
    print(build_report(summary, decision, max_iter))
    return 0


def cli(
    resolve_dir: str = typer.Option(
        "observation/resolve", "--dir", help="the resolve artifact dir"
    ),
    max_iter: int = typer.Option(8, "--max-iter", help="the configured MAX_ITER (for the header)"),
) -> None:
    """Print a markdown 'why it did not converge' report for the needs-human PR comment."""
    try:
        code = run(resolve_dir, max_iter)
    except Exception as exc:  # a report must never fail the needs-human step
        print(f"(could not compose the resolve report: {exc})")
        code = 0
    raise typer.Exit(code)
