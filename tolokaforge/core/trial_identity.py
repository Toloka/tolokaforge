"""Single source of truth for a trial's label and output location.

A trial is identified by the triple ``(entry, task_id, trial_index)``. ``entry``
is the harness entry that owns the trial; the empty string is the single-adapter
sentinel. Both helpers branch on the entry so a single-adapter trial keeps the
two-level, no-prefix forms and a harness trial carries its entry.

The ``trial_id`` these build is an opaque label: it is compared for equality and
used for display, never parsed back into its parts (a ``task_id`` may itself
contain a colon). Identity is carried in explicit fields on the trial spec and
trajectory; this module only renders the label and the matching output subpath.
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "format_trial_id",
    "trial_identity_from_subpath",
    "trial_output_subpath",
]


def format_trial_id(entry: str, task_id: str, trial_index: int) -> str:
    """Render the opaque ``trial_id`` label for a trial.

    Harness entry → ``"{entry}:{task_id}:{trial_index}"``; single adapter
    (``entry == ""``) → ``"{task_id}:{trial_index}"``.
    """
    if entry:
        return f"{entry}:{task_id}:{trial_index}"
    return f"{task_id}:{trial_index}"


def trial_output_subpath(entry: str, task_id: str, trial_index: int) -> Path:
    """The trial's bundle directory, relative to the run's ``trials/`` root.

    Harness entry → ``<entry>/<task_id>/<trial_index>``; single adapter
    (``entry == ""``) → ``<task_id>/<trial_index>``.
    """
    if entry:
        return Path(entry) / task_id / str(trial_index)
    return Path(task_id) / str(trial_index)


def trial_identity_from_subpath(subpath: Path) -> tuple[str, str, str]:
    """Recover ``(entry, task_id, trial_index)`` from a bundle subpath.

    Inverse of :func:`trial_output_subpath`, taking a path relative to the run's
    ``trials/`` root. A two-segment ``<task_id>/<trial_index>`` is the
    single-adapter layout (empty entry); a three-segment
    ``<entry>/<task_id>/<trial_index>`` is the harness layout. The trial index is
    returned as its directory string. Any other depth is not a bundle location
    and raises — the caller must not have matched it as one.
    """
    parts = subpath.parts
    if len(parts) == 2:
        return "", parts[0], parts[1]
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    raise ValueError(
        f"not a trial bundle subpath (expected 2 or 3 segments, got {len(parts)}): {subpath}"
    )
