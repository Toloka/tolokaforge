"""Bundle identity — the one rule every offline command discovers through.

Locks :mod:`tolokaforge.core.grading.replay_layout`:

* a directory records a trial iff it **directly contains** ``trajectory.yaml`` —
  the only file every writer produces, so no recorded trial is removed from a
  batch by a predicate;
* the recorded layouts are handled uniformly — a run dir with a
  ``trials/<task>/<idx>/`` subtree, a flat collection of bundle dirs, and a single
  bundle dir;
* an authored task pack is not a batch of bundles, which is the hazard a
  ``task.yaml``-keyed rule would have;
* nothing beneath a reserved directory name is discovered, at any depth, so
  neither replay command reads the other's output.

What a command cannot *do* with a discovered bundle is its own classification's
answer, locked where that classification lives.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.core.grading.replay_layout import discover_trial_bundles, is_trial_bundle

pytestmark = [pytest.mark.unit, pytest.mark.grading]

#: An authored pack corpus: every directory carries ``task.yaml`` and none carries
#: a ``trajectory.yaml``. Read from the tree rather than staged in ``tmp_path``,
#: because the claim is about the packs this repository actually ships.
_AUTHORED_PACKS = Path("tests/data/grading_parity")


def _bundle(trial_dir: Path, *, files: tuple[str, ...] = ("trajectory.yaml",)) -> Path:
    trial_dir.mkdir(parents=True, exist_ok=True)
    for name in files:
        (trial_dir / name).write_text("task_id: refund_task\n", encoding="utf-8")
    return trial_dir


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        (("trajectory.yaml",), True),
        (("trajectory.yaml", "task.yaml", "grade.yaml"), True),
        (("task.yaml", "grade.yaml"), False),
        ((), False),
    ],
    ids=["the_marker_alone", "a_complete_bundle", "a_pack_directory", "an_empty_directory"],
)
def test_a_directory_records_a_trial_iff_it_directly_holds_the_trajectory(
    tmp_path: Path, files: tuple[str, ...], expected: bool
) -> None:
    """One file decides it, and it is the one every writer produces.

    ``task.yaml`` is written by the conductor alone and ``grade.yaml`` only where a
    verdict exists, so a trial that never ran or was never graded carries neither —
    which is how a recorded trial disappears from a batch that keys identity on
    them.
    """
    assert is_trial_bundle(_bundle(tmp_path / "trial", files=files)) is expected


def test_a_trajectory_a_directory_does_not_directly_hold_is_not_its_own(tmp_path: Path) -> None:
    """*Directly*: a parent two levels above a bundle is not itself one, or a run dir
    would be discovered as a trial beside the trials it holds."""
    run = tmp_path / "run"
    _bundle(run / "trials" / "refund_task" / "0")

    assert is_trial_bundle(run) is False
    assert discover_trial_bundles(run) == [run / "trials" / "refund_task" / "0"]


def test_discovery_is_layout_agnostic_over_the_three_recorded_shapes(tmp_path: Path) -> None:
    """The layouts a source can arrive in, all answered by one walk."""
    nested = tmp_path / "nested"
    subtree = [_bundle(nested / "trials" / "refund_task" / str(index)) for index in (0, 1)]
    flat = tmp_path / "flat"
    loose = [_bundle(flat / name) for name in ("AE-BDG-002_1", "AE-BDG-003_0")]

    assert discover_trial_bundles(nested) == sorted(subtree)
    assert discover_trial_bundles(flat) == sorted(loose)
    assert discover_trial_bundles(flat / "AE-BDG-002_1") == [flat / "AE-BDG-002_1"]


def test_an_authored_pack_tree_is_not_a_batch_of_bundles() -> None:
    """The hazard runs the other way from the one a grade-keyed rule had.

    Authored packs carry ``task.yaml`` and no ``trajectory.yaml``, so a rule keyed
    on the task snapshot would read a pack tree as a corpus of trials. Keyed on the
    trajectory it cannot: nothing in the tree recorded an episode.
    """
    assert _AUTHORED_PACKS.is_dir(), _AUTHORED_PACKS
    assert list(_AUTHORED_PACKS.glob("*/task.yaml")), "the fixture pack tree lost its task files"

    assert discover_trial_bundles(_AUTHORED_PACKS) == []


@pytest.mark.parametrize(
    "nested_under",
    [
        pytest.param(Path("trace_replay") / "earlier", id="trace_replays_output_at_the_top"),
        pytest.param(Path("replays") / "earlier", id="judge_replays_output_at_the_top"),
        pytest.param(Path("trials") / "replays" / "earlier", id="judge_replays_output_nested"),
        pytest.param(Path("trials") / "trace_replay" / "earlier", id="trace_replays_output_nested"),
    ],
)
def test_a_bundle_under_a_reserved_directory_is_not_discovered(
    tmp_path: Path, nested_under: Path
) -> None:
    """Two directory names are reserved anywhere under a source, at any depth.

    ``trace_replay/`` is one replay command's output and ``replays/`` is the
    other's, and a source re-pointed at a run that already holds either would
    otherwise re-check what sits under it. The names are written out here rather
    than imported: reserving a name is a claim about the string, and a test that
    read it off the module could not tell a renamed constant from a widened rule.

    At any depth, because a previously-replayed subtree can be nested arbitrarily
    under whatever the operator points at. The deliberate cost is that a *task*
    named ``replays`` would hide its own trials, which is why both names are
    documented as reserved rather than left to be discovered.
    """
    live = _bundle(tmp_path / "trials" / "refund_task" / "0")
    _bundle(tmp_path / nested_under / "refund_task" / "0")

    assert discover_trial_bundles(tmp_path) == [live]
