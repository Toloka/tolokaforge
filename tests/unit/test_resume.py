"""Tests for resume/retry functionality"""

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tolokaforge.core.resume import ResumePlan, RunState, RunStateManager, TrialState

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

    def test_initialize_run_normalizes_absolute_config_path(self):
        """Absolute config_path under CWD is stored as a CWD-relative path.

        Regression for the inconsistency between the CLI (which gets a
        relative path from argparse) and the programmatic API (which often
        supplies an absolute path from Path(...).resolve()).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            config_file = tmp_root / "configs" / "run.yaml"
            config_file.parent.mkdir()
            config_file.write_text("# placeholder\n")

            output_dir = tmp_root / "results" / "run_0"

            cwd = Path.cwd()
            try:
                import os

                os.chdir(tmp_root)
                manager = RunStateManager(output_dir)
                run_state = manager.initialize_run(
                    run_id="r0",
                    config_path=str(config_file.resolve()),
                    task_ids=["t1"],
                    repeats=1,
                )
            finally:
                os.chdir(cwd)

            # Both paths should now be relative.
            assert not Path(run_state.config_path).is_absolute()
            assert run_state.config_path == "configs/run.yaml"
            assert not Path(run_state.output_dir).is_absolute()
            assert run_state.output_dir == "results/run_0"

    def test_initialize_run_keeps_path_outside_cwd_absolute(self):
        """Path that is not under CWD stays absolute, no exception raised."""
        with tempfile.TemporaryDirectory() as tmpdir:
            outside = Path(tmpdir) / "elsewhere" / "config.yaml"
            outside.parent.mkdir()
            outside.write_text("# placeholder\n")

            with tempfile.TemporaryDirectory() as cwd_tmpdir:
                cwd = Path.cwd()
                try:
                    import os

                    os.chdir(cwd_tmpdir)
                    manager = RunStateManager(Path(cwd_tmpdir) / "out")
                    run_state = manager.initialize_run(
                        run_id="r1",
                        config_path=str(outside),
                        task_ids=["t1"],
                        repeats=1,
                    )
                finally:
                    os.chdir(cwd)

            assert run_state.config_path == str(outside)

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


@pytest.mark.unit
class TestDescribeResumePlan:
    """Behaviour of :meth:`RunStateManager.describe_resume_plan`."""

    def test_returns_none_when_state_file_missing(self, tmp_path: Path) -> None:
        manager = RunStateManager(tmp_path)
        assert manager.describe_resume_plan() is None

    def test_all_pending_reports_zero_done(self, tmp_path: Path) -> None:
        manager = RunStateManager(tmp_path)
        manager.initialize_run(run_id="fresh", config_path="c.yaml", task_ids=["a", "b"], repeats=1)

        plan = manager.describe_resume_plan()

        assert plan == ResumePlan(
            run_id="fresh",
            total=2,
            completed=0,
            already_done=0,
            to_retry=2,
            is_complete=False,
        )

    def test_all_passed_is_complete(self, tmp_path: Path) -> None:
        manager = RunStateManager(tmp_path)
        run_state = manager.initialize_run(
            run_id="done", config_path="c.yaml", task_ids=["a", "b"], repeats=1
        )
        run_state.mark_completed("a", 0, binary_pass=True, score=1.0)
        run_state.mark_completed("b", 0, binary_pass=True, score=1.0)
        manager.save_state(run_state)

        plan = manager.describe_resume_plan()

        assert plan is not None
        assert plan.is_complete is True
        assert plan.total == 2
        assert plan.completed == 2
        assert plan.already_done == 2
        assert plan.to_retry == 0

    def test_behavioural_failure_counts_as_already_done(self, tmp_path: Path) -> None:
        manager = RunStateManager(tmp_path)
        run_state = manager.initialize_run(
            run_id="mixed", config_path="c.yaml", task_ids=["a"], repeats=2
        )
        run_state.mark_completed("a", 0, binary_pass=True, score=1.0)
        # Second trial completed but did not pass, and has no infra-error
        # signature on disk — the retry-exhausted / behavioural-failure case.
        run_state.mark_completed("a", 1, binary_pass=False, score=0.0)
        manager.save_state(run_state)

        plan = manager.describe_resume_plan()

        assert plan is not None
        assert plan.completed == 2
        assert plan.already_done == 2
        assert plan.to_retry == 0
        assert plan.is_complete is True

    def test_infra_failure_counted_in_to_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = RunStateManager(tmp_path)
        run_state = manager.initialize_run(
            run_id="infra", config_path="c.yaml", task_ids=["a"], repeats=2
        )
        run_state.mark_completed("a", 0, binary_pass=True, score=1.0)
        run_state.mark_completed("a", 1, binary_pass=False, score=0.0)
        manager.save_state(run_state)

        def _fake_infra(_self, task_id: str, trial_index: int) -> bool:
            return task_id == "a" and trial_index == 1

        monkeypatch.setattr(RunStateManager, "_has_infrastructure_error", _fake_infra)

        plan = manager.describe_resume_plan()

        assert plan is not None
        assert plan.completed == 2
        assert plan.already_done == 1
        assert plan.to_retry == 1
        assert plan.is_complete is False

    def test_partial_mixed_state(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """One passed + one behavioural-failed + one infra-failed + one pending."""
        manager = RunStateManager(tmp_path)
        run_state = manager.initialize_run(
            run_id="mixed4", config_path="c.yaml", task_ids=["a", "b", "c", "d"], repeats=1
        )
        run_state.mark_completed("a", 0, binary_pass=True, score=1.0)
        run_state.mark_completed("b", 0, binary_pass=False, score=0.0)  # behavioural
        run_state.mark_completed("c", 0, binary_pass=False, score=0.0)  # infra (below)
        # d:0 stays pending
        manager.save_state(run_state)

        def _fake_infra(_self, task_id: str, trial_index: int) -> bool:
            return task_id == "c"

        monkeypatch.setattr(RunStateManager, "_has_infrastructure_error", _fake_infra)

        plan = manager.describe_resume_plan()

        assert plan is not None
        assert plan.run_id == "mixed4"
        assert plan.total == 4
        assert plan.completed == 3  # a, b, c reached "completed" status
        assert plan.already_done == 2  # a (pass) + b (behavioural)
        assert plan.to_retry == 2  # c (infra) + d (pending)
        assert plan.is_complete is False
