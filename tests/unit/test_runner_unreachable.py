"""A tool call the runner never answers ends the trial as lost, not as the agent's failure.

Every tool of a trial runs through the one runner. When the runner cannot be
reached — gRPC ``UNAVAILABLE``, which is what a channel reports when nothing
answers at the runner's address or the connection under the call breaks — every
later call of the trial fails the same way. Handed back as a tool failure, each
reads to the agent as its own error: on a trial whose agent starved the machine,
``echo hi`` and ``true`` each came back as ``UNAVAILABLE`` after minutes, and the
agent spent the rest of its episode retrying commands that never reached its
container. These tests drive the real ``GrpcRunnerClient`` over real channels:
one to an address nothing listens on, one to an in-process server that answers.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from concurrent import futures

import grpc
import pytest

from tolokaforge.core.loop import classify_loop_error
from tolokaforge.core.models import TerminationReason, TrialStatus
from tolokaforge.core.shared_stack_runtime import GrpcRunnerClient
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner import runner_pb2_grpc
from tolokaforge.runner.protocol import (
    RunnerUnreachableError,
    TrialLostError,
    TrialNotRegisteredError,
)

pytestmark = pytest.mark.unit


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _client_at(address: str) -> GrpcRunnerClient:
    """A client wired to *address* without ``connect``'s wait for a ready channel."""
    client = GrpcRunnerClient(address)
    client.channel = grpc.insecure_channel(address)
    client.stub = runner_pb2_grpc.RunnerServiceStub(client.channel)
    return client


class _AbortingRunner(runner_pb2_grpc.RunnerServiceServicer):
    """A runner that is up and answers every call with a gRPC error of its own."""

    def ExecuteTool(self, request: pb2.ExecuteToolRequest, context: grpc.ServicerContext):
        context.abort(grpc.StatusCode.INTERNAL, "tool bridge failed")


@pytest.fixture
def aborting_runner() -> Iterator[str]:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    runner_pb2_grpc.add_RunnerServiceServicer_to_server(_AbortingRunner(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        yield f"127.0.0.1:{port}"
    finally:
        server.stop(grace=None)


def test_a_call_nobody_answers_raises_instead_of_returning_a_tool_failure() -> None:
    client = _client_at(f"127.0.0.1:{_free_port()}")

    with pytest.raises(RunnerUnreachableError) as raised:
        client.execute_tool("task:0", "bash", {"command": "echo hi"}, call_id="toolu_A")

    assert raised.value.trial_id == "task:0"
    assert raised.value.tool_name == "bash"
    assert "could not be reached" in str(raised.value)
    assert isinstance(raised.value.__cause__, grpc.RpcError)


def test_the_loop_ends_an_unreachable_runner_as_a_lost_trial() -> None:
    client = _client_at(f"127.0.0.1:{_free_port()}")
    with pytest.raises(RunnerUnreachableError) as raised:
        client.execute_tool("task:0", "bash", {"command": "true"}, call_id="toolu_A")

    decision = classify_loop_error(raised.value, ())

    assert decision.reason is TerminationReason.TRIAL_LOST
    assert decision.status is TrialStatus.ERROR


def test_both_ways_of_losing_a_trial_share_one_type() -> None:
    """The loop and the trial runner match ``TrialLostError``, so neither route can
    end one of them as ``trial_lost`` and hand the other back to the agent."""
    assert issubclass(TrialNotRegisteredError, TrialLostError)
    assert issubclass(RunnerUnreachableError, TrialLostError)
    assert (
        classify_loop_error(TrialNotRegisteredError("task:0", "bash"), ()).reason
        is TerminationReason.TRIAL_LOST
    )


def test_a_runner_that_answers_with_an_error_is_still_a_tool_failure(
    aborting_runner: str,
) -> None:
    """Only a runner that cannot be reached loses the trial. One that answers —
    even with an error status of its own — reached the call, and the agent reads
    that outcome as before."""
    result = _client_at(aborting_runner).execute_tool(
        "task:0", "bash", {"command": "true"}, call_id="toolu_A"
    )

    assert result.success is False
    assert "tool bridge failed" in (result.error or "")
