"""``tolokaforge grade-run`` — one failure among successes exits 1.

Same real-writer discipline as the end-to-end test — every
``trajectory.yaml`` is emitted via
:meth:`FileArtifactWriter.write_trajectory`. Trial A carries a valid
stored URI; trial B carries a well-formed URI pointing at a bundle
that does not exist in the store. The batch dispatches both, records
one success and one failure, prints per-trial lines, and exits 1
without stopping mid-batch.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from tests.unit.dx.cli.conftest import _FixedScoreKind
from tolokaforge.core.grading.bundle import serialize_grade_bundle
from tolokaforge.core.grading.bundle_store import (
    LocalDiskBundleStore,
    build_bundle_uri,
)
from tolokaforge.core.models import Trajectory, TrialStatus
from tolokaforge.core.models.trajectory import SnapshotStatus
from tolokaforge.core.output.artifacts import FileArtifactWriter
from tolokaforge.dx.cli.main import cli

pytestmark = pytest.mark.unit


def _write_workspace(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "main.py").write_bytes(b"x = 1\n")


def _make_stored_uri(tmp_path: Path, store: LocalDiskBundleStore) -> str:
    workspace = tmp_path / "workspace-ok"
    _write_workspace(workspace)
    bundle_dir = tmp_path / "bundle-ok"
    serialize_grade_bundle(
        bundle_dir,
        trial_id="trial-ok",
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
    run_dir: Path, task_id: str, trial_idx: str, snapshot_status: SnapshotStatus
) -> None:
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


def _store_yaml(store_root: Path, tmp_path: Path) -> Path:
    text = f"type: local_disk\nroot_dir: {store_root}\n"
    path = tmp_path / "store.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_grade_run_records_one_failure_and_exits_one(
    tmp_path: Path,
    register_grader_kind: Callable[[str, type], None],
) -> None:
    register_grader_kind(_FixedScoreKind.NAME, _FixedScoreKind)
    store_root = tmp_path / "store"
    store = LocalDiskBundleStore(root_dir=store_root)
    ok_uri = _make_stored_uri(tmp_path, store)
    store.close()

    missing_uri = build_bundle_uri("local_disk", "d" * 64)

    run_dir = tmp_path / "run"
    _write_trajectory(
        run_dir, "task_a", "0", SnapshotStatus.stored(uri=ok_uri, bundle_size_bytes=1234)
    )
    _write_trajectory(
        run_dir, "task_b", "0", SnapshotStatus.stored(uri=missing_uri, bundle_size_bytes=1234)
    )

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

    assert result.exit_code == 1, (result.stderr, result.stdout)
    assert (out_dir / "task_a" / "0" / "grade.json").exists()
    assert not (out_dir / "task_b" / "0" / "grade.json").exists()

    combined = result.stderr + result.stdout
    assert "regraded" in combined
    assert "failed" in combined
    assert "task_a/0" in combined
    assert "task_b/0" in combined
    assert "regraded 1" in combined
    assert "failed 1" in combined
