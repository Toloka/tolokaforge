"""Canonical: one task run as an independent trial per harness entry.

A two-entry ``harnesses:`` run of the SAME ``task_id`` must stay fully separated
end to end. This drives a real ``Orchestrator.run()`` over a mock runtime
(:class:`~tolokaforge.core.runtime.InMemoryRuntimeBackend`, no Docker, no gRPC
runner) and a grading-phase conductor double, then asserts the whole matrix
chain the ``(entry, task_id, trial_index)`` keying produces:

- two distinct per-trial bundle directories
  (``trials/<entry>/<task>/<idx>/``), neither colliding,
- two trajectories, each tagged with its own ``harness_entry``,
- two durable-queue attempts for the one task id — one per entry,
- two independent ``RunState.trials`` entries keyed by ``(entry, task, idx)``,
- a resume that skips ONLY the completed entry's trial and re-runs the other.

A single-adapter run is driven through the same machinery to prove the matrix
path has not displaced the bare ``trials/<task>/<idx>/`` layout or the
``"{task}:{idx}"`` identity.

The real ``InProcessConductor.run()`` dies without a live environment (it needs
``EnvironmentState.hydrate()``), so the conductor double here builds the trial's
trajectory itself and runs the production ``_grade`` / ``_write_artifacts``
phases — the two that persist the entry-keyed bundle — against an entry-aware
``_TrialSetup``. Everything else (queue, run-state, dispatch, output paths) is
production code.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from tests.canonical._factories import make_task_config, make_task_description
from tests.utils.conductor_phases import runner_stub
from tolokaforge.adapters import register_adapter
from tolokaforge.adapters.base import AdapterEnvironment, BaseAdapter
from tolokaforge.core.conductor import ConductorContext, InProcessConductor, _TrialSetup
from tolokaforge.core.llm import LLMClient
from tolokaforge.core.models import (
    EvaluationConfig,
    Grade,
    GradeComponents,
    Metrics,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    TaskConfig,
    TerminationReason,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
from tolokaforge.core.resume import RunStateManager
from tolokaforge.core.runtime import InMemoryRuntimeBackend
from tolokaforge.core.trial import TrialResult, TrialSpec
from tolokaforge.core.trial_identity import trial_output_subpath

pytestmark = pytest.mark.canonical

_TASK_ID = "shared-task"


# ---------------------------------------------------------------------------
# Fake adapters — one per entry, both serving the SAME task id
# ---------------------------------------------------------------------------


class _MatrixFake(BaseAdapter):
    """Serves a single fixed task; identity encoded per subclass.

    Only the surfaces the grading pre-flight, dispatch, and grading-phase
    conductor touch are implemented — the setup / agent-loop phases are skipped
    by the conductor double, so those abstract methods raise if ever reached.
    """

    _adapter_type = "matrix_fake"

    def get_task_ids(self) -> list[str]:
        return [_TASK_ID]

    def get_task(self, task_id: str) -> TaskConfig:
        return make_task_config(task_id=task_id)

    def get_task_dir(self, task_id: str) -> Path:
        return Path("/matrix") / self._adapter_type / task_id

    def to_task_description(self, task_id: str) -> Any:
        return make_task_description(task_id=task_id, adapter_type=self._adapter_type)

    def get_grading_config(self, task_id: str) -> Any:
        return None

    def create_environment(self, task_id: str) -> AdapterEnvironment:  # pragma: no cover
        raise NotImplementedError

    def get_tools(self, task_id: str) -> list[Any]:  # pragma: no cover
        raise NotImplementedError

    def get_registry_tools(self, task_id, env) -> list[Any]:  # pragma: no cover
        raise NotImplementedError

    def get_system_prompt(self, task_id: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def reset_environment(self, env) -> None:  # pragma: no cover
        raise NotImplementedError

    def compute_golden_hash(self, task_id, env) -> str | None:  # pragma: no cover
        raise NotImplementedError


class _EntryAFake(_MatrixFake):
    _adapter_type = "matrix_fake_a"


class _EntryBFake(_MatrixFake):
    _adapter_type = "matrix_fake_b"


class _SingleFake(_MatrixFake):
    _adapter_type = "matrix_fake_single"


@pytest.fixture(autouse=True)
def _register_fakes() -> None:
    register_adapter("matrix_fake_a", _EntryAFake)
    register_adapter("matrix_fake_b", _EntryBFake)
    register_adapter("matrix_fake_single", _SingleFake)


# ---------------------------------------------------------------------------
# Grading-phase conductor double
# ---------------------------------------------------------------------------


@dataclass
class _Executed:
    """What the conductor actually ran.

    ``specs`` is the dispatch order as ``(entry, task_id, trial_index)``;
    ``agent_models`` records each trial's ``spec.agent_model_config`` keyed by
    entry, so a test can assert the per-entry agent model that threaded onto
    the spec through the real ``Orchestrator.run()`` dispatch.
    """

    specs: list[tuple[str, str, int]] = field(default_factory=list)
    agent_models: dict[str, ModelConfig] = field(default_factory=dict)


class _RecordingAgentClientFactory:
    """Agent-client factory that records each requested ``ModelConfig``.

    Returns a real :class:`LLMClient` per build so the orchestrator's run-level
    and per-entry clients are genuine clients whose ``config`` is assertable,
    while ``requested`` captures exactly which agent models the run built a
    client for (the run-level model plus one per differing entry).
    """

    def __init__(self) -> None:
        self.requested: list[ModelConfig] = []

    def __call__(self, config: ModelConfig) -> LLMClient:
        self.requested.append(config)
        return LLMClient(config)


class _PassingGrader:
    """Returns a passing :class:`Grade` for every trial."""

    def grade(self, spec: TrialSpec, trajectory: Trajectory, agent_system_prompt: str) -> Grade:
        return Grade(
            binary_pass=True,
            score=1.0,
            components=GradeComponents(state_checks=1.0),
            reasons="matrix trial graded",
        )


class _EntryGradingConductor:
    """A :class:`~tolokaforge.core.conductor.Conductor` that drives the
    production grading + artifact-write phases over a trajectory it builds.

    The trajectory is stamped with the spec's entry exactly as production does
    (``trajectory.harness_entry = spec.entry or None``), and the bundle is
    written under the entry-aware ``trials/<entry>/<task>/<idx>/`` subpath the
    production ``_setup_trial`` would have created — so the output location, the
    trajectory tag, and the grade all carry the matrix identity without needing
    a live environment.
    """

    def __init__(self, ctx: ConductorContext, grader: Any, recorder: _Executed) -> None:
        self._conductor = InProcessConductor(**{**vars(ctx), "trial_grader": grader})
        self._output_dir = ctx.output_dir
        self._recorder = recorder

    def run(self, spec: TrialSpec, task_config: TaskConfig) -> TrialResult:
        self._recorder.specs.append((spec.entry, spec.task_id, spec.trial_index))
        self._recorder.agent_models[spec.entry] = spec.agent_model_config
        now = datetime.now(UTC)
        trajectory = Trajectory(
            task_id=task_config.task_id,
            trial_index=spec.trial_index,
            start_ts=now,
            end_ts=now,
            status=TrialStatus.COMPLETED,
            termination_reason=TerminationReason.AGENT_DONE,
            messages=[],
            metrics=Metrics(),
            harness_entry=spec.entry or None,
            adapter_type=spec.task.adapter_type,
        )
        setup = self._setup(spec, task_config)
        runner = runner_stub()
        self._conductor._grade(spec, task_config, setup, trajectory, runner, "sys")
        self._conductor._write_artifacts(spec, task_config, setup, trajectory, runner)
        return TrialResult.from_trajectory(
            trial_id=spec.trial_id, trajectory=trajectory, worker_id=spec.worker_id
        )

    def _setup(self, spec: TrialSpec, task_config: TaskConfig) -> _TrialSetup:
        trial_dir = (
            self._output_dir
            / "trials"
            / trial_output_subpath(spec.entry, task_config.task_id, spec.trial_index)
        )
        return _TrialSetup(
            trial_id=spec.trial_id,
            trial_idx=spec.trial_index,
            task_dir=self._output_dir,
            trial_dir=trial_dir,
            env_state=MagicMock(),
            adapter_env=MagicMock(),
            tool_schemas=[],
            tool_executor=MagicMock(),
            user_tool_schemas=[],
            user_tool_executor=None,
        )


# ---------------------------------------------------------------------------
# Config + orchestrator builders
# ---------------------------------------------------------------------------


def _models() -> dict[str, ModelConfig]:
    return {
        "agent": ModelConfig(provider="openai", name="gpt-4"),
        "user": ModelConfig(provider="openai", name="gpt-4"),
    }


def _orchestrator_config() -> OrchestratorConfig:
    # No retries, no shuffle, one worker: a run whose scheduling is a single
    # deterministic pass, so the recorded execution order is the dispatch order.
    return OrchestratorConfig(
        workers=1,
        repeats=1,
        auto_start_services=False,
        shuffle_trials=False,
        max_attempt_retries=0,
    )


def _matrix_config(output_dir: Path) -> RunConfig:
    return RunConfig(
        models=_models(),
        orchestrator=_orchestrator_config(),
        evaluation=EvaluationConfig(output_dir=str(output_dir)),
        harnesses={
            "entries": [
                {"name": "entry_a", "adapter": "matrix_fake_a"},
                {"name": "entry_b", "adapter": "matrix_fake_b"},
            ]
        },
    )


def _single_adapter_config(output_dir: Path) -> RunConfig:
    return RunConfig(
        models=_models(),
        orchestrator=_orchestrator_config(),
        evaluation=EvaluationConfig(
            output_dir=str(output_dir), harness_adapter={"type": "matrix_fake_single"}
        ),
    )


# Run-level agent (``_models()``) is ``openai/gpt-4``; the two engine-loop
# entries below override ``model.agent`` to a different model each.
_AGENT_BASELINE = ModelConfig(provider="openai", name="gpt-5")
_AGENT_CHALLENGER = ModelConfig(provider="anthropic", name="claude-3-7")


def _two_model_engine_loop_config(output_dir: Path) -> RunConfig:
    """Two engine-loop entries over the one task, each a different ``model.agent``."""
    return RunConfig(
        models=_models(),
        orchestrator=_orchestrator_config(),
        evaluation=EvaluationConfig(output_dir=str(output_dir)),
        harnesses={
            "entries": [
                {
                    "name": "baseline",
                    "adapter": "matrix_fake_a",
                    "mode": "engine_loop",
                    "model": {"agent": _AGENT_BASELINE},
                },
                {
                    "name": "challenger",
                    "adapter": "matrix_fake_b",
                    "mode": "engine_loop",
                    "model": {"agent": _AGENT_CHALLENGER},
                },
            ]
        },
    )


def _run(
    config: RunConfig,
    output_dir: Path,
    *,
    resume: bool = False,
    agent_client_factory: Any | None = None,
) -> tuple[Path, _Executed]:
    recorder = _Executed()
    deps_kwargs: dict[str, Any] = {
        "runtime_backend": InMemoryRuntimeBackend(),
        "conductor_factory": lambda ctx: _EntryGradingConductor(ctx, _PassingGrader(), recorder),
    }
    if agent_client_factory is not None:
        deps_kwargs["agent_client_factory"] = agent_client_factory
    orch = Orchestrator(config, resume=resume, deps=OrchestratorDeps(**deps_kwargs))
    orch.load_tasks()
    run_dir = orch.run(run_id=output_dir.name, output_dir=output_dir)
    return run_dir, recorder


def _trajectory_at(run_dir: Path, entry: str) -> dict[str, Any]:
    path = run_dir / "trials" / trial_output_subpath(entry, _TASK_ID, 0) / "trajectory.yaml"
    return yaml.safe_load(path.read_text())


def _attempt_rows(run_dir: Path) -> list[tuple[str, str, int, str]]:
    conn = sqlite3.connect(run_dir / "run_queue.sqlite")
    try:
        rows = conn.execute(
            "SELECT entry, task_id, trial_index, status FROM attempts ORDER BY entry"
        ).fetchall()
    finally:
        conn.close()
    return [(str(e), str(t), int(i), str(s)) for e, t, i, s in rows]


# ---------------------------------------------------------------------------
# The matrix chain
# ---------------------------------------------------------------------------


class TestSameTaskRunsOncePerEntry:
    """One task under two entries is two independent trials, end to end."""

    def test_two_distinct_output_dirs(self, tmp_path: Path) -> None:
        run_dir, _ = _run(
            _matrix_config(tmp_path / "results" / "matrix_dirs"),
            tmp_path / "results" / "matrix_dirs",
        )

        dir_a = run_dir / "trials" / "entry_a" / _TASK_ID / "0"
        dir_b = run_dir / "trials" / "entry_b" / _TASK_ID / "0"
        assert dir_a != dir_b
        assert (dir_a / "trajectory.yaml").exists()
        assert (dir_b / "trajectory.yaml").exists()
        # The one task id lives under each entry — never at the single-adapter
        # two-level path that would mean the entries collapsed into one.
        assert not (run_dir / "trials" / _TASK_ID / "0").exists()

    def test_two_trajectories_each_tagged_with_its_entry(self, tmp_path: Path) -> None:
        run_dir, _ = _run(
            _matrix_config(tmp_path / "results" / "matrix_traj"),
            tmp_path / "results" / "matrix_traj",
        )

        traj_a = _trajectory_at(run_dir, "entry_a")
        traj_b = _trajectory_at(run_dir, "entry_b")
        assert traj_a["harness_entry"] == "entry_a"
        assert traj_b["harness_entry"] == "entry_b"
        # Same task, routed to each entry's own adapter — so the recorded
        # adapter type differs too, proving the two legs did not share a leg.
        assert traj_a["task_id"] == traj_b["task_id"] == _TASK_ID
        assert traj_a["adapter_type"] == "matrix_fake_a"
        assert traj_b["adapter_type"] == "matrix_fake_b"

    def test_two_queue_attempts_for_one_task(self, tmp_path: Path) -> None:
        run_dir, _ = _run(
            _matrix_config(tmp_path / "results" / "matrix_queue"),
            tmp_path / "results" / "matrix_queue",
        )

        rows = _attempt_rows(run_dir)
        assert rows == [
            ("entry_a", _TASK_ID, 0, "completed"),
            ("entry_b", _TASK_ID, 0, "completed"),
        ]

    def test_two_independent_run_state_entries(self, tmp_path: Path) -> None:
        run_dir, _ = _run(
            _matrix_config(tmp_path / "results" / "matrix_state"),
            tmp_path / "results" / "matrix_state",
        )

        state = json.loads((run_dir / "run_state.json").read_text())
        assert set(state["trials"]) == {f"entry_a:{_TASK_ID}:0", f"entry_b:{_TASK_ID}:0"}
        trial_a = state["trials"][f"entry_a:{_TASK_ID}:0"]
        trial_b = state["trials"][f"entry_b:{_TASK_ID}:0"]
        assert trial_a["entry"] == "entry_a"
        assert trial_b["entry"] == "entry_b"
        assert trial_a["status"] == trial_b["status"] == "completed"
        assert trial_a["binary_pass"] is True
        assert trial_b["binary_pass"] is True

    def test_both_entries_executed(self, tmp_path: Path) -> None:
        _, recorder = _run(
            _matrix_config(tmp_path / "results" / "matrix_exec"),
            tmp_path / "results" / "matrix_exec",
        )

        assert recorder.specs == [("entry_a", _TASK_ID, 0), ("entry_b", _TASK_ID, 0)]


class TestResumeIsSelectivePerEntry:
    """A resume replays only the entry whose trial did not complete."""

    def test_resume_reruns_only_the_incomplete_entry(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "results" / "matrix_resume"

        # Seed a prior run's state: entry_a completed and passed, entry_b never
        # finished. Written with the same (entry, task_id, trial_index) keying a
        # real run produces, through the production RunStateManager.
        manager = RunStateManager(output_dir)
        state = manager.initialize_run(
            run_id=output_dir.name,
            config_path="",
            units=[("entry_a", _TASK_ID), ("entry_b", _TASK_ID)],
            repeats=1,
        )
        state.mark_completed(_TASK_ID, 0, binary_pass=True, score=1.0, entry="entry_a")
        manager.save_state(state)

        # is_completed reads the seeded state selectively by entry.
        assert manager.is_completed(_TASK_ID, 0, entry="entry_a") is True
        assert manager.is_completed(_TASK_ID, 0, entry="entry_b") is False

        run_dir, recorder = _run(_matrix_config(output_dir), output_dir, resume=True)

        # Only entry_b's trial re-ran; entry_a's completed trial was skipped.
        assert recorder.specs == [("entry_b", _TASK_ID, 0)]
        assert (run_dir / "trials" / "entry_b" / _TASK_ID / "0" / "trajectory.yaml").exists()
        assert not (run_dir / "trials" / "entry_a").exists()

        # The queue this resume built holds only the incomplete entry's attempt.
        assert _attempt_rows(run_dir) == [("entry_b", _TASK_ID, 0, "completed")]

        # Final state: entry_a stays completed (untouched), entry_b now completed.
        final = json.loads((run_dir / "run_state.json").read_text())
        assert final["trials"][f"entry_a:{_TASK_ID}:0"]["status"] == "completed"
        assert final["trials"][f"entry_b:{_TASK_ID}:0"]["status"] == "completed"


class TestSingleAdapterLayoutUnchanged:
    """The matrix path must not displace the single-adapter identity."""

    def test_single_adapter_keeps_two_level_layout_and_bare_id(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "results" / "single_run"
        run_dir, recorder = _run(_single_adapter_config(output_dir), output_dir)

        # Two-level bundle path, no entry segment.
        trial_dir = run_dir / "trials" / _TASK_ID / "0"
        assert (trial_dir / "trajectory.yaml").exists()
        assert not (run_dir / "trials" / "matrix_fake_single").exists()

        # Bare "{task}:{idx}" identity in the run-state key, empty entry.
        state = json.loads((run_dir / "run_state.json").read_text())
        assert set(state["trials"]) == {f"{_TASK_ID}:0"}
        assert state["trials"][f"{_TASK_ID}:0"]["entry"] == ""

        # The trajectory carries no harness entry.
        traj = yaml.safe_load((trial_dir / "trajectory.yaml").read_text())
        assert traj["harness_entry"] is None

        # The run dispatched the one task under the empty-entry sentinel.
        assert recorder.specs == [("", _TASK_ID, 0)]

        # One queue attempt, entry-less.
        assert _attempt_rows(run_dir) == [("", _TASK_ID, 0, "completed")]


class TestPerEntryAgentModelThreadsEndToEnd:
    """Two engine-loop entries over one task each run their own declared model.

    Drives the real ``Orchestrator.run()`` + composite build (the path the
    removed engine-loop + per-entry-``model.agent`` refusal used to block)
    over the mock runtime, with no provider keys. Proves the per-entry agent
    model threads all the way onto the trial spec and that a client was built
    carrying each entry's model.
    """

    def test_each_engine_loop_entry_runs_its_own_declared_model(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "results" / "per_entry_models"
        factory = _RecordingAgentClientFactory()
        _, recorder = _run(
            _two_model_engine_loop_config(output_dir),
            output_dir,
            agent_client_factory=factory,
        )

        # Both engine-loop legs ran, one per entry, for the one shared task.
        assert recorder.specs == [
            ("baseline", _TASK_ID, 0),
            ("challenger", _TASK_ID, 0),
        ]

        # Each trial spec carries its entry's declared agent model, and the two
        # differ by provider + name — the per-entry model threaded end to end.
        assert recorder.agent_models["baseline"] == _AGENT_BASELINE
        assert recorder.agent_models["challenger"] == _AGENT_CHALLENGER
        baseline = recorder.agent_models["baseline"]
        challenger = recorder.agent_models["challenger"]
        assert (baseline.provider, baseline.name) != (challenger.provider, challenger.name)

        # The run built a client carrying each entry's model (plus the run-level
        # one), through the agent-client factory seam.
        assert _AGENT_BASELINE in factory.requested
        assert _AGENT_CHALLENGER in factory.requested
        assert _models()["agent"] in factory.requested

    def test_single_adapter_run_reuses_the_run_level_agent_model(self, tmp_path: Path) -> None:
        # Back-compat: a run with no per-entry override builds exactly one agent
        # client (the run-level one) and threads the run-level model onto the
        # trial spec — no per-entry client, nothing shadowing the run-level one.
        output_dir = tmp_path / "results" / "single_reuse"
        factory = _RecordingAgentClientFactory()
        _, recorder = _run(
            _single_adapter_config(output_dir),
            output_dir,
            agent_client_factory=factory,
        )

        assert recorder.agent_models[""] == _models()["agent"]
        assert factory.requested == [_models()["agent"]]
