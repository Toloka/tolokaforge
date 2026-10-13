"""Single-box runs keep their dispatch and their on-disk bundles byte for byte.

The whole-trial seam is selected by ``isinstance(backend, TrialOwningBackend)``
and nothing else, so only the remote backend may take it. These tests lock the
two halves of that promise: no single-box backend satisfies the Protocol and every
one still gets the provisioning executor, and the bundles the provisioning
executor writes — a completed trial and a trial whose environment never came up —
match the golden snapshots.
"""

from __future__ import annotations

import warnings
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.canonical._factories import make_env_endpoints, make_task_config, make_trial_spec
from tolokaforge.core.conductor import ConductorContext
from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import (
    EvaluationConfig,
    Grade,
    GradeComponents,
    Metrics,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    TaskConfig,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.orchestrator import Orchestrator
from tolokaforge.core.output.artifacts import FileArtifactWriter
from tolokaforge.core.runtime import EnvHandle, InMemoryRuntimeBackend, ProvisionError
from tolokaforge.core.trial import TrialResult, TrialSpec
from tolokaforge.core.trial_executor import ProvisioningTrialExecutor, TrialOwningBackend
from tolokaforge.core.trial_node import TrialExecutionContext, build_trial_execution_unit

pytestmark = pytest.mark.canonical

_FIXED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _config(runtime: str | None = None) -> RunConfig:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return RunConfig(
            models={
                "agent": ModelConfig(provider="openai", name="gpt-4"),
                "user": ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6"),
            },
            orchestrator=OrchestratorConfig(
                workers=1, repeats=1, auto_start_services=False, runtime=runtime
            ),
            evaluation=EvaluationConfig(output_dir="/tmp/unused"),
        )


class _WritingConductor:
    """Writes the bundle a conductor writes, from a fixed trajectory."""

    def __init__(self, ctx: ConductorContext) -> None:
        self.ctx = ctx

    def run(self, spec: TrialSpec, task_config: TaskConfig) -> TrialResult:
        trajectory = Trajectory(
            task_id=task_config.task_id,
            trial_index=0,
            start_ts=_FIXED,
            end_ts=_FIXED,
            status=TrialStatus.COMPLETED,
            messages=[],
            metrics=Metrics(),
            grade=Grade(
                binary_pass=True,
                score=1.0,
                components=GradeComponents(),
                reasons="fixed-grade",
            ),
        )
        trial_dir = self.ctx.output_dir / "trials" / task_config.task_id / "0"
        self.ctx.artifact_writer.write_trajectory(trial_dir, trajectory)
        self.ctx.artifact_writer.write_tool_log(trial_dir, trajectory)
        self.ctx.artifact_writer.write_metrics(trial_dir, trajectory)
        self.ctx.artifact_writer.write_grade(trial_dir, trajectory.grade)
        return TrialResult.from_trajectory(trial_id=spec.trial_id, trajectory=trajectory)


class _ProvisionFailsBackend(InMemoryRuntimeBackend):
    def provision(self, spec: TrialSpec) -> EnvHandle:
        raise ProvisionError(trial_id=spec.trial_id, stage="provision", reason="no capacity")


def _unit(tmp_path: Path, backend: Any):
    task = make_task_config(task_id="task-1")
    unit = build_trial_execution_unit(
        TrialExecutionContext(
            adapter=SimpleNamespace(trial_grader_name="runner_rpc"),  # type: ignore[arg-type]
            artifact_writer=FileArtifactWriter(),
            config=_config(),
            logger=get_logger("byte-identity", strict=False),
            agent_client=None,  # type: ignore[arg-type]
            runtime_backend=backend,
            output_dir=tmp_path,
            request_limiter=None,
            grader_config=None,
            grpc_max_message_mb=50,
            conductor_factory=_WritingConductor,  # type: ignore[arg-type]
        )
    )
    spec = make_trial_spec(
        trial_id="task-1:0", task_id="task-1", env_endpoints=make_env_endpoints()
    )
    return unit, task, spec


def _bundle(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): path.read_text()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter(float(n) for n in range(1, 100))
    monkeypatch.setattr(
        "tolokaforge.core.trial_executor.time", SimpleNamespace(monotonic=lambda: next(ticks))
    )
    monkeypatch.setattr(
        "tolokaforge.core.trial_executor.datetime",
        SimpleNamespace(now=lambda tz=None: _FIXED),
    )


def test_a_completed_trial_leaves_the_same_bundle_bytes(tmp_path: Path, canon_snapshot) -> None:
    unit, task, spec = _unit(tmp_path, InMemoryRuntimeBackend())

    unit.execute(spec, task)

    canon_snapshot("single_box_bundles").assert_match(_bundle(tmp_path), "completed.json")


def test_a_trial_that_never_provisioned_leaves_the_same_bundle_bytes(
    tmp_path: Path, canon_snapshot
) -> None:
    unit, task, spec = _unit(tmp_path, _ProvisionFailsBackend())

    unit.execute(spec, task)

    canon_snapshot("single_box_bundles").assert_match(_bundle(tmp_path), "provision_failure.json")


@pytest.mark.parametrize("runtime", [None, "shared", "per_trial", "in_memory"])
def test_no_single_box_backend_is_trial_owning_and_each_gets_the_provisioning_executor(
    tmp_path: Path, runtime: str | None
) -> None:
    orchestrator = Orchestrator(_config(runtime))

    backend = orchestrator._construct_runtime_backend("executor:50051")
    unit, _, _ = _unit(tmp_path, backend)

    assert not isinstance(backend, TrialOwningBackend)
    assert isinstance(unit.executor, ProvisioningTrialExecutor)
    assert unit.executor.runtime_backend is backend
    assert unit.executor.conductor is unit.conductor


def test_the_in_memory_test_backend_is_not_trial_owning() -> None:
    assert not isinstance(InMemoryRuntimeBackend(), TrialOwningBackend)
