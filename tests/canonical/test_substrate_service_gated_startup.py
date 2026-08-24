"""``SubstrateService`` — proto surface + config-gated startup + read-only invariant.

Locks the four behaviours Stage 1 of issue #1261 commits to:

1. ``RunConfig(grader=GraderConfig(expose_substrate=True))`` round-trips
   through pydantic; the default remains ``False`` so a brownfield
   ``run_config.yaml`` parses unchanged.
2. ``runner/__main__.get_config()`` maps ``RUNNER_EXPOSE_SUBSTRATE`` to
   the boolean the runner container starts with (unset / anything but
   ``"true"`` → ``False``).
3. A runner started with the flag off returns ``UNIMPLEMENTED`` on a
   ``SubstrateService/ReadFinalDBState`` call over an in-process gRPC
   channel; with the flag on the same call returns the trial's DB state
   assembled the same way ``RunnerServiceImpl._assemble_jsonpath_state``
   assembles it today (RAW, mirroring ``db_client.get_state``).
4. The servicer module is structurally read-only: it holds
   ``_READ_ONLY = True`` AND no public method's name matches a write
   verb (``set_`` / ``insert`` / ``update`` / ``write`` / ``delete``
   / ``mutate``).
"""

from __future__ import annotations

import inspect
import json
from concurrent import futures
from contextlib import contextmanager
from typing import Any

import grpc
import pytest

from tolokaforge.core.models.run_config import GraderConfig
from tolokaforge.runner import (
    add_RunnerServiceServicer_to_server,
    add_SubstrateServiceServicer_to_server,
)
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner import runner_pb2_grpc as pb2_grpc
from tolokaforge.runner.__main__ import get_config
from tolokaforge.runner.models import RunnerInitialStateConfig, StateResponse, TaskDescription
from tolokaforge.runner.service import RunnerServiceImpl, TrialContextRuntime
from tolokaforge.runner.substrate_service import SubstrateServicer

pytestmark = pytest.mark.canonical


_TRIAL_ID = "task:0"
_RAW_STATE = {"users": [{"id": "u1", "name": "Alice", "session_token": "S-1"}]}


def _minimal_task_description() -> TaskDescription:
    """A TaskDescription just rich enough for the servicer's ReadInitialState +
    ReadFinalDBState paths to run; no agent/user tools, no grading config."""
    return TaskDescription.model_validate(
        {
            "task_id": "cleanup_e2e",
            "name": "Substrate startup",
            "category": "test",
            "description": "In-process substrate startup gate",
            "adapter_type": "tau",
            "system_prompt": "You are a test assistant.",
            "initial_state": RunnerInitialStateConfig(
                tables={"users": [{"id": "u1", "name": "Alice"}]},
            ).model_dump(),
            "agent_tools": [],
            "user_tools": [],
        }
    )


class _FakeDBServiceClient:
    """Async-shaped DB client stand-in wired only for the endpoints the
    substrate ``ReadFinalDBState`` / ``ReadFinalDBStateStable`` calls hit.

    Mirrors ``DBServiceClient.get_state`` + ``get_stable_state``: both are
    async and return a pydantic ``StateResponse`` / ``StableStateResponse``.
    The runner service's ``_run_async`` bridges these onto its dedicated
    loop, so the test does not need to manage awaits itself.
    """

    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        self._tables = tables

    async def get_state(
        self, trial_id: str, tables: list[str] | None = None
    ) -> StateResponse:  # noqa: ARG002
        return StateResponse(data=self._tables, version=1, full_hash="full", stable_hash="stable")

    async def get_stable_state(self, trial_id: str) -> Any:  # noqa: ARG002
        raise AssertionError(
            "RAW ReadFinalDBState must not reach get_stable_state (parity depends "
            "on this split staying honest)"
        )

    async def health_check(self) -> Any:
        raise AssertionError("SubstrateService test does not exercise health_check")

    async def close(self) -> None:
        return None


@contextmanager
def _running_runner(*, expose_substrate: bool):
    """Bring up an in-process gRPC server carrying ``RunnerService`` and
    (conditionally) ``SubstrateService``, with a fake DB client that
    returns ``_RAW_STATE``. Yields a connected channel plus the underlying
    ``RunnerServiceImpl`` (so a test can pre-register a trial)."""
    fake_db = _FakeDBServiceClient(_RAW_STATE)
    # RunnerServiceImpl expects a real DBServiceClient shape; the fake
    # exposes the only two async methods this test exercises. rag_client
    # stays None because the KB test in stage 2 exercises the KBSearch
    # path; here we only need the DB read.
    runner = RunnerServiceImpl(db_client=fake_db)  # type: ignore[arg-type]
    trial_context = TrialContextRuntime(
        trial_id=_TRIAL_ID, task_description=_minimal_task_description()
    )
    runner.trials[_TRIAL_ID] = trial_context

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    add_RunnerServiceServicer_to_server(runner, server)
    if expose_substrate:
        add_SubstrateServiceServicer_to_server(SubstrateServicer(runner), server)
    port = server.add_insecure_port("[::]:0")
    server.start()
    try:
        with grpc.insecure_channel(f"localhost:{port}") as channel:
            yield channel, runner
    finally:
        server.stop(grace=None)
        # Release the dedicated event-loop thread the runner service owns.
        if runner._loop.is_running():
            runner._loop.call_soon_threadsafe(runner._loop.stop)


