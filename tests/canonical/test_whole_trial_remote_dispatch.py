"""``--runtime remote`` ships each whole trial to a leased node and rebuilds a reduced bundle.

An :class:`InProcessNode` — a real :class:`TrialNodeServiceImpl` running the real
``run_trial_from_inputs`` over the ``in_memory`` substrate — stands in for the
gRPC transport. The control plane is built exactly as ``--runtime remote``
builds it (a real ``Orchestrator``, a real ``GrpcNodeDispatchAdapter``, the real
``build_trial_execution_unit``), so these tests pin the routing
``RemoteTrialExecutor`` -> ``dispatch_adapter.run_trial_node`` -> node -> graded
``TrialResult``, the reduced bundle on the control plane, and how each node
failure maps onto the orchestrator's retry classes. No Docker, no LLM.
"""

from __future__ import annotations

import json
import re
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.canonical._node_fakes import (
    InProcessNode,
    InProcessNodeClientFactory,
    write_native_pack,
)
from tolokaforge.core.dispatch.errors import DispatchTransient
from tolokaforge.core.dispatch.grpc_node import GrpcNodeDispatchAdapter
from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import (
    EvaluationConfig,
    GraderConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    TaskConfig,
    TerminationReason,
    TrialStatus,
)
from tolokaforge.core.node_registry import InMemoryNodeRegistry
from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
from tolokaforge.core.output.artifacts import FileArtifactWriter
from tolokaforge.core.plugin_registry import UnknownImplementationError
from tolokaforge.core.runtime import ProvisionError
from tolokaforge.core.trial import TrialNodeInputs, TrialResult, TrialSpec
from tolokaforge.core.trial_executor import RemoteTrialExecutor
from tolokaforge.core.trial_node import (
    TaskPackDriftError,
    TrialExecutionContext,
    build_single_task_adapter,
    build_trial_execution_unit,
    compute_task_pack_hash,
)
from tolokaforge.secrets import get_default
from tolokaforge.trial_node.protocol import NodeTransientError

pytestmark = [pytest.mark.canonical, pytest.mark.usefixtures("env_backed_secrets")]

_NODES = ["node-a:50061", "node-b:50061"]
_AGENT = ModelConfig(provider="openai", name="gpt-4")
_USER = ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6")
_MAX_MB = 9


def _config(out: Path, project: Path | None = None, **orchestrator: Any) -> RunConfig:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return RunConfig(
            models={"agent": _AGENT, "user": _USER, "judge": _USER},
            orchestrator=OrchestratorConfig(
                **{
                    "workers": 1,
                    "repeats": 1,
                    "shuffle_trials": False,
                    "runtime": "remote",
                    "remote": {"nodes": _NODES, "lease_timeout_s": 5},
                    **orchestrator,
                }
            ),
            evaluation=EvaluationConfig(
                output_dir=str(out), projects=[str(project)] if project else []
            ),
            compute={"grpc_max_message_mb": _MAX_MB},
        )


def _grpc_node_adapter(factory: InProcessNodeClientFactory) -> GrpcNodeDispatchAdapter:
    return GrpcNodeDispatchAdapter(
        registry=InMemoryNodeRegistry(_NODES, lease_timeout_s=5),
        grpc_max_message_mb=_MAX_MB,
        connect_timeout_s=11.0,
        retry_interval_s=0.5,
        node_client_factory=factory,
    )


def _spec_for(task: TaskConfig, trial_index: int = 0, agent: ModelConfig = _AGENT) -> TrialSpec:
    from tests.canonical._factories import make_env_endpoints

    return TrialSpec(
        trial_id=f"{task.task_id}:{trial_index}",
        run_id="remote-run",
        task=build_single_task_adapter(task).to_task_description(task.task_id),
        agent_model_config=agent,
        user_model_config=_USER,
        max_turns=task.max_turns,
        env_endpoints=make_env_endpoints(),
    )


