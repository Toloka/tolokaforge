"""``grade-run`` bundle discovery across the single-adapter and harness layouts.

A single-adapter run writes bundles at ``trials/<task>/<idx>/``; a harness run
with matrix trial identity writes them one level deeper at
``trials/<entry>/<task>/<idx>/``. Discovery must find both, recover each
bundle's ``(entry, task_id, trial_index)`` from its path, and never mistake a
trial's ``native/`` sidecar artifact for a bundle of its own.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.core.trial_identity import trial_output_subpath
from tolokaforge.dx.cli.grade import _discover_trials

pytestmark = pytest.mark.unit


def _touch_bundle(trials_root: Path, subpath: Path) -> None:
    """Write a bundle marker at ``trials/<subpath>/trajectory.yaml``."""
    bundle_dir = trials_root / subpath
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "trajectory.yaml").write_text("messages: []\n", encoding="utf-8")


def _identities(run_dir: Path) -> set[tuple[str, str, str]]:
    return {(t.entry, t.task_id, t.trial_idx) for t in _discover_trials(run_dir)}


def test_discovers_both_layouts_and_ignores_native_sidecar(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    trials_root = run_dir / "trials"

    # One single-adapter bundle (two-level) and two harness-entry bundles over
    # the same task (three-level).
    _touch_bundle(trials_root, trial_output_subpath("", "task_single", 0))
    _touch_bundle(trials_root, trial_output_subpath("entry_a", "task_shared", 0))
    _touch_bundle(trials_root, trial_output_subpath("entry_b", "task_shared", 0))

    # A native sidecar under the single-adapter bundle: three-level, but nested
    # inside a real bundle — must not be mistaken for a harness-entry trial.
    (trials_root / "task_single" / "0" / "native").mkdir(parents=True)
    (trials_root / "task_single" / "0" / "native" / "trajectory.yaml").write_text(
        "staged: artifact\n", encoding="utf-8"
    )
    # A native sidecar under a harness bundle (four-level) and an ordinary
    # non-bundle artifact — neither is a trajectory.yaml at a bundle depth.
    harness_native = trials_root / "entry_a" / "task_shared" / "0" / "native" / "logs"
    harness_native.mkdir(parents=True)
    (harness_native.parent / "trajectory.yaml").write_text("x\n", encoding="utf-8")
    (harness_native / "reward.txt").write_text("1.0\n", encoding="utf-8")

    discovered = _discover_trials(run_dir)

    assert _identities(run_dir) == {
        ("", "task_single", "0"),
        ("entry_a", "task_shared", "0"),
        ("entry_b", "task_shared", "0"),
    }

    # Each bundle's output subpath mirrors its on-disk location (the inverse of
    # the writer's trial_output_subpath), and no sidecar leaked in.
    by_identity = {(t.entry, t.task_id, t.trial_idx): t for t in discovered}
    assert by_identity[("", "task_single", "0")].subpath == trial_output_subpath(
        "", "task_single", 0
    )
    assert by_identity[("entry_a", "task_shared", "0")].subpath == trial_output_subpath(
        "entry_a", "task_shared", 0
    )
    assert all("native" not in t.subpath.parts for t in discovered)


def test_single_adapter_tree_discovers_exactly_its_bundles(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    trials_root = run_dir / "trials"
    for task_id, idx in (("t1", 0), ("t1", 1), ("t2", 0)):
        _touch_bundle(trials_root, trial_output_subpath("", task_id, idx))

    discovered = _discover_trials(run_dir)

    assert _identities(run_dir) == {
        ("", "t1", "0"),
        ("", "t1", "1"),
        ("", "t2", "0"),
    }
    # Every recovered bundle is a two-level single-adapter path (empty entry).
    assert all(t.entry == "" and len(t.subpath.parts) == 2 for t in discovered)
    assert all(t.label == f"{t.task_id}/{t.trial_idx}" for t in discovered)