class TestConfigRoundTrip:
    """The compatibility surface Stage 1 introduces: one new optional field
    on ``GraderConfig`` with a False default so an existing ``run_config.
    yaml`` parses unchanged."""

    def test_default_is_false(self) -> None:
        assert GraderConfig().expose_substrate is False

    def test_true_round_trips_through_pydantic(self) -> None:
        cfg = GraderConfig(expose_substrate=True)
        assert cfg.expose_substrate is True
        dumped = cfg.model_dump()
        assert dumped["expose_substrate"] is True
        reparsed = GraderConfig.model_validate(dumped)
        assert reparsed.expose_substrate is True

    def test_extra_forbid_still_holds(self) -> None:
        # Regression guard: adding the field must not accidentally relax
        # ``extra="forbid"`` on GraderConfig.
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            GraderConfig.model_validate({"expose_substrate": True, "unknown_field": 1})


class TestEnvVarParsing:
    """The ``RUNNER_EXPOSE_SUBSTRATE`` env var is the sole surface the
    runner container reads. Honest-absence: unset / empty / anything but
    ``"true"`` (case-insensitive) means the surface is off."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, False),
            ("", False),
            ("false", False),
            ("0", False),
            ("true", True),
            ("TRUE", True),
            ("  true  ", True),
        ],
    )
    def test_env_var_maps_to_bool(
        self,
        monkeypatch: pytest.MonkeyPatch,
        value: str | None,
        expected: bool,
    ) -> None:
        if value is None:
            monkeypatch.delenv("RUNNER_EXPOSE_SUBSTRATE", raising=False)
        else:
            monkeypatch.setenv("RUNNER_EXPOSE_SUBSTRATE", value)
        assert get_config()["expose_substrate"] is expected


class TestReadOnlyByConstruction:
    """The servicer's read-only guarantee is structural, not a docstring
    promise. This test walks the public method set on
    :class:`SubstrateServicer` (compared against the generated base
    :class:`SubstrateServiceServicer`) and refuses any name whose prefix
    matches a write verb."""

    _WRITE_PREFIXES = ("set_", "insert", "update", "write", "delete", "mutate")

    def test_read_only_invariant_flag_is_set(self) -> None:
        assert SubstrateServicer._READ_ONLY is True

    def test_no_public_method_names_match_a_write_verb(self) -> None:
        offenders: list[str] = []
        for name, _ in inspect.getmembers(SubstrateServicer, inspect.isfunction):
            if name.startswith("_"):
                continue
            for prefix in self._WRITE_PREFIXES:
                if name.lower().startswith(prefix):
                    offenders.append(name)
        assert not offenders, (
            f"SubstrateServicer public method(s) match a write verb "
            f"({offenders!r}); read-only invariant broken."
        )

    def test_generated_base_carries_only_read_rpcs(self) -> None:
        # The generated servicer base's methods form the RPC surface. Adding
        # a write RPC to runner.proto would surface as a matching method on
        # this base and the same prefix check would catch it before a
        # servicer implementation lands.
        for name, _ in inspect.getmembers(pb2_grpc.SubstrateServiceServicer, inspect.isfunction):
            if name.startswith("_"):
                continue
            for prefix in self._WRITE_PREFIXES:
                assert not name.lower().startswith(prefix), (
                    f"Generated SubstrateServiceServicer carries a write-verb RPC "
                    f"({name!r}); runner.proto has drifted."
                )


class TestGatedStartup:
    """The registration decision the runner's ``__main__`` makes: with the
    flag off, ``SubstrateService/*`` calls return ``UNIMPLEMENTED``; with
    the flag on, the servicer answers with the trial's RAW final DB state
    assembled the same way ``_assemble_jsonpath_state`` assembles it."""

    def test_flag_off_returns_unimplemented(self) -> None:
        with _running_runner(expose_substrate=False) as (channel, _runner):
            stub = pb2_grpc.SubstrateServiceStub(channel)
            with pytest.raises(grpc.RpcError) as exc_info:
                stub.ReadFinalDBState(pb2.ReadFinalDBStateRequest(trial_id=_TRIAL_ID))
            assert exc_info.value.code() == grpc.StatusCode.UNIMPLEMENTED

    def test_flag_on_returns_the_trials_raw_final_db_state(self) -> None:
        with _running_runner(expose_substrate=True) as (channel, _runner):
            stub = pb2_grpc.SubstrateServiceStub(channel)
            response = stub.ReadFinalDBState(pb2.ReadFinalDBStateRequest(trial_id=_TRIAL_ID))
        # The wire shape is a JSON string of ``{table: [rows]}`` — same shape
        # ``GetStateResponse.state_json`` carries and the RAW state the
        # runner's ``_assemble_jsonpath_state`` reads off ``get_state`` today.
        assert response.trial_not_found is False
        assert json.loads(response.state_json) == _RAW_STATE

    def test_flag_on_initial_state_reads_from_task_description(self) -> None:
        # ReadInitialState is a substrate read all four locks depend on
        # (Stage 2's callback substrate will dial it once and cache); lock
        # the shape here to catch a signature drift.
        with _running_runner(expose_substrate=True) as (channel, _runner):
            stub = pb2_grpc.SubstrateServiceStub(channel)
            response = stub.ReadInitialState(pb2.ReadInitialStateRequest(trial_id=_TRIAL_ID))
        assert json.loads(response.state_json) == {"users": [{"id": "u1", "name": "Alice"}]}