class _Unit:
    """An orchestrator-shaped execution unit over a remote backend and one in-process node."""

    def __init__(
        self,
        tmp_path: Path,
        node: InProcessNode,
        *,
        orchestrator: dict[str, Any] | None = None,
        agent: ModelConfig = _AGENT,
        **client_kwargs: Any,
    ) -> None:
        self.tmp_path = tmp_path
        self.agent = agent
        self.task = write_native_pack(tmp_path / "pack")
        self.factory = InProcessNodeClientFactory(node, **client_kwargs)
        self.backend = _grpc_node_adapter(self.factory)
        self.out = tmp_path / "out"
        self.unit = build_trial_execution_unit(
            TrialExecutionContext(
                adapter=build_single_task_adapter(self.task),
                artifact_writer=FileArtifactWriter(),
                config=_config(self.out, **(orchestrator or {})),
                logger=get_logger("remote-dispatch", strict=False),
                agent_client=None,  # type: ignore[arg-type] — never reached on the control plane
                runtime_backend=None,
                dispatch_adapter=self.backend,
                output_dir=self.out,
                request_limiter=None,
                grader_config=GraderConfig(name="runner_rpc"),
                grpc_max_message_mb=_MAX_MB,
            )
        )

    def execute(self, trial_index: int = 0) -> TrialResult:
        return self.unit.execute(_spec_for(self.task, trial_index, self.agent), self.task)

    def close(self) -> None:
        self.unit.close()
        self.backend.close()


@pytest.fixture
def make_unit(tmp_path: Path):
    units: list[_Unit] = []

    def make(node: InProcessNode, **kwargs: Any) -> _Unit:
        unit = _Unit(tmp_path, node, **kwargs)
        units.append(unit)
        return unit

    yield make
    for unit in units:
        unit.close()


def _node(tmp_path: Path, **kwargs: Any) -> InProcessNode:
    return InProcessNode(tmp_path / "node-out", **kwargs)


def _raising(exc: BaseException):
    def run(inputs: TrialNodeInputs) -> TrialResult:
        raise exc

    return run


# ---- The seam -------------------------------------------------------------------


def test_a_remote_backend_routes_the_trial_through_the_remote_executor_to_a_graded_result(
    tmp_path: Path, make_unit
) -> None:
    node = _node(tmp_path)
    harness = make_unit(node)

    result = harness.execute()

    assert isinstance(harness.unit.executor, RemoteTrialExecutor)
    # The dispatch path builds no control-plane conductor: the node owns it.
    assert harness.unit.conductor is None
    assert result.trial_id == "flat:0"
    assert result.trajectory.status is TrialStatus.COMPLETED
    assert result.trajectory.grade is not None
    (client,) = harness.factory.clients
    assert client.address in _NODES
    assert client.max_message_mb == _MAX_MB
    assert client.connect_calls == [{"timeout": 11.0, "retry_interval": 0.5}]
    assert client.closed


def test_the_node_receives_inputs_it_can_run_the_trial_from(tmp_path: Path, make_unit) -> None:
    node = _node(tmp_path)
    harness = make_unit(node)

    harness.execute()

    (inputs,) = node.received
    assert inputs.runtime == "auto"
    assert inputs.conductor == "in_process"
    assert inputs.grpc_max_message_mb == _MAX_MB
    assert inputs.grader_config == GraderConfig(name="runner_rpc")
    assert inputs.task.task_id == "flat"
    assert inputs.task_pack_hash == compute_task_pack_hash(harness.task.source_dir)
    # The node supplies its own output directory under its own root.
    assert inputs.output_dir == tmp_path / "node-out" / "remote-run"


