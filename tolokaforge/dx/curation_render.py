"""Rendering for :command:`tolokaforge curate` output.

Every discovered trial gets a line — admitted with its recorded label, or rejected with
the reason and the observation behind it — because a curating run that printed only what
it kept would make the corpus's composition unreadable at the moment it is decided.

The module never constructs its own :class:`rich.console.Console`: every call takes the
shared one as a keyword-only argument.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import Console
from rich.markup import escape

if TYPE_CHECKING:
    from tolokaforge.core.grading.corpus_curation import CurationOutcome

__all__ = ["render_curation"]


def render_curation(outcome: CurationOutcome, *, dry_run: bool, console: Console) -> None:
    """Print one line per discovered trial, then what the run did with them."""
    manifest = outcome.manifest
    label = "would admit" if dry_run else "admitted"
    for entry in manifest.bundles:
        record = "record" if entry.record_carried else "no record"
        console.print(
            f"[success]{label}[/success] {escape(entry.directory)} "
            f"[dim](met={entry.met}, {record})[/dim]"
        )
    for rejection in manifest.excluded:
        console.print(
            f"[warn]rejected[/warn] {escape(rejection.source)} — "
            f"{rejection.reason.value} by {rejection.by.value} "
            f"[dim]({escape(rejection.observation)})[/dim]"
        )

    console.print(
        f"\n[bold]{len(manifest.bundles)} admitted, {len(manifest.excluded)} rejected[/bold] "
        f"for {escape(manifest.criterion)} over "
        f"{', '.join(escape(str(source)) for source in outcome.sources)}"
    )
    if outcome.written:
        console.print(f"Corpus: {escape(str(outcome.into))}")
    elif manifest.bundles:
        console.print(f"[dim]would write {escape(str(outcome.into))}[/dim]")
