"""``GrpcRunnerClient`` instances wired to real gRPC channels, without ``connect``'s wait.

:func:`unreachable_runner_client` points one at a loopback port nothing listens
on, which is what a trial's tool calls meet once its runner stops answering:
every ``ExecuteTool`` fails at the transport with ``UNAVAILABLE``.
"""

from __future__ import annotations

import socket

import grpc

from tolokaforge.core.shared_stack_runtime import GrpcRunnerClient
from tolokaforge.runner import runner_pb2_grpc


def runner_client_at(address: str) -> GrpcRunnerClient:
    """A client on a channel to *address*, built without waiting for it to be ready."""
    client = GrpcRunnerClient(address)
    client.channel = grpc.insecure_channel(address)
    client.stub = runner_pb2_grpc.RunnerServiceStub(client.channel)
    return client


def unreachable_runner_client() -> GrpcRunnerClient:
    """A client whose runner address has nothing listening on it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return runner_client_at(f"127.0.0.1:{port}")
