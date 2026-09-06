"""Unit tests for ``InProcessConductor._produce_grade_bundle`` — the
trial-end snapshot producer seam.

Exercises all four :class:`SnapshotOutcome` values reachable from the
seam under the four gate combinations: mode off, ungraded trial,
successful produce + store, oversize discard, and produce failure. Every
outcome is written verbatim onto ``trajectory.snapshot_status``; the
trial-run coroutine never re-raises even on a produce failure (contained
by the seam so the run continues).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.core.conductor import InProcessConductor, _TrialSetup
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import (
    EvaluationConfig,
    Grade,
    GraderConfig,
    LocalDiskBundleStoreConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    SnapshotBundleConfig,
    SnapshotOutcome,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.trial import EnvEndpoints, TrialSpec
from tolokaforge.runner.models import TaskDescription

pytestmark = pytest.mark.unit


def _make_trajectory(*, graded: bool = True) -> Trajectory:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    grade = Grade(score=1.0, binary_pass=True) if graded else None
    return Trajectory(
        task_id="t1",
        trial_index=0,
        start_ts=now,
        end_ts=now,
        status=TrialStatus.COMPLETED,
        messages=[],
        grade=grade,
    )


def _make_setup() -> _TrialSetup:
    return _TrialSetup(
        trial_id="t1:0",
        trial_idx=0,
        task_dir=Path("/tmp/task"),
        trial_dir=Path("/tmp/trial"),
        env_state=MagicMock(),
        adapter_env=MagicMock(),
        tool_schemas=[],
        tool_executor=MagicMock(),
        user_tool_schemas=[],
        user_tool_executor=None,
    )


def _make_spec() -> TrialSpec:
    return TrialSpec(
        trial_id="t1:0",
        run_id="test-run",
        attempt_id=0,
        worker_id=None,
        task=TaskDescription(
            task_id="t1",
            name="t1",
            category="test",
            description="stub",
            adapter_type="native",
            system_prompt="",
        ),
        agent_model_config=ModelConfig(provider="anthropic", name="stub"),
        max_turns=10,
        default_tool_timeout_s=30.0,
        env_endpoints=EnvEndpoints(db_url="http://db:8000", runner_url="http://runner:50051"),
    )


def _make_conductor(*, snapshot: SnapshotBundleConfig | None) -> InProcessConductor:
    config = RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4")},
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir="/tmp/test_output"),
        grader=GraderConfig(expose_substrate=True, snapshot=snapshot),
    )
    return InProcessConductor(
        adapter=MagicMock(),
        artifact_writer=MagicMock(),
        config=config,
        logger=StructuredLogger("test"),
        agent_client=MagicMock(),
        runtime_backend=MagicMock(),
        trial_grader=MagicMock(),
        output_dir=Path("/tmp/test"),
    )


class TestSnapshotDisabled:
    def test_produce_bundle_noop_when_snapshot_disabled(self) -> None:
        conductor = _make_conductor(snapshot=None)
        trajectory = _make_trajectory()
        conductor._produce_grade_bundle(_make_spec(), _make_setup(), trajectory)
        assert trajectory.snapshot_status is None
        conductor.runtime_backend.build_grade_bundle.assert_not_called()

    def test_produce_bundle_noop_when_grade_is_none(self, tmp_path: Path) -> None:
        snapshot = SnapshotBundleConfig(
            enabled=True,
            store=LocalDiskBundleStoreConfig(root_dir=str(tmp_path)),
        )
        conductor = _make_conductor(snapshot=snapshot)
        trajectory = _make_trajectory(graded=False)
        conductor._produce_grade_bundle(_make_spec(), _make_setup(), trajectory)
        assert trajectory.snapshot_status is None
        conductor.runtime_backend.build_grade_bundle.assert_not_called()


class TestStoredOutcome:
    def test_produce_bundle_stores_and_records_uri(self, tmp_path: Path) -> None:
        snapshot = SnapshotBundleConfig(
            enabled=True,
            store=LocalDiskBundleStoreConfig(root_dir=str(tmp_path)),
        )
        conductor = _make_conductor(snapshot=snapshot)

        # Make build_grade_bundle write a tiny real bundle so the size walk
        # returns a small non-zero value.
        def fake_build(trial_id: str, *, out_dir: Path):
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "manifest.json").write_bytes(b'{"schema_version":"1.0"}')
            return MagicMock()

        conductor.runtime_backend.build_grade_bundle.side_effect = fake_build
        trajectory = _make_trajectory()
        spec = _make_spec()

        conductor._produce_grade_bundle(spec, _make_setup(), trajectory)

        # remember_trial_inputs was called before build_grade_bundle
        assert conductor.runtime_backend.remember_trial_inputs.call_args_list[0].args == (
            "t1:0",
            trajectory,
            spec.task,
        )
        conductor.runtime_backend.build_grade_bundle.assert_called_once()
        assert trajectory.snapshot_status is not None
        assert trajectory.snapshot_status.outcome is SnapshotOutcome.STORED
        assert trajectory.snapshot_status.uri is not None
        assert trajectory.snapshot_status.uri.startswith("bundle://local_disk/")
        assert trajectory.snapshot_status.bundle_size_bytes > 0


class TestOversizeOutcome:
    def test_produce_bundle_records_oversize_and_skips_store_put(self, tmp_path: Path) -> None:
        snapshot = SnapshotBundleConfig(
            enabled=True,
            store=LocalDiskBundleStoreConfig(root_dir=str(tmp_path)),
            max_bundle_mb=0.00001,  # ~10 bytes cap
        )
        conductor = _make_conductor(snapshot=snapshot)

        def fake_build(trial_id: str, *, out_dir: Path):
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "manifest.json").write_bytes(b"X" * 1024)
            return MagicMock()

        conductor.runtime_backend.build_grade_bundle.side_effect = fake_build
        trajectory = _make_trajectory()

        conductor._produce_grade_bundle(_make_spec(), _make_setup(), trajectory)

        assert trajectory.snapshot_status is not None
        assert trajectory.snapshot_status.outcome is SnapshotOutcome.OVERSIZE
        assert trajectory.snapshot_status.bundle_size_bytes == 1024
        assert trajectory.snapshot_status.cap_bytes is not None
        assert trajectory.snapshot_status.reason is not None
        assert "MB" in trajectory.snapshot_status.reason
        # The store dir should be empty (nothing put)
        put_dir = tmp_path / "grade_bundles"
        assert not any(put_dir.iterdir())


class TestProduceFailedOutcome:
    def test_produce_bundle_records_produce_failed(self, tmp_path: Path) -> None:
        snapshot = SnapshotBundleConfig(
            enabled=True,
            store=LocalDiskBundleStoreConfig(root_dir=str(tmp_path)),
        )
        conductor = _make_conductor(snapshot=snapshot)
        conductor.runtime_backend.build_grade_bundle.side_effect = RuntimeError("boom")
        trajectory = _make_trajectory()

        # Must not re-raise — the seam contains produce failures.
        conductor._produce_grade_bundle(_make_spec(), _make_setup(), trajectory)

        assert trajectory.snapshot_status is not None
        assert trajectory.snapshot_status.outcome is SnapshotOutcome.PRODUCE_FAILED
        assert trajectory.snapshot_status.reason == "boom"


class TestUngradedOutcomeFactory:
    """The seam early-returns on ``trajectory.grade is None`` and does not
    write UNGRADED. Test the factory here so its shape stays discoverable."""

    def test_ungraded_factory_emits_enum(self) -> None:
        from tolokaforge.core.models import SnapshotStatus

        status = SnapshotStatus.ungraded()
        assert status.outcome is SnapshotOutcome.UNGRADED
        assert status.uri is None
        assert status.reason is None


class TestStoreFailedOutcome:
    """The seam contains every failure between ``build_store()`` and
    ``store.close()``: store construction failure, store.put failure, and
    the close-time exception the outer finally survives. Each records
    :attr:`SnapshotOutcome.PRODUCE_FAILED` on the trajectory and lets the
    trial's normal completion path continue.
    """

    def test_produce_bundle_records_produce_failed_on_store_put_failure(
        self, tmp_path: Path
    ) -> None:
        snapshot = SnapshotBundleConfig(
            enabled=True,
            store=LocalDiskBundleStoreConfig(root_dir=str(tmp_path)),
        )
        conductor = _make_conductor(snapshot=snapshot)

        def fake_build(trial_id: str, *, out_dir: Path):
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "manifest.json").write_bytes(b'{"schema_version":"1.0"}')
            return MagicMock()

        conductor.runtime_backend.build_grade_bundle.side_effect = fake_build

        fake_store = MagicMock()
        fake_store.put.side_effect = OSError("ENOSPC")
        with patch.object(SnapshotBundleConfig, "build_store", return_value=fake_store):
            trajectory = _make_trajectory()
            # Must not re-raise — the seam contains storage failures.
            conductor._produce_grade_bundle(_make_spec(), _make_setup(), trajectory)

        assert trajectory.snapshot_status is not None
        assert trajectory.snapshot_status.outcome is SnapshotOutcome.PRODUCE_FAILED
        assert "ENOSPC" in trajectory.snapshot_status.reason
        fake_store.close.assert_called_once()

    def test_produce_bundle_records_produce_failed_on_build_store_failure(
        self, tmp_path: Path
    ) -> None:
        snapshot = SnapshotBundleConfig(
            enabled=True,
            store=LocalDiskBundleStoreConfig(root_dir=str(tmp_path)),
        )
        conductor = _make_conductor(snapshot=snapshot)
        with patch.object(
            SnapshotBundleConfig,
            "build_store",
            side_effect=ImportError("boto3 missing"),
        ):
            trajectory = _make_trajectory()
            conductor._produce_grade_bundle(_make_spec(), _make_setup(), trajectory)

        assert trajectory.snapshot_status is not None
        assert trajectory.snapshot_status.outcome is SnapshotOutcome.PRODUCE_FAILED
        assert "store construction" in trajectory.snapshot_status.reason
        assert "boto3 missing" in trajectory.snapshot_status.reason
        # No build_grade_bundle call — failure was before the substrate stashed inputs.
        conductor.runtime_backend.build_grade_bundle.assert_not_called()

    def test_produce_bundle_survives_store_close_failure(self, tmp_path: Path) -> None:
        """A close-time exception must not mask the primary outcome."""
        snapshot = SnapshotBundleConfig(
            enabled=True,
            store=LocalDiskBundleStoreConfig(root_dir=str(tmp_path)),
        )
        conductor = _make_conductor(snapshot=snapshot)

        def fake_build(trial_id: str, *, out_dir: Path):
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "manifest.json").write_bytes(b'{"schema_version":"1.0"}')
            return MagicMock()

        conductor.runtime_backend.build_grade_bundle.side_effect = fake_build

        fake_store = MagicMock()
        fake_store.put.return_value = "bundle://local_disk/deadbeef"
        fake_store.close.side_effect = RuntimeError("close boom")
        with patch.object(SnapshotBundleConfig, "build_store", return_value=fake_store):
            trajectory = _make_trajectory()
            conductor._produce_grade_bundle(_make_spec(), _make_setup(), trajectory)

        # Primary outcome (stored) is preserved; close failure is swallowed.
        assert trajectory.snapshot_status is not None
        assert trajectory.snapshot_status.outcome is SnapshotOutcome.STORED
        fake_store.close.assert_called_once()
