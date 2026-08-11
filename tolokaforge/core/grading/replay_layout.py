"""Where a recorded trial sits on disk, for every command that reads one.

One identity, one home: a directory records a trial iff it **directly contains
``trajectory.yaml``**. That is the only file every writer produces —
``task.yaml`` is written by the conductor alone, and a trial the conductor never
reached carries none; ``grade.yaml`` is written only where a verdict exists, and
a trial nothing graded carries none. Keying identity on either removes recorded
trials from a batch by a predicate, where what a command cannot *do* with a
bundle is a question its own classification answers out loud.

The weaker filter is the deliberate cost: a directory holding a stray
``trajectory.yaml`` is a bundle, and a command pointed at its parent reports it
as a named failure rather than skipping it. Every alternative filter is a
contents check, and every contents check is a file some writer legitimately omits.

This module imports nothing from :mod:`tolokaforge.core.grading.replay`,
:mod:`tolokaforge.core.grading.trace_replay` or :mod:`tolokaforge.core.llm`, so
the commands that spend nothing still reach neither a judge nor an LLM client on
their import path — a structural property, not a promise.
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "JUDGE_REPLAY_DIRNAME",
    "RESERVED_DIRNAMES",
    "TRACE_REPLAY_DIRNAME",
    "discover_trial_bundles",
    "is_trial_bundle",
]

#: The file whose presence makes a directory a recorded trial.
BUNDLE_MARKER = "trajectory.yaml"
#: Trace replay's output subtree.
TRACE_REPLAY_DIRNAME = "trace_replay"
#: Judge replay's output subtree.
JUDGE_REPLAY_DIRNAME = "replays"
#: Directory names reserved anywhere under a source: nothing beneath one is
#: discovered, at any depth, because a previously-replayed subtree can be nested
#: arbitrarily. Neither command's output holds a ``trajectory.yaml`` today, so the
#: exclusion is what holds when either one's write set changes. The trade is
#: deliberate — a *task* named ``replays`` would hide its own trials.
RESERVED_DIRNAMES = frozenset({TRACE_REPLAY_DIRNAME, JUDGE_REPLAY_DIRNAME})


def is_trial_bundle(path: Path) -> bool:
    """Whether *path* is a directory recording a trial."""
    return path.is_dir() and (path / BUNDLE_MARKER).exists()


def discover_trial_bundles(source: Path) -> list[Path]:
    """Every recorded trial under *source*, sorted, layout-agnostic.

    Handles the recorded layouts uniformly: a single bundle dir returns itself; a
    run dir with a ``trials/<task>/<idx>/`` subtree and a flat collection of bundle
    dirs both return what they hold. Bundles are identified by directory path —
    never by string-splitting ``<task>_<idx>``, since task ids carry both hyphens
    and underscores. Nothing beneath a :data:`RESERVED_DIRNAMES` directory is
    returned, at any depth.
    """
    source = Path(source)
    if is_trial_bundle(source):
        return [source]
    return sorted(
        marker.parent
        for marker in source.rglob(BUNDLE_MARKER)
        if RESERVED_DIRNAMES.isdisjoint(marker.relative_to(source).parts)
    )