def _string_leaves(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _string_leaves(value)
    elif isinstance(node, list):
        for item in node:
            yield from _string_leaves(item)


def test_the_node_sees_no_secret_values(tmp_path: Path, make_unit) -> None:
    node = _node(tmp_path)
    harness = make_unit(node)

    harness.execute()

    (client,) = harness.factory.clients
    (request,) = client.requests
    wire = json.loads(request.trial_node_inputs_json)
    leaves = list(_string_leaves(wire))
    names = {"OPENROUTER_API_KEY"}
    for leaf in leaves:
        names.update(re.findall(r"\$\{secret:([^}]+)\}", leaf))
    secrets = get_default()
    resolved = {name: secrets.get_secret(name) for name in names}
    assert resolved["OPENROUTER_API_KEY"] == "sk-openrouter-test"
    for name, value in resolved.items():
        if not value:
            continue
        leaking = [leaf for leaf in leaves if value in leaf]
        assert not leaking, f"the value of secret {name} reached the node's wire payload"


def test_the_reduced_bundle_is_written_from_the_returned_trajectory(
    tmp_path: Path, make_unit
) -> None:
    harness = make_unit(_node(tmp_path))

    result = harness.execute()

    trial_dir = harness.out / "trials" / "flat" / "0"
    assert sorted(p.name for p in trial_dir.iterdir()) == [
        "grade.yaml",
        "metrics.yaml",
        "tool_log.yaml",
        "trajectory.yaml",
    ]
    grade = yaml.safe_load((trial_dir / "grade.yaml").read_text())
    assert grade["reasons"] == result.trajectory.grade.reasons
    trajectory = yaml.safe_load((trial_dir / "trajectory.yaml").read_text())
    assert trajectory["task_id"] == "flat"


def test_the_leased_node_is_released_after_every_trial(tmp_path: Path, make_unit) -> None:
    harness = make_unit(_node(tmp_path))

    for index in range(len(_NODES) + 1):
        harness.execute(index)

    assert len(harness.factory.clients) == len(_NODES) + 1
    assert all(client.closed for client in harness.factory.clients)


def test_a_full_orchestrator_run_dispatches_every_trial_to_the_node(tmp_path: Path) -> None:
    write_native_pack(tmp_path / "pack")
    node = _node(tmp_path)
    factory = InProcessNodeClientFactory(node)
    config = _config(tmp_path / "out", tmp_path / "pack", repeats=2, auto_start_services=False)
    orch = Orchestrator(config, deps=OrchestratorDeps(dispatch_adapter=_grpc_node_adapter(factory)))

    run_dir = orch.run(run_id="remote-run", output_dir=tmp_path / "run")

    assert [inputs.trial_spec.trial_id for inputs in node.received] == ["flat:0", "flat:1"]
    assert sorted(t.status for t in orch.results) == [TrialStatus.COMPLETED] * 2
    assert all(t.grade is not None for t in orch.results)
    bundles = sorted(run_dir.glob("trials/flat/*/grade.yaml"))
    assert len(bundles) == 2


def test_the_explicit_dispatch_block_drives_the_whole_trial_path(tmp_path: Path) -> None:
    """``orchestrator.dispatch={adapter: grpc_node, config: {nodes}}`` selects the dispatch path."""
    write_native_pack(tmp_path / "pack")
    node = _node(tmp_path)
    factory = InProcessNodeClientFactory(node)
    config = RunConfig(
        models={"agent": _AGENT, "user": _USER, "judge": _USER},
        orchestrator=OrchestratorConfig(
            workers=1,
            repeats=1,
            auto_start_services=False,
            dispatch={"adapter": "grpc_node", "config": {"nodes": _NODES, "lease_timeout_s": 5}},
        ),
        evaluation=EvaluationConfig(
            output_dir=str(tmp_path / "out"), projects=[str(tmp_path / "pack")]
        ),
        compute={"grpc_max_message_mb": _MAX_MB},
    )
    orch = Orchestrator(config, deps=OrchestratorDeps(dispatch_adapter=_grpc_node_adapter(factory)))

    orch.run(run_id="dispatch-run", output_dir=tmp_path / "run")

    assert [inputs.trial_spec.trial_id for inputs in node.received] == ["flat:0"]
    assert [t.status for t in orch.results] == [TrialStatus.COMPLETED]


# ---- Node failures map onto the orchestrator's retry classes ---------------------


def _is_retryable(result: TrialResult) -> bool:
    return Orchestrator._is_retryable_trajectory(result.trajectory)


def _failure_bundle(harness: _Unit) -> dict[str, Any]:
    return yaml.safe_load((harness.out / "trials" / "flat" / "0" / "metrics.yaml").read_text())


def test_a_node_provision_error_is_a_non_retryable_provision_failure(
    tmp_path: Path, make_unit
) -> None:
    harness = make_unit(
        _node(
            tmp_path,
            run_trial=_raising(
                ProvisionError(trial_id="flat:0", stage="await_ready", reason="db never healthy")
            ),
        )
    )

    result = harness.execute()

    assert result.trajectory.termination_reason is TerminationReason.PROVISION_ERROR
    assert result.trajectory.provision_stage == "await_ready"
    assert result.trajectory.grade is None
    assert not _is_retryable(result)
    metrics = _failure_bundle(harness)
    assert metrics["error_reason"] == "db never healthy"
    assert metrics["error_stage"] == "await_ready"


@pytest.mark.parametrize(
    ("raised", "error_type", "reason_fragment"),
    [
        (TaskPackDriftError("pack drifted"), "TaskPackDrift", "pack drifted"),
        (NotImplementedError("adapter is not node-runnable"), "NotImplemented", "node-runnable"),
        (
            UnknownImplementationError("nope", "tolokaforge.graders", ["runner_rpc"]),
            "InvalidConfig",
            "nope",
        ),
    ],
)
def test_a_deterministic_node_refusal_is_a_non_retryable_loud_failure(
    tmp_path: Path, make_unit, raised: Exception, error_type: str, reason_fragment: str
) -> None:
    harness = make_unit(_node(tmp_path, run_trial=_raising(raised)))

    result = harness.execute()

    assert result.trajectory.termination_reason is TerminationReason.PROVISION_ERROR
    assert not _is_retryable(result)
    reason = _failure_bundle(harness)["error_reason"]
    assert reason.startswith(f"{error_type}: ")
    assert reason_fragment in reason


def test_a_missing_node_secret_is_refused_before_the_trial_runs(tmp_path: Path, make_unit) -> None:
    node = _node(tmp_path)
    harness = make_unit(node)
    harness.task = harness.task.model_copy(update={"description": "key ${secret:NODE_ONLY_KEY}"})

    result = harness.execute()

    assert node.received == []
    assert not _is_retryable(result)
    reason = _failure_bundle(harness)["error_reason"]
    assert reason.startswith("SecretPreflight: ")
    assert "${secret:NODE_ONLY_KEY}" in reason


def test_a_malformed_secret_reference_is_refused_before_the_trial_runs(
    tmp_path: Path, make_unit
) -> None:
    node = _node(tmp_path)
    harness = make_unit(node)
    harness.task = harness.task.model_copy(update={"description": "key ${secret:oops"})

    result = harness.execute()

    assert node.received == []
    assert (
        "malformed reference at inputs.task.description" in _failure_bundle(harness)["error_reason"]
    )
    assert result.trajectory.termination_reason is TerminationReason.PROVISION_ERROR


def test_a_node_older_than_the_engine_is_a_non_retryable_version_skew(
    tmp_path: Path, make_unit
) -> None:
    node = _node(tmp_path)
    harness = make_unit(node, client_protocol_version=0)

    result = harness.execute()

    assert node.received == []
    assert not _is_retryable(result)
    reason = _failure_bundle(harness)["error_reason"]
    assert reason.startswith("VersionSkew: ")
    assert "upgrade the engine" in reason


def _assert_both_nodes_are_free(harness: _Unit) -> None:
    harness.backend.registry.lease("probe:0")
    harness.backend.registry.lease("probe:1")


def test_an_internal_node_failure_raises_so_the_orchestrator_retries(
    tmp_path: Path, make_unit
) -> None:
    harness = make_unit(_node(tmp_path, run_trial=_raising(RuntimeError("node blew up"))))

    with pytest.raises(DispatchTransient, match="node blew up"):
        harness.execute()

    _assert_both_nodes_are_free(harness)


def test_a_stray_value_error_deep_in_a_trial_is_not_an_invalid_config(
    tmp_path: Path, make_unit
) -> None:
    harness = make_unit(_node(tmp_path, run_trial=_raising(ValueError("deep in the trial"))))

    with pytest.raises(DispatchTransient, match="deep in the trial"):
        harness.execute()

    _assert_both_nodes_are_free(harness)


def test_an_unreachable_node_raises_so_the_orchestrator_retries(tmp_path: Path, make_unit) -> None:
    harness = make_unit(_node(tmp_path))
    harness.backend.node_client_factory = _unreachable_factory

    with pytest.raises(DispatchTransient, match="unavailable"):
        harness.execute()

    _assert_both_nodes_are_free(harness)


def _unreachable_factory(address: str, max_message_mb: int):
    class _Down:
        def connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None:
            raise NodeTransientError(f"node {address} is unavailable during HealthCheck")

        def run_trial(self, trial_node_inputs_json: str):
            raise AssertionError("an unreachable node is never sent a trial")

        def close(self) -> None:
            pass

    return _Down()


def test_the_orchestrator_retries_a_transient_node_failure_and_completes_the_trial(
    tmp_path: Path,
) -> None:
    write_native_pack(tmp_path / "pack")
    attempts: list[int] = []

    def flaky(inputs: TrialNodeInputs) -> TrialResult:
        attempts.append(inputs.trial_spec.attempt_id)
        if len(attempts) == 1:
            raise RuntimeError("node blew up")
        from tests.canonical._node_fakes import run_in_memory

        return run_in_memory(inputs)

    node = _node(tmp_path, run_trial=flaky)
    config = _config(tmp_path / "out", tmp_path / "pack", auto_start_services=False)
    config.compute.max_attempt_retries = 1
    orch = Orchestrator(
        config,
        deps=OrchestratorDeps(
            dispatch_adapter=_grpc_node_adapter(InProcessNodeClientFactory(node))
        ),
    )

    orch.run()

    assert attempts == [0, 1]
    assert [t.status for t in orch.results] == [TrialStatus.COMPLETED]


def test_the_orchestrator_does_not_retry_a_deterministic_node_refusal(tmp_path: Path) -> None:
    write_native_pack(tmp_path / "pack")
    node = _node(tmp_path, run_trial=_raising(TaskPackDriftError("pack drifted")))
    config = _config(tmp_path / "out", tmp_path / "pack", auto_start_services=False)
    config.compute.max_attempt_retries = 2
    orch = Orchestrator(
        config,
        deps=OrchestratorDeps(
            dispatch_adapter=_grpc_node_adapter(InProcessNodeClientFactory(node))
        ),
    )

    orch.run()

    assert len(node.received) == 1
    (trajectory,) = orch.results
    assert trajectory.termination_reason is TerminationReason.PROVISION_ERROR


def test_a_busy_node_is_a_transient_failure(tmp_path: Path, make_unit) -> None:
    node = _node(tmp_path)
    harness = make_unit(node)
    node.service._trial_lock.acquire()  # a trial already in flight on the node
    try:
        with pytest.raises(DispatchTransient, match="already running a trial"):
            harness.execute()
    finally:
        node.service._trial_lock.release()


def test_the_node_reports_busy_in_its_health_check(tmp_path: Path) -> None:
    node = _node(tmp_path)
    from tolokaforge.trial_node import trial_node_pb2

    idle = node.service.HealthCheck(trial_node_pb2.HealthCheckRequest(), None)
    node.service._trial_lock.acquire()
    try:
        busy = node.service.HealthCheck(trial_node_pb2.HealthCheckRequest(), None)
    finally:
        node.service._trial_lock.release()

    assert (idle.status, idle.trial_in_flight) == ("ready", False)
    assert (busy.status, busy.trial_in_flight) == ("busy", True)


def test_the_response_of_a_failed_node_names_the_error_type_and_stage(tmp_path: Path) -> None:
    node = _node(tmp_path, run_trial=_raising(TaskPackDriftError("pack drifted")))
    client = InProcessNodeClientFactory(node)("node-a:50061", 50)
    task = write_native_pack(tmp_path / "pack")
    inputs = TrialNodeInputs(
        task=task,
        trial_spec=_spec_for(task),
        grpc_max_message_mb=50,
        runtime="auto",
        conductor="in_process",
    )

    outcome = client.run_trial(inputs.model_dump_json())

    assert not outcome.success
    assert (outcome.error_type, outcome.stage) == ("TaskPackDrift", "task_pack")
    assert "pack drifted" in outcome.reason


# ---- Run settings and fallback chains reach the node ---------------------------


_CARRIED = {
    "timeouts": {"turn_s": 45, "episode_s": 900},
    "max_turns": 7,
    "max_trial_tokens": 12345,
    "stuck_heuristics": {"max_repeated_tool_calls": 3},
    "agent_loop": "custom-loop",
}
_FALLBACK = ModelConfig(provider="openai", name="gpt-4o-mini")


class _Rebuilt:
    """What the node's trial body built: the run config and the agent client."""

    def __init__(self) -> None:
        self.contexts: list[TrialExecutionContext] = []


@pytest.fixture
def rebuilt(monkeypatch: pytest.MonkeyPatch) -> _Rebuilt:
    import tolokaforge.core.trial_node as trial_node_mod

    seen = _Rebuilt()
    real = trial_node_mod.build_trial_execution_unit

    def recording(ctx: TrialExecutionContext):
        seen.contexts.append(ctx)
        return real(ctx)

    monkeypatch.setattr(trial_node_mod, "build_trial_execution_unit", recording)
    return seen


def test_a_run_with_non_default_settings_dispatches_and_the_node_rebuilds_them(
    tmp_path: Path, make_unit, rebuilt: _Rebuilt
) -> None:
    from tolokaforge.core.llm.fallback_client import FallbackLLMClient

    node = _node(tmp_path)
    agent = _AGENT.model_copy(update={"fallbacks": [_FALLBACK]})
    harness = make_unit(node, orchestrator=_CARRIED, agent=agent)

    result = harness.execute()

    assert result.trajectory.status is TrialStatus.COMPLETED
    (inputs,) = node.received
    assert inputs.run_settings.max_turns == 7
    assert inputs.run_settings.agent_loop == "custom-loop"
    (ctx,) = rebuilt.contexts
    orchestrator = ctx.config.orchestrator
    assert (orchestrator.timeouts.turn_s, orchestrator.timeouts.episode_s) == (45, 900)
    assert orchestrator.max_turns == 7
    assert orchestrator.max_trial_tokens == 12345
    assert orchestrator.stuck_heuristics.max_repeated_tool_calls == 3
    assert orchestrator.agent_loop == "custom-loop"
    assert isinstance(ctx.agent_client, FallbackLLMClient)
    assert [model.name for model in ctx.agent_client.chain] == ["gpt-4", "gpt-4o-mini"]


def test_a_run_without_fallbacks_builds_a_bare_client_on_the_node(
    tmp_path: Path, make_unit, rebuilt: _Rebuilt
) -> None:
    from tolokaforge.core.llm import LLMClient

    make_unit(_node(tmp_path)).execute()

    (ctx,) = rebuilt.contexts
    assert type(ctx.agent_client) is LLMClient


def test_a_secret_reference_only_a_fallback_carries_is_caught_by_the_node_preflight(
    tmp_path: Path, make_unit
) -> None:
    node = _node(tmp_path)
    fallback = _FALLBACK.model_copy(
        update={"extra_headers": {"x-key": "${secret:FALLBACK_ONLY_KEY}"}}
    )
    agent = _AGENT.model_copy(update={"fallbacks": [fallback]})
    harness = make_unit(node, agent=agent)

    result = harness.execute()

    assert node.received == []
    assert result.trajectory.termination_reason is TerminationReason.PROVISION_ERROR
    reason = _failure_bundle(harness)["error_reason"]
    assert reason.startswith("SecretPreflight: ")
    assert "${secret:FALLBACK_ONLY_KEY}" in reason
