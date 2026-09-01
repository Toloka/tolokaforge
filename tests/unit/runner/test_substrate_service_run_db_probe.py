"""Locks :meth:`SubstrateServicer.RunDbProbe` wire shape.

Drives the servicer's ``RunDbProbe`` handler with a scripted
:func:`~tolokaforge.core.grading.db_probes._fetch_probe_rows` and decodes
``rows_json`` through :class:`GrpcSubstrateClient` — the same round-trip the
independent-grader substrate reaches through in production. Asserts a
``datetime`` scalar lands as ``str(dt)`` byte-identical with the in-process
leg (see ``tests/unit/grading/test_in_process_substrate_db_probe.py``); asserts
a servicer-side exception surfaces as a gRPC ``INTERNAL`` status which the
client wraps as :class:`SubstrateUnreachableError`.
"""

from __future__ import annotations

import datetime as _dt
from concurrent import futures

import grpc
import pytest

from tolokaforge.core.grading import db_probes as db_probes_module
from tolokaforge.core.grading.substrate import SubstrateUnreachableError
from tolokaforge.core.grading.substrate_client import GrpcSubstrateClient
from tolokaforge.runner import (
    add_SubstrateServiceServicer_to_server,
)
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner import runner_pb2_grpc as pb2_grpc
from tolokaforge.runner.service import RunnerServiceImpl
from tolokaforge.runner.substrate_service import SubstrateServicer

pytestmark = pytest.mark.unit


class _FakeDBClient:
    async def close(self) -> None:
        return None


def _running_servicer():
    runner = RunnerServiceImpl(db_client=_FakeDBClient())  # type: ignore[arg-type]
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    add_SubstrateServiceServicer_to_server(SubstrateServicer(runner), server)
    port = server.add_insecure_port("[::]:0")
    server.start()
    return runner, server, port


def _stop(runner: RunnerServiceImpl, server: grpc.Server) -> None:
    server.stop(grace=None)
    if runner._loop.is_running():
        runner._loop.call_soon_threadsafe(runner._loop.stop)


def test_datetime_scalar_coerces_to_string_over_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripted_dt = _dt.datetime(2026, 1, 1, 12, 34, 56)

    async def fake_fetch(dsn: str, query: str) -> list[dict[str, object]]:  # noqa: ARG001
        return [{"a": "x", "b": 1, "c": scripted_dt}]

    monkeypatch.setattr(db_probes_module, "_fetch_probe_rows", fake_fetch)

    runner, server, port = _running_servicer()
    try:
        with grpc.insecure_channel(f"localhost:{port}") as channel:
            client = GrpcSubstrateClient(channel, trial_id="unused")
            rows = client.run_db_probe("postgresql://x", "SELECT ...")
    finally:
        _stop(runner, server)

    assert rows == [{"a": "x", "b": 1, "c": str(scripted_dt)}]


def test_empty_result_set_returns_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch(dsn: str, query: str) -> list[dict[str, object]]:  # noqa: ARG001
        return []

    monkeypatch.setattr(db_probes_module, "_fetch_probe_rows", fake_fetch)

    runner, server, port = _running_servicer()
    try:
        with grpc.insecure_channel(f"localhost:{port}") as channel:
            client = GrpcSubstrateClient(channel, trial_id="unused")
            rows = client.run_db_probe("postgresql://x", "SELECT ...")
    finally:
        _stop(runner, server)

    assert rows == []


def test_servicer_exception_becomes_substrate_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_fetch(dsn: str, query: str) -> list[dict[str, object]]:  # noqa: ARG001
        raise ConnectionError("could not connect to app-db:5432")

    monkeypatch.setattr(db_probes_module, "_fetch_probe_rows", failing_fetch)

    runner, server, port = _running_servicer()
    try:
        with grpc.insecure_channel(f"localhost:{port}") as channel:
            client = GrpcSubstrateClient(channel, trial_id="unused")
            with pytest.raises(SubstrateUnreachableError):
                client.run_db_probe("postgresql://x", "SELECT ...")
    finally:
        _stop(runner, server)


def test_non_array_payload_is_refused_by_client() -> None:
    class _StubStub:
        def RunDbProbe(  # noqa: ARG002, N802
            self, request: pb2.RunDbProbeRequest
        ) -> pb2.RunDbProbeResponse:
            return pb2.RunDbProbeResponse(rows_json='"not a list"')

    client = GrpcSubstrateClient.__new__(GrpcSubstrateClient)
    client._trial_id = "unused"  # type: ignore[attr-defined]
    client._stub = _StubStub()  # type: ignore[attr-defined]

    with pytest.raises(SubstrateUnreachableError, match="non-array rows_json"):
        client.run_db_probe("postgresql://x", "SELECT ...")


def test_read_only_structural_gate_still_holds() -> None:
    write_prefixes = ("set_", "insert", "update", "write", "delete", "mutate")
    offenders = [
        name
        for name in dir(SubstrateServicer)
        if not name.startswith("_") and any(name.lower().startswith(p) for p in write_prefixes)
    ]
    assert offenders == [], f"read-only invariant broken: {offenders!r}"


def test_generated_base_has_run_db_probe_method() -> None:
    assert hasattr(pb2_grpc.SubstrateServiceServicer, "RunDbProbe")
