"""The per-trial execution unit threads the same collaborators the orchestrator composes.

A recording conductor captures the :class:`ConductorContext` it is built with, so
the assertions are about which objects reach the conductor, not about trial output.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.conductor import ConductorContext, InMemoryConductor
from tolokaforge.core.models.run_config import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
)
from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
from tolokaforge.core.output.artifacts import InMemoryArtifactWriter
from tolokaforge.core.runtime import ProvisionError
from tolokaforge.core.trial_executor import ProvisioningTrialExecutor
from tolokaforge.core.trial_node import (
    TrialExecutionContext,
    TrialExecutionUnit,
    build_trial_execution_unit,
)

pytestmark = pytest.mark.canonical


class _RecordingGrader:
    def __init__(self) -> None:
        self.closed = 0

    def grade(self, *args: Any, **kwargs: Any) -> None:
        return None

    def close(self) -> None:
        self.closed += 1


class _RecordingConductor(InMemoryConductor):
    def __init__(self, ctx: ConductorContext) -> None:
        super().__init__()
        self.ctx = ctx


def _config() -> RunConfig:
    return RunConfig(
        models={
            "agent": ModelConfig(provider="openai", name="gpt-4"),
            "user": ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6"),
        },
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir="/tmp/test_output"),
    )


@pytest.fixture
def grader(monkeypatch: pytest.MonkeyPatch) -> _RecordingGrader:
    built = _RecordingGrader()
    monkeypatch.setattr(
        "tolokaforge.core.trial_node.load_trial_grader",
        lambda name: lambda ctx: built,
    )
    return built


def _orchestrator_unit(
    tmp_path: Path, writer: InMemoryArtifactWriter, backend: Any, agent_client: Any
) -> tuple[Orchestrator, TrialExecutionUnit, Any]:
    def factory(ctx: ConductorContext) -> _RecordingConductor:
        return _RecordingConductor(ctx)

    orch = Orchestrator(
        _config(), deps=OrchestratorDeps(artifact_writer=writer, conductor_factory=factory)
    )
    orch.adapter = MagicMock()
    unit = orch._build_trial_unit(
        agent_client=agent_client,
        runtime_backend=backend,
        output_dir=tmp_path,
        request_limiter=None,
    )
    return orch, unit, factory


def test_unit_threads_the_same_objects_the_orchestrator_composes(
    tmp_path: Path, grader: _RecordingGrader
) -> None:
    writer = InMemoryArtifactWriter()
    backend = MagicMock()
    agent_client = MagicMock()
    orch, orch_unit, factory = _orchestrator_unit(tmp_path, writer, backend, agent_client)

    direct = build_trial_execution_unit(
        TrialExecutionContext(
            adapter=orch.adapter,
            artifact_writer=writer,
            config=orch.config,
            logger=orch.logger,
            agent_client=agent_client,
            runtime_backend=backend,
            output_dir=tmp_path,
            request_limiter=None,
            grader_config=orch.config.grader,
            grpc_max_message_mb=orch._grpc_max_message_mb(),
            conductor_factory=factory,
        )
    )

    for unit in (orch_unit, direct):
        ctx = unit.conductor.ctx
        assert ctx.trial_grader is grader
        assert unit.grader is grader
        assert ctx.runtime_backend is backend
        assert ctx.artifact_writer is writer
        assert ctx.agent_client is agent_client
        assert ctx.config is orch.config
        assert ctx.adapter is orch.adapter
        assert ctx.output_dir == tmp_path
        assert isinstance(unit.provisioning_executor, ProvisioningTrialExecutor)
        assert unit.provisioning_executor.runtime_backend is backend
        assert unit.provisioning_executor.conductor is unit.conductor
        assert unit.provisioning_executor.artifact_writer is writer


def test_the_default_conductor_comes_from_the_registry(
    tmp_path: Path, grader: _RecordingGrader, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "tolokaforge.core.trial_node.load_conductor",
        lambda name: lambda ctx: _RecordingConductor(ctx),
    )
    orch = Orchestrator(_config(), deps=OrchestratorDeps(artifact_writer=InMemoryArtifactWriter()))
    orch.adapter = MagicMock()
    unit = orch._build_trial_unit(
        agent_client=MagicMock(),
        runtime_backend=MagicMock(),
        output_dir=tmp_path,
        request_limiter=None,
    )
    assert isinstance(unit.conductor, _RecordingConductor)


def test_closing_the_unit_closes_the_grader(tmp_path: Path, grader: _RecordingGrader) -> None:
    _, unit, _ = _orchestrator_unit(tmp_path, InMemoryArtifactWriter(), MagicMock(), MagicMock())
    unit.close()
    assert grader.closed == 1


def test_orchestrator_teardown_closes_the_unit_grader(
    tmp_path: Path, grader: _RecordingGrader
) -> None:
    orch, _, _ = _orchestrator_unit(tmp_path, InMemoryArtifactWriter(), MagicMock(), MagicMock())
    orch._close_trial_units()
    assert grader.closed == 1


def test_a_failed_conductor_build_closes_the_resolved_grader(
    tmp_path: Path, grader: _RecordingGrader
) -> None:
    def failing(_ctx: ConductorContext) -> Any:
        raise RuntimeError("conductor build failed")

    ctx = TrialExecutionContext(
        adapter=MagicMock(),
        artifact_writer=InMemoryArtifactWriter(),
        config=_config(),
        logger=MagicMock(),
        agent_client=MagicMock(),
        runtime_backend=MagicMock(),
        output_dir=tmp_path,
        request_limiter=None,
        grader_config=None,
        grpc_max_message_mb=4,
        conductor_factory=failing,
    )
    with pytest.raises(RuntimeError, match="conductor build failed"):
        build_trial_execution_unit(ctx)
    assert grader.closed == 1


def test_provision_error_policy_propagates_when_asked(tmp_path: Path) -> None:
    backend = MagicMock()
    backend.provision.side_effect = ProvisionError(stage="provision", reason="no capacity")
    spec = MagicMock()
    spec.trial_id = "task:0"
    executor = ProvisioningTrialExecutor(
        runtime_backend=backend,
        conductor=MagicMock(),
        logger=MagicMock(),
        output_dir=tmp_path,
        artifact_writer=InMemoryArtifactWriter(),
        provision_error="propagate",
    )
    with pytest.raises(ProvisionError, match="no capacity"):
        executor.execute(spec, MagicMock())


def test_provision_error_policy_rejects_unknown_values(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="provision_error"):
        ProvisioningTrialExecutor(
            runtime_backend=MagicMock(),
            conductor=MagicMock(),
            logger=MagicMock(),
            output_dir=tmp_path,
            artifact_writer=InMemoryArtifactWriter(),
            provision_error="ignore",  # type: ignore[arg-type]
        )
