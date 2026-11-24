"""The identity grading runs as survives a real gRPC round-trip.

A protocol bump claims a field crosses the wire; a client asserted against a
mock stub proves only that the client built a message. These drive a live
in-process gRPC server and a real :class:`GrpcSubstrateClient` — the round-trip
an independent grader reaches the runner through in production — so the
``user`` on ``ExecInServiceRequest`` and ``MaterialisePathRequest`` is pinned
through proto serialisation, not through a recorded call.

Both RPCs carry it because grading must not run as one user in process and
another across gRPC: a pack that seals its reward directory against its agent is
denied on exactly the paths it grades by when they disagree. The destination
preparation is the sharper of the two — a copy landing on a half-emptied tree
grades the trial against the wrong filesystem rather than losing an audit trail.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent import futures
from contextlib import contextmanager
from typing import Any

import grpc
import pytest

from tolokaforge.core.grading.substrate_client import GrpcSubstrateClient
from tolokaforge.runner import add_SubstrateServiceServicer_to_server
from tolokaforge.runner.substrate_service import SubstrateServicer

pytestmark = pytest.mark.canonical

_TRIAL_ID = "task:0"


class _RecordingRunner:
    """Stands in for ``RunnerServiceImpl``, recording what each seam was given."""

    def __init__(self) -> None:
        self.trials = {_TRIAL_ID: object()}
        self.exec_calls: list[dict[str, Any]] = []
        self.materialise_calls: list[dict[str, Any]] = []

    def _exec_in_service(
        self,
        *,
        service: str | None,
        command: str,
        timeout_s: float,
        trial_id: str,
        user: str | None = None,
    ) -> tuple[int, str]:
        self.exec_calls.append(
            {
                "service": service,
                "command": command,
                "timeout_s": timeout_s,
                "user": user,
                "trial_id": trial_id,
            }
        )
        return (0, "ok")

    def _materialise_path(
        self,
        *,
        src_service: str | None,
        dst_service: str | None,
        path: str,
        exclude: list[str],
        trial_id: str,
        user: str | None = None,
    ) -> None:
        self.materialise_calls.append(
            {
                "src_service": src_service,
                "dst_service": dst_service,
                "path": path,
                "exclude": exclude,
                "user": user,
                "trial_id": trial_id,
            }
        )


@contextmanager
def _client_over_a_live_channel() -> Iterator[tuple[GrpcSubstrateClient, _RecordingRunner]]:
    """A real client and server either side of a loopback channel."""
    runner = _RecordingRunner()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    add_SubstrateServiceServicer_to_server(SubstrateServicer(runner), server)  # type: ignore[arg-type]
    port = server.add_insecure_port("[::]:0")
    server.start()
    try:
        with grpc.insecure_channel(f"localhost:{port}") as channel:
            yield GrpcSubstrateClient(channel, _TRIAL_ID), runner
    finally:
        server.stop(grace=None)


def test_the_declared_user_crosses_the_wire_on_both_grading_rpcs() -> None:
    with _client_over_a_live_channel() as (client, runner):
        exit_code, output = client.exec_in_service(
            service="verifier", command="cat /logs/verifier/ctrf.json", timeout_s=7.0, user="root"
        )
        client.materialise_path(
            src_service=None,
            dst_service="verifier",
            path="/app/output",
            exclude=["__pycache__"],
            user="root",
        )

    assert (exit_code, output) == (0, "ok")
    assert runner.exec_calls == [
        {
            "service": "verifier",
            "command": "cat /logs/verifier/ctrf.json",
            "timeout_s": 7.0,
            "user": "root",
            "trial_id": _TRIAL_ID,
        }
    ]
    assert runner.materialise_calls == [
        {
            "src_service": None,
            "dst_service": "verifier",
            "path": "/app/output",
            "exclude": ["__pycache__"],
            "user": "root",
            "trial_id": _TRIAL_ID,
        }
    ]


def test_no_declared_user_arrives_as_the_container_default() -> None:
    """proto3 has no null string, so the client's ``None`` has to survive as one."""
    with _client_over_a_live_channel() as (client, runner):
        client.exec_in_service(service=None, command="collect", timeout_s=1.0)
        client.materialise_path(src_service="tests-vault", dst_service=None, path="/tests")

    assert runner.exec_calls[0]["user"] is None
    assert runner.exec_calls[0]["service"] is None
    assert runner.materialise_calls[0]["user"] is None
    assert runner.materialise_calls[0]["dst_service"] is None
