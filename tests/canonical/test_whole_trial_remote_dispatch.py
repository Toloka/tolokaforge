"""``--runtime remote`` ships each whole trial to a leased node and rebuilds a reduced bundle.

An :class:`InProcessNode` — a real :class:`TrialNodeServiceImpl` running the real
``run_trial_from_inputs`` over the ``in_memory`` substrate — stands in for the
gRPC transport. The control plane is built exactly as ``--runtime remote``
builds it (a real ``Orchestrator``, a real ``RemoteRuntimeBackend``, the real
``build_trial_execution_unit``), so these tests pin the routing
``RemoteTrialExecutor`` -> ``backend.run_trial_node`` -> node -> graded
``TrialResult``, the reduced bundle on the control plane, and how each node
failure maps onto the orchestrator's retry classes. No Docker, no LLM.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.canonical._node_fakes import (
    InProcessNode,
    InProcessNodeClientFactory,
    write_native_pack,
)
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
from tolokaforge.core.remote_runtime import RemoteRuntimeBackend
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
from tolokaforge.trial_node.protocol import NodeTransientError

pytestmark = [pytest.mark.canonical, pytest.mark.usefixtures("env_backed_secrets")]

_NODES = ["node-a:50061", "node-b:50061"]
_AGENT = ModelConfig(provider="openai", name="gpt-4")
_USER = ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6")
_MAX_MB = 9


class _NeverRunConductor:
    """A control-plane conductor that fails the test if the control plane runs a trial."""

    def run(self, spec: TrialSpec, task_config: TaskConfig) -> TrialResult:
        raise AssertionError("the control-plane conductor must not run a remote trial")


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


def _remote_backend(factory: InProcessNodeClientFactory) -> RemoteRuntimeBackend:
    return RemoteRuntimeBackend(
        registry=InMemoryNodeRegistry(_NODES, lease_timeout_s=5),
        grpc_max_message_mb=_MAX_MB,
        connect_timeout_s=11.0,
        retry_interval_s=0.5,
        node_client_factory=factory,
    )


def _spec_for(task: TaskConfig, trial_index: int = 0) -> TrialSpec:
    from tests.canonical._factories import make_env_endpoints

    return TrialSpec(
        trial_id=f"{task.task_id}:{trial_index}",
        run_id="remote-run",
        task=build_single_task_adapter(task).to_task_description(task.task_id),
        agent_model_config=_AGENT,
        user_model_config=_USER,
        max_turns=task.max_turns,
        env_endpoints=make_env_endpoints(),
    )


class _Unit:
    """An orchestrator-shaped execution unit over a remote backend and one in-process node."""

    def __init__(self, tmp_path: Path, node: InProcessNode, **client_kwargs: Any) -> None:
        self.tmp_path = tmp_path
        self.task = write_native_pack(tmp_path / "pack")
        self.factory = InProcessNodeClientFactory(node, **client_kwargs)
        self.backend = _remote_backend(self.factory)
        self.out = tmp_path / "out"
        self.control_conductor = _NeverRunConductor()
        self.unit = build_trial_execution_unit(
            TrialExecutionContext(
                adapter=build_single_task_adapter(self.task),
                artifact_writer=FileArtifactWriter(),
                config=_config(self.out),
                logger=get_logger("remote-dispatch", strict=False),
                agent_client=None,  # type: ignore[arg-type] — never reached on the control plane
                runtime_backend=self.backend,
                output_dir=self.out,
                request_limiter=None,
                grader_config=GraderConfig(name="runner_rpc"),
                grpc_max_message_mb=_MAX_MB,
                conductor_factory=lambda _ctx: self.control_conductor,  # type: ignore[return-value]
            )
        )

    def execute(self, trial_index: int = 0) -> TrialResult:
        return self.unit.execute(_spec_for(self.task, trial_index), self.task)

    def close(self) -> None:
        self.unit.close()
        self.backend.close()


@pytest.fixture
def make_unit(tmp_path: Path):
    units: list[_Unit] = []

    def make(node: InProcessNode, **client_kwargs: Any) -> _Unit:
        unit = _Unit(tmp_path, node, **client_kwargs)
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
    assert isinstance(harness.unit.conductor, _NeverRunConductor)
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


def test_the_node_sees_no_secret_values(tmp_path: Path, make_unit) -> None:
    node = _node(tmp_path)
    harness = make_unit(node)

    harness.execute()

    (client,) = harness.factory.clients
    (request,) = client.requests
    assert "sk-openrouter-test" not in request.trial_node_inputs_json


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
    orch = Orchestrator(config, deps=OrchestratorDeps(runtime_backend=_remote_backend(factory)))

    run_dir = orch.run(run_id="remote-run", output_dir=tmp_path / "run")

    assert [inputs.trial_spec.trial_id for inputs in node.received] == ["flat:0", "flat:1"]
    assert sorted(t.status for t in orch.results) == [TrialStatus.COMPLETED] * 2
    assert all(t.grade is not None for t in orch.results)
    bundles = sorted(run_dir.glob("trials/flat/*/grade.yaml"))
    assert len(bundles) == 2


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
        (ValueError("no user simulator"), "InvalidConfig", "no user simulator"),
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

    with pytest.raises(NodeTransientError, match="node blew up"):
        harness.execute()

    _assert_both_nodes_are_free(harness)


def test_an_unreachable_node_raises_so_the_orchestrator_retries(tmp_path: Path, make_unit) -> None:
    harness = make_unit(_node(tmp_path))
    harness.backend.node_client_factory = _unreachable_factory

    with pytest.raises(NodeTransientError, match="unavailable"):
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
        deps=OrchestratorDeps(runtime_backend=_remote_backend(InProcessNodeClientFactory(node))),
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
        deps=OrchestratorDeps(runtime_backend=_remote_backend(InProcessNodeClientFactory(node))),
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
        with pytest.raises(NodeTransientError, match="already running a trial"):
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
