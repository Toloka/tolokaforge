"""``tolokaforge grade-run`` end-to-end through the real Stage 1 writer.

Directly locks Stage 1 as a prerequisite. The fixture writes every
``trajectory.yaml`` via :meth:`FileArtifactWriter.write_trajectory`
(the shipped writer, not hand-crafted YAML) so a regression that drops
``snapshot_status`` from the writer would flip every classification to
"no snapshot_status recorded" and break this test.

Three trials cover the discovery matrix:

* ``task_a/0`` — stored bundle → dispatched.
* ``task_a/1`` — ``SnapshotOutcome.OVERSIZE`` → skipped with reason.
* ``task_b/0`` — ``snapshot_status = None`` → skipped as pre-snapshot.

The CLI runs against the fixture ``_FixedScoreKind`` so the produced
``Grade`` compares equal to a canned value regardless of substrate.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from tests.unit.dx.cli.conftest import _FixedScoreKind, canned_grade
from tolokaforge.core.grading.bundle import serialize_grade_bundle
from tolokaforge.core.grading.bundle_store import LocalDiskBundleStore
from tolokaforge.core.models import Trajectory, TrialStatus
from tolokaforge.core.models.grade import Grade
from tolokaforge.core.models.trajectory import SnapshotStatus
from tolokaforge.core.output.artifacts import FileArtifactWriter
from tolokaforge.dx.cli.main import cli

pytestmark = pytest.mark.unit


def _write_workspace(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "main.py").write_bytes(b"print('hi')\n")


def _put_bundle(tmp_path: Path, store: LocalDiskBundleStore, trial_id: str, tag: str) -> str:
    workspace = tmp_path / f"workspace-{tag}"
    _write_workspace(workspace)
    bundle_dir = tmp_path / f"bundle-{tag}"
    serialize_grade_bundle(
        bundle_dir,
        trial_id=trial_id,
        initial_state={"tables": {}},
        final_state={"tables": {}},
        final_state_stable={"tables": {}},
        filesystem_root=workspace,
        checks=None,
        kb=None,
        trajectory={"llm_messages": []},
        grading_config={"combine_method": "weighted", "weights": {"custom": 1.0}},
    )
    return store.put(bundle_dir)


def _write_trajectory(
    run_dir: Path,
    task_id: str,
    trial_idx: str,
    snapshot_status: SnapshotStatus | None,
) -> Path:
    ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    trajectory = Trajectory(
        task_id=task_id,
        trial_index=int(trial_idx),
        start_ts=ts,
        end_ts=ts,
        status=TrialStatus.COMPLETED,
        messages=[],
        snapshot_status=snapshot_status,
    )
    trial_dir = run_dir / "trials" / task_id / trial_idx
    FileArtifactWriter().write_trajectory(trial_dir, trajectory)
    return trial_dir / "trajectory.yaml"


def _store_yaml(store_root: Path, tmp_path: Path) -> Path:
    text = f"type: local_disk\nroot_dir: {store_root}\n"
    path = tmp_path / "store.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_grade_run_dispatches_only_stored_trials(
    tmp_path: Path,
    register_grader_kind: Callable[[str, type], None],
) -> None:
    register_grader_kind(_FixedScoreKind.NAME, _FixedScoreKind)
    store_root = tmp_path / "store"
    store = LocalDiskBundleStore(root_dir=store_root)
    stored_uri = _put_bundle(tmp_path, store, "trial-stored", "a")
    store.close()

    run_dir = tmp_path / "run"
    _write_trajectory(
        run_dir,
        "task_a",
        "0",
        SnapshotStatus.stored(uri=stored_uri, bundle_size_bytes=1234),
    )
    _write_trajectory(
        run_dir,
        "task_a",
        "1",
        SnapshotStatus.oversize(bundle_size_bytes=40_000_000, cap_bytes=33_554_432),
    )
    _write_trajectory(run_dir, "task_b", "0", None)

    out_dir = tmp_path / "regrades"

    result = CliRunner(mix_stderr=False).invoke(
        cli,
        [
            "grade-run",
            str(run_dir),
            "--with-kind",
            _FixedScoreKind.NAME,
            "--store-config",
            str(_store_yaml(store_root, tmp_path)),
            "--out",
            str(out_dir),
        ],
    )

    assert result.exit_code == 0, (result.stderr, result.stdout)

    dispatched_grade = out_dir / "task_a" / "0" / "grade.json"
    assert dispatched_grade.exists()
    assert Grade.model_validate_json(dispatched_grade.read_text()) == canned_grade()

    assert not (out_dir / "task_a" / "1" / "grade.json").exists()
    assert not (out_dir / "task_b" / "0" / "grade.json").exists()

    combined = result.stderr + result.stdout
    assert "regraded" in combined
    assert "skip" in combined
    assert "task_a/0" in combined
    assert "task_a/1" in combined
    assert "task_b/0" in combined
    assert "bundle oversize" in combined
    assert "no snapshot_status recorded" in combined
    assert "discovered 3" in combined
    assert "regraded 1" in combined
    assert "skipped 2" in combined
    assert "failed 0" in combined
