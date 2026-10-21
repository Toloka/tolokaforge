"""The trial-node client and service over a real loopback gRPC channel.

A real server hosts :class:`TrialNodeServiceImpl` (the trial body is the real
``run_trial_from_inputs`` over the ``in_memory`` substrate) and a real
:class:`TrialNodeClient` dials it, so the wire contract, the connect budget, the
skew pre-flight and the one-trial lock are all exercised on the transport they
run on in production. No Docker, no LLM.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from concurrent import futures
from pathlib import Path

import grpc
import pytest

from tests.canonical._factories import make_env_endpoints
from tests.canonical._node_fakes import run_in_memory, write_native_pack
from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.trial import TrialNodeInputs, TrialResult, TrialSpec
from tolokaforge.core.trial_node import build_single_task_adapter
from tolokaforge.runner.grpc_limits import (
    KEEPALIVE_TIME_MS,
    channel_options,
    keepalive_options,
)
from tolokaforge.trial_node import trial_node_pb2, trial_node_pb2_grpc
from tolokaforge.trial_node.client import TrialNodeClient
from tolokaforge.trial_node.protocol import (
    NODE_PROTOCOL_VERSION,
    NodeErrorType,
    NodeRefusedError,
    NodeTransientError,
)
from tolokaforge.trial_node.service import RunTrialFn, TrialNodeServiceImpl

pytestmark = [pytest.mark.canonical, pytest.mark.usefixtures("env_backed_secrets")]

_AGENT = ModelConfig(provider="openai", name="gpt-4")
_USER = ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6")


class _Server:
    def __init__(
        self, tmp_path: Path, *, max_message_mb: int = 50, run_trial: RunTrialFn = run_in_memory
    ):
        self.service = TrialNodeServiceImpl(
            logger=get_logger("wire-node", strict=False),
            max_message_mb=max_message_mb,
            output_root=tmp_path / "node-out",
            run_trial=run_trial,
        )
        self.server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=4),
            options=channel_options(max_message_mb) + keepalive_options(),
        )
        trial_node_pb2_grpc.add_TrialNodeServiceServicer_to_server(self.service, self.server)
        self.address = f"127.0.0.1:{self.server.add_insecure_port('127.0.0.1:0')}"
        self.server.start()

    def stop(self) -> None:
        self.server.stop(grace=0).wait()


@pytest.fixture
def serve(tmp_path: Path) -> Iterator:
    servers: list[_Server] = []

    def start(**kwargs) -> _Server:
        server = _Server(tmp_path, **kwargs)
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.stop()


def _inputs(tmp_path: Path) -> TrialNodeInputs:
    task = write_native_pack(tmp_path / "pack")
    spec = TrialSpec(
        trial_id="flat:0",
        run_id="wire-run",
        task=build_single_task_adapter(task).to_task_description("flat"),
        agent_model_config=_AGENT,
        user_model_config=_USER,
        env_endpoints=make_env_endpoints(),
    )
    return TrialNodeInputs(
        task=task,
        trial_spec=spec,
        grpc_max_message_mb=50,
        runtime="auto",
        conductor="in_process",
    )


def test_a_trial_runs_to_a_graded_result_over_the_wire(serve, tmp_path: Path) -> None:
    server = serve()

    with TrialNodeClient(server.address) as client:
        client.connect(timeout=5.0, retry_interval=0.05)
        outcome = client.run_trial(_inputs(tmp_path).model_dump_json())

    assert outcome.success
    result = TrialResult.model_validate_json(outcome.trial_result_json)
    assert result.trial_id == "flat:0"
    assert result.trajectory.grade is not None
    assert outcome.node_protocol_version == NODE_PROTOCOL_VERSION


def test_a_node_failure_comes_back_as_a_structured_outcome(serve, tmp_path: Path) -> None:
    def refuse(inputs: TrialNodeInputs) -> TrialResult:
        raise NotImplementedError("adapter is not node-runnable")

    server = serve(run_trial=refuse)

    with TrialNodeClient(server.address) as client:
        client.connect(timeout=5.0, retry_interval=0.05)
        outcome = client.run_trial(_inputs(tmp_path).model_dump_json())

    assert not outcome.success
    assert (outcome.error_type, outcome.stage) == (NodeErrorType.NOT_IMPLEMENTED, "adapter")
    assert "node-runnable" in outcome.reason


def test_a_second_trial_on_a_busy_node_is_refused_as_busy(serve, tmp_path: Path) -> None:
    started, release = threading.Event(), threading.Event()

    def slow(inputs: TrialNodeInputs) -> TrialResult:
        started.set()
        assert release.wait(timeout=10)
        return run_in_memory(inputs)

    server = serve(run_trial=slow)
    payload = _inputs(tmp_path).model_dump_json()
    first_outcome: list = []

    def run_first() -> None:
        with TrialNodeClient(server.address) as client:
            client.connect(timeout=5.0, retry_interval=0.05)
            first_outcome.append(client.run_trial(payload))

    thread = threading.Thread(target=run_first)
    thread.start()
    try:
        assert started.wait(timeout=10)
        with TrialNodeClient(server.address) as second:
            second.connect(timeout=5.0, retry_interval=0.05)
            busy = second.run_trial(payload)
            health = second._connected_stub().HealthCheck(trial_node_pb2.HealthCheckRequest())
    finally:
        release.set()
        thread.join(timeout=10)

    assert (busy.success, busy.error_type) == (False, NodeErrorType.NODE_BUSY)
    assert (health.status, health.trial_in_flight) == ("busy", True)
    assert first_outcome[0].success


def test_connect_reports_the_node_unavailable_once_the_budget_is_spent() -> None:
    client = TrialNodeClient("127.0.0.1:1")

    started = time.monotonic()
    with pytest.raises(NodeTransientError, match="unavailable"):
        client.connect(timeout=0.5, retry_interval=0.05)

    assert time.monotonic() - started < 5.0
    client.close()


def test_a_node_that_lacks_the_trial_node_api_is_a_version_skew(tmp_path: Path) -> None:
    bare = grpc.server(futures.ThreadPoolExecutor(max_workers=1))
    port = bare.add_insecure_port("127.0.0.1:0")
    bare.start()
    try:
        client = TrialNodeClient(f"127.0.0.1:{port}")
        with pytest.raises(NodeRefusedError, match="Upgrade the node image") as raised:
            client.connect(timeout=2.0, retry_interval=0.05)
        client.close()
    finally:
        bare.stop(grace=0).wait()

    assert raised.value.error_type == NodeErrorType.VERSION_SKEW


def test_a_node_reporting_an_older_protocol_is_refused_at_connect(
    serve, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = serve()
    monkeypatch.setattr("tolokaforge.trial_node.service.NODE_PROTOCOL_VERSION", 0)

    with TrialNodeClient(server.address) as client:
        with pytest.raises(NodeRefusedError) as raised:
            client.connect(timeout=2.0, retry_interval=0.05)

    assert raised.value.error_type == NodeErrorType.VERSION_SKEW
    assert "speaks trial-node protocol 0" in raised.value.reason


def test_a_node_with_a_smaller_message_bound_is_refused_at_connect(serve) -> None:
    server = serve(max_message_mb=4)

    with TrialNodeClient(server.address, max_message_mb=50) as client:
        with pytest.raises(NodeRefusedError) as raised:
            client.connect(timeout=2.0, retry_interval=0.05)

    assert raised.value.error_type == NodeErrorType.MESSAGE_SIZE_SKEW
    assert "TOLOKAFORGE_GRPC_MAX_MESSAGE_MB" in raised.value.reason


def test_an_oversized_trial_is_refused_with_the_message_size_remedy(serve, tmp_path: Path) -> None:
    server = serve(max_message_mb=4)
    oversized = _inputs(tmp_path).model_dump_json() + " " * (5 * 1024 * 1024)

    with TrialNodeClient(server.address, max_message_mb=4) as client:
        client.connect(timeout=2.0, retry_interval=0.05)
        with pytest.raises(NodeRefusedError) as raised:
            client.run_trial(oversized)

    assert raised.value.error_type == NodeErrorType.MESSAGE_SIZE_SKEW
    assert "compute.grpc_max_message_mb" in raised.value.reason


def test_running_before_connecting_fails_loud() -> None:
    with pytest.raises(RuntimeError, match="not connected"):
        TrialNodeClient("127.0.0.1:1").run_trial("{}")


def test_the_keepalive_options_ping_while_a_single_call_runs() -> None:
    options = dict(keepalive_options())

    assert options["grpc.keepalive_time_ms"] == KEEPALIVE_TIME_MS == 20_000
    assert options["grpc.keepalive_timeout_ms"] == 10_000
    assert options["grpc.keepalive_permit_without_calls"] == 1
    assert options["grpc.http2.max_pings_without_data"] == 0
    assert options["grpc.http2.min_ping_interval_without_data_ms"] <= KEEPALIVE_TIME_MS


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.parametrize("via_cli", [False, True], ids=["python-m", "tolokaforge-cli"])
def test_the_node_process_entry_serves_the_trial_node_api(tmp_path: Path, via_cli: bool) -> None:
    port = _free_port()
    command = (
        [str(Path(sys.executable).parent / "tolokaforge"), "trial-node"]
        if via_cli
        else [sys.executable, "-m", "tolokaforge.trial_node"]
    )
    process = subprocess.Popen(
        [*command, "--port", str(port), "--output-root", str(tmp_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with TrialNodeClient(f"127.0.0.1:{port}") as client:
            client.connect(timeout=60.0, retry_interval=0.25)
            health = client._connected_stub().HealthCheck(trial_node_pb2.HealthCheckRequest())
    finally:
        process.terminate()
        process.wait(timeout=15)

    assert (health.status, health.node_protocol_version) == ("ready", NODE_PROTOCOL_VERSION)
    assert health.node_max_message_mb == 50
