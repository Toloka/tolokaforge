"""The gRPC message bound on real loopback channels.

A real ``grpc.server`` built with :func:`channel_options` serves a minimal
runner servicer; the production :class:`GrpcRunnerClient` and
:class:`LiveRunnerCallbackGradingSubstrate` dial it over TCP. Nothing on the
transport is stubbed, so these tests show what a large task world meets in
production:

* a state above gRPC's 4 MiB default but under the configured bound arrives
  whole (gRPC's own default would cap the receive at 4 MiB);
* a message above the bound fails with ``RESOURCE_EXHAUSTED``, and the
  client's error names the engine's limit and ``compute.grpc_max_message_mb``
  while keeping each call's failure shape.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from concurrent import futures
from contextlib import contextmanager

import grpc
import pytest

from tolokaforge.core.grading.substrate import SubstrateUnreachableError
from tolokaforge.core.grading.substrate_live import LiveRunnerCallbackGradingSubstrate
from tolokaforge.core.shared_stack_runtime import GrpcRunnerClient
from tolokaforge.runner import runner_pb2, runner_pb2_grpc
from tolokaforge.runner.grpc_limits import MAX_MESSAGE_ENV_VAR, channel_options
from tolokaforge.runner.protocol import ENGINE_PROTOCOL_VERSION

pytestmark = pytest.mark.unit

_MIB = 1024 * 1024


def _state_json(size_mib: int) -> str:
    """A JSON world of roughly ``size_mib`` MiB."""
    return json.dumps({"records": "x" * (size_mib * _MIB)})


class _WorldRunner(runner_pb2_grpc.RunnerServiceServicer):
    """Serves a fixed world and records the size of every registration."""

    def __init__(self, state_json: str) -> None:
        self.state_json = state_json
        self.registered_spec_bytes: list[int] = []

    def RegisterTrial(self, request, context):  # noqa: N802 — gRPC method name
        self.registered_spec_bytes.append(len(request.trial_spec_json))
        return runner_pb2.RegisterTrialResponse(
            success=True, runner_protocol_version=ENGINE_PROTOCOL_VERSION
        )

    def GetState(self, request, context):  # noqa: N802 — gRPC method name
        return runner_pb2.GetStateResponse(
            success=True, state_json=self.state_json, stable_hash="h", full_hash="h"
        )

    def HealthCheck(self, request, context):  # noqa: N802 — gRPC method name
        return runner_pb2.HealthCheckResponse(status="healthy")


class _WorldSubstrate(runner_pb2_grpc.SubstrateServiceServicer):
    def __init__(self, state_json: str) -> None:
        self.state_json = state_json

    def ReadFinalDBState(self, request, context):  # noqa: N802 — gRPC method name
        return runner_pb2.ReadStateResponse(state_json=self.state_json)


@contextmanager
def _serve(max_message_mb: int, *, runner=None, substrate=None) -> Iterator[str]:
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=2), options=channel_options(max_message_mb)
    )
    if runner is not None:
        runner_pb2_grpc.add_RunnerServiceServicer_to_server(runner, server)
    if substrate is not None:
        runner_pb2_grpc.add_SubstrateServiceServicer_to_server(substrate, server)
    port = server.add_insecure_port("localhost:0")
    server.start()
    try:
        yield f"localhost:{port}"
    finally:
        server.stop(grace=None)


@contextmanager
def _client(address: str, max_message_mb: int) -> Iterator[GrpcRunnerClient]:
    client = GrpcRunnerClient(address, max_message_mb=max_message_mb)
    client.connect(timeout=10.0, retry_interval=0.05)
    try:
        yield client
    finally:
        client.close()


def _assert_names_limit_and_setting(error: str, operation: str, max_message_mb: int) -> None:
    assert error.startswith(f"{operation} failed: the message exceeds the gRPC message size limit")
    assert f"The engine's limit is {max_message_mb} MiB" in error
    assert "compute.grpc_max_message_mb" in error
    assert MAX_MESSAGE_ENV_VAR in error


class TestGetState:
    def test_state_above_grpc_default_and_under_the_bound_arrives_whole(self) -> None:
        world = _state_json(6)
        with (
            _serve(16, runner=_WorldRunner(world)) as address,
            _client(address, 16) as client,
        ):
            result = client.get_state("task:0")

        assert result["success"] is True
        assert result["error"] is None
        assert result["state_json"] == world

    def test_state_above_the_bound_fails_with_resource_exhausted(self) -> None:
        """The raw call fails with ``RESOURCE_EXHAUSTED``; ``get_state``
        keeps its failure dict and names the limit and the setting."""
        with (
            _serve(32, runner=_WorldRunner(_state_json(10))) as address,
            _client(address, 8) as client,
        ):
            assert client.stub is not None
            with pytest.raises(grpc.RpcError) as raw:
                client.stub.GetState(runner_pb2.GetStateRequest(trial_id="task:0"))
            result = client.get_state("task:0")

        assert raw.value.code() is grpc.StatusCode.RESOURCE_EXHAUSTED
        assert result == {
            "success": False,
            "error": result["error"],
            "state_json": None,
            "stable_hash": None,
            "full_hash": None,
        }
        _assert_names_limit_and_setting(result["error"], "GetState", 8)


class TestRegisterTrial:
    def test_spec_above_grpc_default_and_under_the_bound_registers(self) -> None:
        runner = _WorldRunner("{}")
        spec_json = _state_json(6)
        with _serve(16, runner=runner) as address, _client(address, 16) as client:
            result = client.register_trial("task:0", spec_json)

        assert result["success"] is True
        assert runner.registered_spec_bytes == [len(spec_json)]

    def test_spec_above_the_shared_bound_is_refused_before_sending(self) -> None:
        runner = _WorldRunner("{}")
        with _serve(8, runner=runner) as address, _client(address, 8) as client:
            result = client.register_trial("task:0", _state_json(10))

        assert result["success"] is False
        assert result["tool_schemas"] == []
        assert runner.registered_spec_bytes == []
        _assert_names_limit_and_setting(result["error"], "RegisterTrial", 8)

    def test_spec_above_the_runner_bound_is_refused_by_the_runner(self) -> None:
        """A runner serving with a smaller bound (a task compose that pins
        its own value) refuses the registration; the gRPC detail in the
        error carries the runner's figure next to the engine's."""
        runner = _WorldRunner("{}")
        with _serve(8, runner=runner) as address, _client(address, 32) as client:
            assert client.stub is not None
            with pytest.raises(grpc.RpcError) as raw:
                client.stub.RegisterTrial(
                    runner_pb2.RegisterTrialRequest(
                        trial_id="task:0", trial_spec_json=_state_json(10)
                    )
                )
            result = client.register_trial("task:0", _state_json(10))

        assert raw.value.code() is grpc.StatusCode.RESOURCE_EXHAUSTED
        assert result["success"] is False
        assert runner.registered_spec_bytes == []
        _assert_names_limit_and_setting(result["error"], "RegisterTrial", 32)


class TestGraderSubstrateChannel:
    """The grader reads the final state through its own substrate channel."""

    def test_bound_from_env_admits_a_large_final_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Constructed with the registered two-argument shape (as the grader
        container does), the channel takes its bound from the env."""
        monkeypatch.setenv(MAX_MESSAGE_ENV_VAR, "16")
        world = {"records": "x" * (6 * _MIB)}
        with _serve(16, substrate=_WorldSubstrate(json.dumps(world))) as address:
            substrate = LiveRunnerCallbackGradingSubstrate(address, "task:0")
            try:
                assert substrate.final_state() == world
            finally:
                substrate.close()

    def test_explicit_bound_refuses_a_larger_final_state(self) -> None:
        with _serve(32, substrate=_WorldSubstrate(_state_json(10))) as address:
            substrate = LiveRunnerCallbackGradingSubstrate(address, "task:0", max_message_mb=8)
            try:
                with pytest.raises(SubstrateUnreachableError) as exc:
                    substrate.final_state()
            finally:
                substrate.close()

        assert "RESOURCE_EXHAUSTED" in str(exc.value)
