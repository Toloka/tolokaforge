"""Tests for resume/retry functionality"""

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tolokaforge.core.resume import RunState, RunStateManager, TrialState

pytestmark = pytest.mark.unit


@pytest.mark.unit
class TestRunState:
    """Test RunState model"""

    def test_run_state_creation(self):
        """Test creating a run state"""
        trials = {
            "task1:0": TrialState(task_id="task1", trial_index=0, status="pending"),
            "task1:1": TrialState(
                task_id="task1", trial_index=1, status="completed", binary_pass=True, score=0.9
            ),
        }

        run_state = RunState(
            run_id="test_run",
            config_path="test.yaml",
            output_dir="/tmp/output",
            start_ts=datetime.now(tz=timezone.utc),
            last_updated=datetime.now(tz=timezone.utc),
            status="running",
            total_trials=2,
            completed_trials=1,
            failed_trials=0,
            trials=trials,
        )

        assert run_state.run_id == "test_run"
        assert run_state.total_trials == 2
        assert run_state.completed_trials == 1

    def test_get_pending_trials(self):
        """Test getting pending trials"""
        trials = {
            "task1:0": TrialState(task_id="task1", trial_index=0, status="pending"),
            "task1:1": TrialState(task_id="task1", trial_index=1, status="completed"),
            "task2:0": TrialState(task_id="task2", trial_index=0, status="failed"),
        }

        run_state = RunState(
            run_id="test",
            config_path="test.yaml",
            output_dir="/tmp",
            start_ts=datetime.now(tz=timezone.utc),
            last_updated=datetime.now(tz=timezone.utc),
            status="running",
            total_trials=3,
            completed_trials=1,
            failed_trials=1,
            trials=trials,
        )

        pending = run_state.get_pending_trials()
        assert len(pending) == 2  # pending and failed
        assert any(t.status == "pending" for t in pending)
        assert any(t.status == "failed" for t in pending)

    def test_mark_completed(self):
        """Test marking trial as completed"""
        trials = {
            "task1:0": TrialState(task_id="task1", trial_index=0, status="running"),
        }

        run_state = RunState(
            run_id="test",
            config_path="test.yaml",
            output_dir="/tmp",
            start_ts=datetime.now(tz=timezone.utc),
            last_updated=datetime.now(tz=timezone.utc),
            status="running",
            total_trials=1,
            completed_trials=0,
            failed_trials=0,
            trials=trials,
        )

        run_state.mark_completed("task1", 0, True, 0.95)

        assert run_state.trials["task1:0"].status == "completed"
        assert run_state.trials["task1:0"].binary_pass is True
        assert run_state.trials["task1:0"].score == 0.95
        assert run_state.completed_trials == 1

    def test_mark_failed(self):
        """Test marking trial as failed"""
        trials = {
            "task1:0": TrialState(task_id="task1", trial_index=0, status="running"),
        }

        run_state = RunState(
            run_id="test",
            config_path="test.yaml",
            output_dir="/tmp",
            start_ts=datetime.now(tz=timezone.utc),
            last_updated=datetime.now(tz=timezone.utc),
            status="running",
            total_trials=1,
            completed_trials=0,
            failed_trials=0,
            trials=trials,
        )

        run_state.mark_failed("task1", 0, "Timeout error")

        assert run_state.trials["task1:0"].status == "failed"
        assert run_state.trials["task1:0"].error == "Timeout error"
        assert run_state.failed_trials == 1


@pytest.mark.unit
class TestRunStateManager:
    """Test RunStateManager"""

    def test_initialize_run(self):
        """Test initializing a new run"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = RunStateManager(Path(tmpdir))

            run_state = manager.initialize_run(
                run_id="test_run", config_path="test.yaml", task_ids=["task1", "task2"], repeats=2
            )

            assert run_state.run_id == "test_run"
            assert run_state.total_trials == 4  # 2 tasks * 2 repeats
            assert len(run_state.trials) == 4

            # Check state file was created
            state_file = Path(tmpdir) / "run_state.json"
            assert state_file.exists()

    def test_load_state(self):
        """Test loading run state from disk"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = RunStateManager(Path(tmpdir))

            # Initialize run
            manager.initialize_run(
                run_id="test_run", config_path="test.yaml", task_ids=["task1"], repeats=1
            )

            # Load state
            loaded_state = manager.load_state()
            assert loaded_state is not None
            assert loaded_state.run_id == "test_run"
            assert loaded_state.total_trials == 1

    def test_is_completed(self):
        """Test checking if trial is completed"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = RunStateManager(Path(tmpdir))

            run_state = manager.initialize_run(
                run_id="test_run", config_path="test.yaml", task_ids=["task1"], repeats=2
            )

            # Mark one trial as completed
            run_state.mark_completed("task1", 0, True, 0.9)
            manager.save_state(run_state)

            # Check completion status
            assert manager.is_completed("task1", 0) is True
            assert manager.is_completed("task1", 1) is False

    def test_get_resume_info(self):
        """Test getting resume information"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = RunStateManager(Path(tmpdir))

            run_state = manager.initialize_run(
                run_id="test_run", config_path="test.yaml", task_ids=["task1", "task2"], repeats=2
            )

            # Mark some trials as completed
            run_state.mark_completed("task1", 0, True, 0.9)
            run_state.mark_failed("task1", 1, "Error")
            manager.save_state(run_state)

            resume_info = manager.get_resume_info()
            assert resume_info is not None
            assert resume_info["run_id"] == "test_run"
            assert resume_info["total_trials"] == 4
            assert resume_info["completed_trials"] == 1
            assert resume_info["failed_trials"] == 1
            assert resume_info["pending_trials"] == 3  # 1 failed + 2 pending
            assert resume_info["can_resume"] is True

    def test_no_state_file(self):
        """Test loading when no state file exists"""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = RunStateManager(Path(tmpdir))
            loaded_state = manager.load_state()
            assert loaded_state is None

            resume_info = manager.get_resume_info()
            assert resume_info is None
