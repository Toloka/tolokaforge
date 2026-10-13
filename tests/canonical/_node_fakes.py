"""An in-process trial node for tests that need a node without a network or Docker.

:class:`InProcessNode` is a real :class:`TrialNodeServiceImpl` whose trial body
is the real ``run_trial_from_inputs`` pointed at the ``in_memory`` runtime and
conductor, so the service's version gate, one-trial lock, secret pre-flight and
error mapping all run for real. :class:`InProcessNodeClient` stands in for the
gRPC client: it hands each request straight to the service and records what the
control plane did to it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.canonical._factories import write_yaml_file
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import TaskConfig
from tolokaforge.core.trial import TrialNodeInputs, TrialResult
from tolokaforge.core.trial_node import run_trial_from_inputs
from tolokaforge.trial_node import trial_node_pb2
from tolokaforge.trial_node.protocol import NODE_PROTOCOL_VERSION, NodeTrialOutcome
from tolokaforge.trial_node.service import RunTrialFn, TrialNodeServiceImpl


def run_in_memory(inputs: TrialNodeInputs) -> TrialResult:
    """Run the trial the way a node would, against the in-memory substrate."""
    return run_trial_from_inputs(
        inputs.model_copy(update={"runtime": "in_memory", "conductor": "in_memory"})
    )


class InProcessNode:
    """A trial node service reachable through :class:`InProcessNodeClient`."""

    def __init__(
        self,
        output_root: Path,
        *,
        run_trial: RunTrialFn = run_in_memory,
        max_message_mb: int = 50,
    ) -> None:
        self.received: list[TrialNodeInputs] = []

        def recording(inputs: TrialNodeInputs) -> TrialResult:
            self.received.append(inputs)
            return run_trial(inputs)

        self.service = TrialNodeServiceImpl(
            logger=get_logger("in-process-node", strict=False),
            max_message_mb=max_message_mb,
            output_root=output_root,
            run_trial=recording,
        )


class InProcessNodeClient:
    """A node client that calls a service object directly instead of over gRPC."""

    def __init__(
        self,
        address: str,
        max_message_mb: int,
        node: InProcessNode,
        *,
        client_protocol_version: int = NODE_PROTOCOL_VERSION,
    ) -> None:
        self.address = address
        self.max_message_mb = max_message_mb
        self.node = node
        self.client_protocol_version = client_protocol_version
        self.connect_calls: list[dict[str, float]] = []
        self.requests: list[trial_node_pb2.RunTrialRequest] = []
        self.closed = False

    def connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None:
        self.connect_calls.append({"timeout": timeout, "retry_interval": retry_interval})

    def run_trial(self, trial_node_inputs_json: str) -> NodeTrialOutcome:
        request = trial_node_pb2.RunTrialRequest(
            trial_node_inputs_json=trial_node_inputs_json,
            client_protocol_version=self.client_protocol_version,
        )
        self.requests.append(request)
        response = self.node.service.RunTrial(request, None)
        return NodeTrialOutcome(
            success=response.success,
            trial_result_json=response.trial_result_json,
            error_type=response.error_type,
            stage=response.stage,
            reason=response.reason,
            node_protocol_version=response.node_protocol_version,
        )

    def close(self) -> None:
        self.closed = True


class InProcessNodeClientFactory:
    """A ``NodeClientFactory`` that builds :class:`InProcessNodeClient`s over one node."""

    def __init__(self, node: InProcessNode, **client_kwargs: Any) -> None:
        self.node = node
        self.client_kwargs = client_kwargs
        self.clients: list[InProcessNodeClient] = []

    def __call__(self, address: str, max_message_mb: int) -> InProcessNodeClient:
        client = InProcessNodeClient(address, max_message_mb, self.node, **self.client_kwargs)
        self.clients.append(client)
        return client


def write_native_pack(root: Path, *, task_id: str = "flat") -> TaskConfig:
    """Write a one-task native pack under *root* and load its :class:`TaskConfig`."""
    task_dir = root / "tasks" / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "initial_state.json").write_text('{"notes": []}')
    write_yaml_file(
        task_dir / "task.yaml",
        {
            "task_id": task_id,
            "name": task_id,
            "category": "tool_use",
            "description": task_id,
            "interaction_mode": "agent_only",
            "initial_user_message": "add a note",
            "initial_state": {"json_db": "initial_state.json"},
            "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
            "grading": "grading.yaml",
        },
    )
    write_yaml_file(
        task_dir / "grading.yaml",
        {
            "combine": {"weights": {"state_checks": 1.0}, "pass_threshold": 1.0},
            "state_checks": {"jsonpaths": [{"path": "$.db.notes", "equals": []}]},
        },
    )
    return NativeAdapter({"base_dir": str(root), "tasks_glob": "tasks/**/task.yaml"}).get_task(
        task_id
    )
