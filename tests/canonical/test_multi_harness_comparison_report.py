"""Canonical: the per-harness comparison surfaces in the run report.

A mixed run — the engine loop (``native`` bucket) against one delegated harness
over the same task — must land its comparison in ``metadata_slices.json`` and log
the labelled table beside the aggregate-results line. A single-adapter run through
the same machinery must populate only the single ``native`` bucket and render no
table (the degrade-gracefully contract).

This drives a real ``Orchestrator.run()`` over a mock runtime
(:class:`~tolokaforge.core.runtime.InMemoryRuntimeBackend`, no Docker, no gRPC
runner) and a grading-phase conductor double that builds each trial's trajectory
and runs the production grading + artifact-write phases — the same shape the
matrix-identity canonical test uses. The aggregate artifacts are captured through
an :class:`~tolokaforge.core.output.aggregates.InMemoryAggregateWriter`, and the
run's logger carries the rendered table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.canonical._factories import make_task_config, make_task_description
from tests.utils.conductor_phases import runner_stub
from tolokaforge.adapters import register_adapter
from tolokaforge.adapters.base import AdapterEnvironment, BaseAdapter
from tolokaforge.core.conductor import ConductorContext, InProcessConductor, _TrialSetup
from tolokaforge.core.execution_mode import ExecutionMode
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
from tolokaforge.core.output.aggregates import InMemoryAggregateWriter
from tolokaforge.core.output.harness_comparison import (
    HARNESS_COMPARISON_FOOTNOTE,
    NATIVE_BUCKET,
    format_harness_comparison_table,
)
from tolokaforge.core.runtime import InMemoryRuntimeBackend
from tolokaforge.core.trial import TrialResult, TrialSpec
from tolokaforge.core.trial_identity import trial_output_subpath

pytestmark = pytest.mark.canonical

_TASK_ID = "shared-task"
_FAMILY = "db"

# The engine-loop entry is named "native" so its harness bucket is the native
# bucket; the delegated entry is a named harness. Mode is fixed by entry name.
_ENGINE_LOOP_ENTRIES = frozenset({"", "native"})


# ---------------------------------------------------------------------------
# Fake adapters — one per entry, all serving the SAME task id + family
# ---------------------------------------------------------------------------


class _ComparisonFake(BaseAdapter):
    """Serves a single fixed task in a fixed family; identity per subclass.

    Only the surfaces the grading pre-flight, dispatch, and grading-phase
    conductor touch are implemented — the setup / agent-loop phases are skipped
    by the conductor double, so those abstract methods raise if ever reached.
    """

    _adapter_type = "comparison_fake"

    def get_task_ids(self) -> list[str]:
        return [_TASK_ID]

    def get_task(self, task_id: str) -> TaskConfig:
        return make_task_config(task_id=task_id, category=_FAMILY)

    def get_task_dir(self, task_id: str) -> Path:
        return Path("/comparison") / self._adapter_type / task_id

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


class _NativeFake(_ComparisonFake):
    _adapter_type = "comparison_native"


class _HarnessFake(_ComparisonFake):
    _adapter_type = "comparison_harness"


class _SingleFake(_ComparisonFake):
    _adapter_type = "comparison_single"


@pytest.fixture(autouse=True)
def _register_fakes() -> None:
    register_adapter("comparison_native", _NativeFake)
    register_adapter("comparison_harness", _HarnessFake)
    register_adapter("comparison_single", _SingleFake)


# ---------------------------------------------------------------------------
# Grading-phase conductor double — tags each trajectory with its execution mode
# ---------------------------------------------------------------------------


@dataclass
class _Executed:
    specs: list[tuple[str, str, int]] = field(default_factory=list)


class _PassingGrader:
    def grade(self, spec: TrialSpec, trajectory: Trajectory, agent_system_prompt: str) -> Grade:
        return Grade(
            binary_pass=True,
            score=1.0,
            components=GradeComponents(state_checks=1.0),
            reasons="comparison trial graded",
        )


class _ComparisonConductor:
    """Runs the production grading + artifact-write phases over a trajectory it
    builds, stamping it with the spec's entry and the entry's execution mode."""

    def __init__(self, ctx: ConductorContext, grader: Any, recorder: _Executed) -> None:
        self._conductor = InProcessConductor(**{**vars(ctx), "trial_grader": grader})
        self._output_dir = ctx.output_dir
        self._recorder = recorder

    def run(self, spec: TrialSpec, task_config: TaskConfig) -> TrialResult:
        self._recorder.specs.append((spec.entry, spec.task_id, spec.trial_index))
        now = datetime.now(UTC)
        mode = (
            ExecutionMode.ENGINE_LOOP
            if spec.entry in _ENGINE_LOOP_ENTRIES
            else ExecutionMode.DELEGATED
        )
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
            execution_mode=mode,
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
    return OrchestratorConfig(
        workers=1,
        repeats=1,
        auto_start_services=False,
        shuffle_trials=False,
        max_attempt_retries=0,
    )


def _mixed_config(output_dir: Path) -> RunConfig:
    """The engine loop ("native") versus one delegated harness over one task."""
    return RunConfig(
        models=_models(),
        orchestrator=_orchestrator_config(),
        evaluation=EvaluationConfig(output_dir=str(output_dir)),
        harnesses={
            "entries": [
                {"name": "native", "adapter": "comparison_native"},
                {"name": "terminal_bench", "adapter": "comparison_harness"},
            ]
        },
    )


def _single_adapter_config(output_dir: Path) -> RunConfig:
    return RunConfig(
        models=_models(),
        orchestrator=_orchestrator_config(),
        evaluation=EvaluationConfig(
            output_dir=str(output_dir), harness_adapter={"type": "comparison_single"}
        ),
    )


@dataclass
class _RunResult:
    orch: Orchestrator
    writer: InMemoryAggregateWriter
    # The orchestrator logger is cached by name across the process, so its
    # in-memory ``logs`` list carries prior runs' entries too. This marks where
    # this run's entries begin, so the table lookup reads only this run.
    log_baseline: int


def _run(config: RunConfig, output_dir: Path) -> _RunResult:
    recorder = _Executed()
    writer = InMemoryAggregateWriter()
    orch = Orchestrator(
        config,
        deps=OrchestratorDeps(
            runtime_backend=InMemoryRuntimeBackend(),
            conductor_factory=lambda ctx: _ComparisonConductor(ctx, _PassingGrader(), recorder),
            run_aggregate_writer=writer,
        ),
    )
    orch.load_tasks()
    log_baseline = len(orch.logger.logs)
    orch.run(run_id=output_dir.name, output_dir=output_dir)
    return _RunResult(orch=orch, writer=writer, log_baseline=log_baseline)


def _only_bundle(writer: InMemoryAggregateWriter) -> Any:
    bundles = list(writer.runs.values())
    assert len(bundles) == 1, f"expected one run bundle, got {len(bundles)}"
    return bundles[0]


def _logged_table(result: _RunResult) -> str | None:
    """The comparison table this run logged, or None if it logged none."""
    for entry in result.orch.logger.logs[result.log_baseline :]:
        if HARNESS_COMPARISON_FOOTNOTE in entry["message"]:
            return entry["message"]
    return None


# ---------------------------------------------------------------------------
# Mixed run — the comparison populates and renders
# ---------------------------------------------------------------------------


class TestMixedRunSurfacesTheComparison:
    def test_metadata_slices_carry_the_per_harness_dimensions(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "results" / "mixed_slices"
        result = _run(_mixed_config(output_dir), output_dir)

        slices = _only_bundle(result.writer).metadata_slices
        assert slices is not None

        assert set(slices["by_harness_entry"]) == {NATIVE_BUCKET, "terminal_bench"}
        assert set(slices["by_execution_mode"]) == {"engine_loop", "delegated"}
        assert set(slices["by_harness_and_task_family"]) == {
            f"{NATIVE_BUCKET}::{_FAMILY}",
            f"terminal_bench::{_FAMILY}",
        }

    def test_rendered_table_tags_cost_and_turns_and_carries_the_footnote(
        self, tmp_path: Path
    ) -> None:
        output_dir = tmp_path / "results" / "mixed_table"
        result = _run(_mixed_config(output_dir), output_dir)

        # The orchestrator logged the table beside the aggregate line.
        logged = _logged_table(result)
        assert logged is not None
        assert "cost (per-harness basis)" in logged
        assert "turns (per-harness basis)" in logged
        assert HARNESS_COMPARISON_FOOTNOTE in logged

        # And the formatter reproduces it from the produced slices.
        slices = _only_bundle(result.writer).metadata_slices
        table = format_harness_comparison_table(
            {
                key: slices[key]
                for key in (
                    "by_harness_entry",
                    "by_execution_mode",
                    "by_harness_and_task_family",
                )
            }
        )
        assert table is not None
        assert "cost (per-harness basis)" in table
        assert "turns (per-harness basis)" in table
        assert HARNESS_COMPARISON_FOOTNOTE in table


# ---------------------------------------------------------------------------
# Single-adapter run — degrade gracefully
# ---------------------------------------------------------------------------


class TestSingleAdapterRunRendersNothing:
    def test_single_native_bucket_and_no_table(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "results" / "single_run"
        result = _run(_single_adapter_config(output_dir), output_dir)

        slices = _only_bundle(result.writer).metadata_slices
        assert slices is not None

        # Everything lands in the single native bucket; nothing to compare.
        assert set(slices["by_harness_entry"]) == {NATIVE_BUCKET}
        assert set(slices["by_harness_and_task_family"]) == {f"{NATIVE_BUCKET}::{_FAMILY}"}

        # No table on the run surface.
        assert _logged_table(result) is None
        table = format_harness_comparison_table(
            {
                key: slices[key]
                for key in (
                    "by_harness_entry",
                    "by_execution_mode",
                    "by_harness_and_task_family",
                )
            }
        )
        assert table is None
