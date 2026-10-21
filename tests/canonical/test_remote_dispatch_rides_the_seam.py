"""A native trial reaches a graded result through the backend the orchestrator builds for ``--runtime remote``.

The adapter is the one the :class:`Orchestrator` builds from the
``runtime='remote'`` alias; only the transport is replaced, by an in-process
node. The trial runs through the same execution unit the orchestrator builds, so
the whole-trial seam is what is under test: ``RemoteTrialExecutor`` ->
``GrpcNodeDispatchAdapter.run_trial_node`` -> node client -> node -> graded
result. A coding-harness run is refused before any dispatch, since a dispatch
node runs the native loop without harness mode.
"""

from __future__ import annotations

import tempfile
import warnings
from pathlib import Path

import pytest

from tests.canonical._factories import make_env_endpoints
from tests.canonical._node_fakes import (
    InProcessNode,
    InProcessNodeClientFactory,
    write_native_pack,
)
from tolokaforge.core.dispatch.grpc_node import GrpcNodeDispatchAdapter
from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import (
    EvaluationConfig,
    GraderConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    TaskConfig,
    TrialStatus,
)
from tolokaforge.core.orchestrator import Orchestrator
from tolokaforge.core.output.artifacts import InMemoryArtifactWriter
from tolokaforge.core.trial import TrialNodeInputs, TrialSpec
from tolokaforge.core.trial_executor import RemoteTrialExecutor
from tolokaforge.core.trial_node import (
    TrialExecutionContext,
    build_single_task_adapter,
    build_trial_execution_unit,
)
from tolokaforge.trial_node import trial_node_pb2
from tolokaforge.trial_node.protocol import NODE_PROTOCOL_VERSION

pytestmark = [pytest.mark.canonical, pytest.mark.usefixtures("env_backed_secrets")]

_NODE = "node-a:50061"
_AGENT = ModelConfig(provider="openai", name="gpt-4")
_USER = ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6")
_MAX_MB = 9


def _orchestrator(tmp_path: Path) -> tuple[Orchestrator, RunConfig]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        config = RunConfig(
            models={"agent": _AGENT, "user": _USER},
            orchestrator=OrchestratorConfig(
                workers=1,
                repeats=1,
                runtime="remote",
                remote={"nodes": [_NODE], "lease_timeout_s": 5},
            ),
            evaluation=EvaluationConfig(output_dir=str(tmp_path / "out")),
            compute={"grpc_max_message_mb": _MAX_MB},
        )
    return Orchestrator(config), config


def _spec_for(task: TaskConfig) -> TrialSpec:
    return TrialSpec(
        trial_id=f"{task.task_id}:0",
        run_id="remote-seam",
        task=build_single_task_adapter(task).to_task_description(task.task_id),
        agent_model_config=_AGENT,
        user_model_config=_USER,
        env_endpoints=make_env_endpoints(),
    )


def test_native_trial_dispatches_through_the_leased_node_to_a_graded_result(
    tmp_path: Path,
) -> None:
    task = write_native_pack(tmp_path / "pack")
    orchestrator, config = _orchestrator(tmp_path)
    adapter = orchestrator._build_dispatch_adapter(orchestrator._dispatch_config())
    assert isinstance(adapter, GrpcNodeDispatchAdapter)
    node = InProcessNode(tmp_path / "node-out")
    factory = InProcessNodeClientFactory(node)
    adapter.node_client_factory = factory
    unit = build_trial_execution_unit(
        TrialExecutionContext(
            adapter=build_single_task_adapter(task),
            artifact_writer=InMemoryArtifactWriter(),
            config=config,
            logger=get_logger("remote-seam", strict=False),
            agent_client=None,  # type: ignore[arg-type] — the node builds its own agent client
            runtime_backend=None,
            dispatch_adapter=adapter,
            output_dir=tmp_path / "out",
            request_limiter=None,
            grader_config=GraderConfig(name="runner_rpc"),
            grpc_max_message_mb=_MAX_MB,
        )
    )
    try:
        result = unit.execute(_spec_for(task), task)
    finally:
        unit.close()
        adapter.close()

    assert isinstance(unit.executor, RemoteTrialExecutor)
    (client,) = factory.clients
    assert client.address == _NODE
    assert client.max_message_mb == _MAX_MB
    assert client.closed
    (request,) = client.requests
    assert isinstance(request, trial_node_pb2.RunTrialRequest)
    assert request.client_protocol_version == NODE_PROTOCOL_VERSION
    assert TrialNodeInputs.model_validate_json(
        request.trial_node_inputs_json
    ).trial_spec.trial_id == ("flat:0")
    assert result.trajectory.status is TrialStatus.COMPLETED
    assert result.trajectory.grade is not None


def test_a_coding_harness_run_is_refused_rather_than_dispatched(
    test_data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A node-runnable delegated adapter (terminal_bench) is not refused for its
    type, but a coding-harness run on it is — the node runs the native loop."""
    from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    adapter = TerminalBenchAdapter(
        {
            "terminal_bench_dir": str(test_data_dir / "terminal_bench_tasks"),
            "staging_root": str(tmp_path / "staging"),
        }
    )
    delegated = adapter.get_task("echo-hello")
    assert delegated.adapter_type == "terminal_bench"
    orchestrator, _ = _orchestrator(tmp_path)
    orchestrator.adapter = adapter
    orchestrator.tasks = [delegated]

    # The node-runnable terminal_bench adapter alone is not refused.
    orchestrator._refuse_settings_the_node_does_not_carry()

    # Configuring coding-harness mode on top is refused.
    orchestrator.config.models["agent"] = _AGENT.model_copy(update={"harness": "claude-code"})
    with pytest.raises(ValueError, match="coding-harness mode"):
        orchestrator._refuse_unsupported_remote_run()
