"""Resume/retry support for interrupted runs"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import BaseModel

from tolokaforge.core.engine_run_state import read_persisted_run_id
from tolokaforge.core.logging import get_logger
from tolokaforge.core.output_writer import TRAJECTORY_FILENAME
from tolokaforge.core.trial_identity import format_trial_id, trial_output_subpath

#: Format marker for the ``(entry, task_id, trial_index)`` resume keying.
#: Written on every state file this build creates. A harnesses run whose
#: ``run_state.json`` predates the marker is refused on resume (see
#: :meth:`RunStateManager.load_state`); single-adapter state files load
#: unchanged whether or not they carry it.
IDENTITY_FORMAT = 1


class ResumeIdentityFormatError(RuntimeError):
    """Raised when a harnesses run resumes a state file that predates
    ``(entry, task_id, trial_index)`` resume keying."""


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

    entry: str = ""
    """The harness entry that owns this trial; empty for a single-adapter run.
    Defaulted so state files written before harness keying load unchanged."""
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

    trials: dict[str, TrialState]
    """Keyed by the trial's :func:`format_trial_id` label:
    ``"{entry}:{task_id}:{trial_index}"`` under a harness entry,
    ``"{task_id}:{trial_index}"`` for a single-adapter run."""

    identity_format: int = IDENTITY_FORMAT
    """Marker for the ``(entry, task_id, trial_index)`` keying. Present on every
    state file this build writes; its *absence* in a loaded harnesses run's file
    is what trips the resume guard. Defaulted so a single-adapter state file
    written before the marker still loads."""

    zero_coverage: bool = False
    """Set at completion: ``measured_trials == 0`` on a run with
    ``total_attempts > 0``. See ``docs/adr/0041-zero-coverage-exit-signal.md``.
    Defaulted so state files written by earlier code load unchanged."""

    zero_judge_graded: bool = False
    """Set at completion: every produced grade has ``judge_status == ERRORED``.
    See ``docs/adr/0041-zero-coverage-exit-signal.md``. Defaulted so state files
    written by earlier code load unchanged."""

    def get_pending_trials(self) -> list[TrialState]:
        """Get list of trials not yet completed"""
        return [trial for trial in self.trials.values() if trial.status in ("pending", "failed")]

    def get_completed_trials(self) -> list[TrialState]:
        """Get list of completed trials"""
        return [trial for trial in self.trials.values() if trial.status == "completed"]

    def mark_completed(
        self,
        task_id: str,
        trial_index: int,
        binary_pass: bool | None,
        score: float | None,
        entry: str = "",
    ):
        """Mark trial as completed.

        ``binary_pass`` / ``score`` are ``None`` for a trial that produced no
        grade: the attempt is over, but there is no verdict to record.
        """
        key = format_trial_id(entry, task_id, trial_index)
        if key in self.trials:
            self.trials[key].status = "completed"
            self.trials[key].end_ts = datetime.now(tz=timezone.utc)
            self.trials[key].binary_pass = binary_pass
            self.trials[key].score = score
            self.completed_trials += 1

    def mark_failed(self, task_id: str, trial_index: int, error: str, entry: str = ""):
        """Mark trial as failed"""
        key = format_trial_id(entry, task_id, trial_index)
        if key in self.trials:
            self.trials[key].status = "failed"
            self.trials[key].end_ts = datetime.now(tz=timezone.utc)
            self.trials[key].error = error
            self.failed_trials += 1

    def mark_running(self, task_id: str, trial_index: int, entry: str = ""):
        """Mark trial as currently running"""
        key = format_trial_id(entry, task_id, trial_index)
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
        self,
        run_id: str,
        config_path: str,
        units: list[tuple[str, str]],
        repeats: int,
    ) -> RunState:
        """Initialize a new run state.

        ``units`` are ``(entry, task_id)`` pairs — one per task the run
        dispatches, carrying its owning harness entry (empty for a
        single-adapter run). The same ``task_id`` under two entries yields two
        independent trial keys, so a matrix run does not collide.
        """

        # Create trial list
        trials = {}
        for entry, task_id in units:
            for trial_idx in range(repeats):
                key = format_trial_id(entry, task_id, trial_idx)
                trials[key] = TrialState(
                    entry=entry, task_id=task_id, trial_index=trial_idx, status="pending"
                )

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

    def load_state(self, *, require_identity_marker: bool = False) -> RunState | None:
        """Load run state from disk.

        ``require_identity_marker`` is set by a harnesses run: its trials are
        keyed ``(entry, task_id, trial_index)``, so a state file written before
        that keying (no ``identity_format`` key) cannot be resumed safely and is
        refused with :class:`ResumeIdentityFormatError`. A single-adapter run
        leaves it ``False`` and loads such a file unchanged.
        """
        if not self.state_file.exists():
            return None

        try:
            with open(self.state_file) as f:
                data = json.load(f)
        except Exception as e:
            logger = get_logger("resume")
            logger.warning("Failed to load run state", error=str(e))
            return None

        if require_identity_marker and "identity_format" not in data:
            raise ResumeIdentityFormatError(
                f"Cannot resume harnesses run at {self.output_dir}: its "
                "run_state.json predates harness-aware trial keying (no "
                "identity_format marker), so its trial keys cannot be matched to "
                "(entry, task_id, trial_index). Start a fresh run directory, or "
                "resume with the engine version that wrote this state."
            )

        try:
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

    def _has_infrastructure_error(self, task_id: str, trial_index: int, entry: str = "") -> bool:
        """Check if trial has infrastructure errors (429, status=error)"""
        trial_dir = self.output_dir / "trials" / trial_output_subpath(entry, task_id, trial_index)

        if not trial_dir.exists():
            return False

        # Check trajectory for 429 error or error status
        trajectory_path = trial_dir / TRAJECTORY_FILENAME
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

    def is_completed(
        self,
        task_id: str,
        trial_index: int,
        entry: str = "",
        *,
        run_state: RunState | None = None,
    ) -> bool:
        """Check if trial is completed and should be skipped.

        ``run_state`` is a state the caller already loaded; without it the state is
        read from disk. A caller checking many trials passes it, so the state file
        is parsed once rather than once per trial.

        Returns True if:
        - Trial passed successfully
        - Trial failed due to behavioral issues (not infrastructure)

        Returns False if:
        - Trial doesn't exist yet
        - Trial has infrastructure errors (needs retry)
        - Trial status is not completed
        """
        if run_state is None:
            run_state = self.load_state()
        if not run_state:
            return False

        key = format_trial_id(entry, task_id, trial_index)
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
        has_infra_error = self._has_infrastructure_error(task_id, trial_index, entry)

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
            if self.is_completed(
                trial.task_id, trial.trial_index, trial.entry, run_state=run_state
            ):
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

    def mark_run_completed(self, *, zero_coverage: bool, zero_judge_graded: bool) -> None:
        """Stamp ``status: "completed"`` and the two completion gates onto the state file.

        The two booleans are keyword-only so the completion gates cannot be
        swapped at the call site. See
        ``docs/adr/0041-zero-coverage-exit-signal.md``.
        """
        run_state = self.load_state()
        if run_state:
            run_state.status = "completed"
            run_state.zero_coverage = zero_coverage
            run_state.zero_judge_graded = zero_judge_graded
            self.save_state(run_state)

    def mark_run_paused(self):
        """Mark run as paused (e.g., after KeyboardInterrupt)"""
        run_state = self.load_state()
        if run_state:
            run_state.status = "paused"
            self.save_state(run_state)
