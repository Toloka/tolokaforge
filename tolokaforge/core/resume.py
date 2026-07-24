"""Resume/retry support for interrupted runs"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import BaseModel

from tolokaforge.core.engine_run_state import read_persisted_run_id
from tolokaforge.core.logging import get_logger


@dataclass(frozen=True)
class ResumePlan:
    """Summary of what a resumed invocation will replay.

    ``already_done`` matches :meth:`RunStateManager.is_completed`: trials
    that either passed or failed behaviourally (retry-exhausted) will be
    skipped. ``to_retry`` is everything else — pending, running, and
    infra-failed trials that should re-execute.
    """

    run_id: str
    total: int
    completed: int
    already_done: int
    to_retry: int
    is_complete: bool


def resolve_resume_run_directory(run_dir: Path) -> tuple[str, Path]:
    """Return ``(run_id, run_dir)`` for an existing resumable run directory.

    The canonical ``run_id`` is read from ``<run_dir>/engine_run_state.json``
    (written by ``prepare`` / the orchestrator on first run). When that file
    is absent — e.g. a legacy CLI run that predates engine state persistence —
    the ``run_id`` in ``<run_dir>/run_state.json`` is used. If neither file
    is present, falls through to ``run_dir.name`` only when at least one of
    them exists; a directory lacking both raises ``RuntimeError``.

    The returned ``run_dir`` is passed through unchanged (no ``.resolve()``),
    matching :func:`tolokaforge.core.orchestrator.resolve_run_directory`.
    """
    run_dir = Path(run_dir)
    engine_state_present = (run_dir / "engine_run_state.json").exists()
    run_state_present = (run_dir / "run_state.json").exists()

    if not engine_state_present and not run_state_present:
        raise RuntimeError(
            f"{run_dir} is not a resumable run directory: no engine_run_state.json "
            "or run_state.json present. Run `tolokaforge run` first (without --resume) "
            "to create one."
        )

    run_id = read_persisted_run_id(run_dir)
    if run_id:
        return run_id, run_dir

    if run_state_present:
        data = json.loads((run_dir / "run_state.json").read_text())
        persisted = data.get("run_id")
        if persisted:
            return persisted, run_dir

    return run_dir.name, run_dir


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

    @staticmethod
    def _normalize_to_relative(path_str: str) -> str:
        """Normalize path to be relative to CWD when possible.

        Ensures consistent paths in run_state.json regardless of whether the
        run was started via CLI (relative paths) or programmatic API (often
        absolute paths from Path.resolve()).

        Both the input and CWD are resolved to handle platform symlinks
        (e.g. macOS ``/var`` → ``/private/var``).
        """
        p = Path(path_str)
        if p.is_absolute():
            try:
                return str(p.resolve().relative_to(Path.cwd().resolve()))
            except ValueError:
                return path_str  # Path not under CWD, keep absolute
        return path_str

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
            config_path=self._normalize_to_relative(config_path),
            output_dir=self._normalize_to_relative(str(self.output_dir)),
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

    def describe_resume_plan(self) -> ResumePlan | None:
        """Summarise what a resumed invocation would replay.

        Uses :meth:`is_completed` semantics for the ``already_done`` count
        (completed + behavioural-failed trials are skipped; pending, running,
        and infra-failed trials are replayed). Returns ``None`` when
        ``run_state.json`` is absent, matching :meth:`get_resume_info`.
        """
        run_state = self.load_state()
        if run_state is None:
            return None

        completed = 0
        already_done = 0
        for trial in run_state.trials.values():
            if trial.status == "completed":
                completed += 1
            if self.is_completed(trial.task_id, trial.trial_index):
                already_done += 1

        to_retry = run_state.total_trials - already_done
        return ResumePlan(
            run_id=run_state.run_id,
            total=run_state.total_trials,
            completed=completed,
            already_done=already_done,
            to_retry=to_retry,
            is_complete=to_retry == 0,
        )

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
