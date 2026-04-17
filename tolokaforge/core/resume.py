"""Resume/retry support for interrupted runs"""

import json
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import BaseModel

from tolokaforge.core.logging import get_logger


class TrialState(BaseModel):
    """State of a single trial"""

    task_id: str
    trial_index: int
    status: str  # "pending", "running", "completed", "failed"
    start_ts: datetime | None = None
    end_ts: datetime | None = None
    binary_pass: bool | None = None
    score: float | None = None
    error: str | None = None


class RunState(BaseModel):
    """State of an entire run for resume"""

    run_id: str
    config_path: str
    output_dir: str
    start_ts: datetime
    last_updated: datetime
    status: str  # "running", "paused", "completed", "failed"

    total_trials: int
    completed_trials: int
    failed_trials: int

    trials: dict[str, TrialState]  # key: "{task_id}:{trial_index}"

    def get_pending_trials(self) -> list[TrialState]:
        """Get list of trials not yet completed"""
        return [trial for trial in self.trials.values() if trial.status in ("pending", "failed")]

    def get_completed_trials(self) -> list[TrialState]:
        """Get list of completed trials"""
        return [trial for trial in self.trials.values() if trial.status == "completed"]

    def mark_completed(self, task_id: str, trial_index: int, binary_pass: bool, score: float):
        """Mark trial as completed"""
        key = f"{task_id}:{trial_index}"
        if key in self.trials:
            self.trials[key].status = "completed"
            self.trials[key].end_ts = datetime.now(tz=timezone.utc)
            self.trials[key].binary_pass = binary_pass
            self.trials[key].score = score
            self.completed_trials += 1

    def mark_failed(self, task_id: str, trial_index: int, error: str):
        """Mark trial as failed"""
        key = f"{task_id}:{trial_index}"
        if key in self.trials:
            self.trials[key].status = "failed"
            self.trials[key].end_ts = datetime.now(tz=timezone.utc)
            self.trials[key].error = error
            self.failed_trials += 1

    def mark_running(self, task_id: str, trial_index: int):
        """Mark trial as currently running"""
        key = f"{task_id}:{trial_index}"
        if key in self.trials:
            self.trials[key].status = "running"
            self.trials[key].start_ts = datetime.now(tz=timezone.utc)


class RunStateManager:
    """Manages run state persistence for resume functionality"""

    def __init__(self, output_dir: Path):
        self.output_dir = Path(output_dir)
        self.state_file = self.output_dir / "run_state.json"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def initialize_run(
        self, run_id: str, config_path: str, task_ids: list[str], repeats: int
    ) -> RunState:
        """Initialize a new run state"""

        # Create trial list
        trials = {}
        for task_id in task_ids:
            for trial_idx in range(repeats):
                key = f"{task_id}:{trial_idx}"
                trials[key] = TrialState(task_id=task_id, trial_index=trial_idx, status="pending")

        run_state = RunState(
            run_id=run_id,
            config_path=config_path,
            output_dir=str(self.output_dir),
            start_ts=datetime.now(tz=timezone.utc),
            last_updated=datetime.now(tz=timezone.utc),
            status="running",
            total_trials=len(trials),
            completed_trials=0,
            failed_trials=0,
            trials=trials,
        )

        self.save_state(run_state)
        return run_state

    def load_state(self) -> RunState | None:
        """Load run state from disk"""
        if not self.state_file.exists():
            return None

        try:
            with open(self.state_file) as f:
                data = json.load(f)
                return RunState(**data)
        except Exception as e:
            logger = get_logger("resume")
            logger.warning("Failed to load run state", error=str(e))
            return None

    def save_state(self, run_state: RunState):
        """Save run state to disk"""
        run_state.last_updated = datetime.now(tz=timezone.utc)

        with open(self.state_file, "w") as f:
            json.dump(run_state.model_dump(mode="json"), f, indent=2, default=str)

    def _has_infrastructure_error(self, task_id: str, trial_index: int) -> bool:
        """Check if trial has infrastructure errors (429, status=error)"""
        trial_dir = self.output_dir / "trials" / task_id / str(trial_index)

        if not trial_dir.exists():
            return False

        # Check trajectory for 429 error or error status
        trajectory_path = trial_dir / "trajectory.yaml"
        if trajectory_path.exists():
            try:
                with open(trajectory_path) as f:
                    traj_data = yaml.safe_load(f)

                # Check status field
                if traj_data.get("status") == "error":
                    return True

                # Check for 429 in content
                with open(trajectory_path) as f:
                    content = f.read()
                    if "Error code: 429" in content or "RateLimitError" in content:
                        return True
            except Exception:
                pass

        return False

    def is_completed(self, task_id: str, trial_index: int) -> bool:
        """Check if trial is completed and should be skipped.

        Returns True if:
        - Trial passed successfully
        - Trial failed due to behavioral issues (not infrastructure)

        Returns False if:
        - Trial doesn't exist yet
        - Trial has infrastructure errors (needs retry)
        - Trial status is not completed
        """
        run_state = self.load_state()
        if not run_state:
            return False

        key = f"{task_id}:{trial_index}"
        if key not in run_state.trials:
            return False

        trial = run_state.trials[key]

        # Not completed yet - needs to run
        if trial.status != "completed":
            return False

        # Check if trial passed - skip successful trials
        if trial.binary_pass:
            return True

        # Trial failed - check if due to infrastructure or behavioral
        has_infra_error = self._has_infrastructure_error(task_id, trial_index)

        if has_infra_error:
            # Infrastructure failure - needs retry
            return False
        else:
            # Behavioral failure - skip (won't improve on retry)
            return True

    def get_resume_info(self) -> dict | None:
        """Get information about resumable run"""
        run_state = self.load_state()
        if not run_state:
            return None

        pending = run_state.get_pending_trials()
        completed = run_state.get_completed_trials()

        return {
            "run_id": run_state.run_id,
            "status": run_state.status,
            "total_trials": run_state.total_trials,
            "completed_trials": len(completed),
            "failed_trials": run_state.failed_trials,
            "pending_trials": len(pending),
            "progress_pct": (
                (len(completed) / run_state.total_trials * 100) if run_state.total_trials > 0 else 0
            ),
            "can_resume": len(pending) > 0,
        }

    def mark_run_completed(self):
        """Mark entire run as completed"""
        run_state = self.load_state()
        if run_state:
            run_state.status = "completed"
            self.save_state(run_state)

    def mark_run_paused(self):
        """Mark run as paused (e.g., after KeyboardInterrupt)"""
        run_state = self.load_state()
        if run_state:
            run_state.status = "paused"
            self.save_state(run_state)
