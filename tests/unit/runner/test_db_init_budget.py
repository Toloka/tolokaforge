"""A trial's DB init gets the whole-state budget, not the HTTP client's default.

``RegisterTrial`` sends the trial's whole initial state to the database service in
one ``POST /trials/{id}/init``. For a large world that takes as long to send and
load as a whole-state read takes to answer, and the client's 30 s default cut a
registration off a second past it, with an empty message (an ``httpx`` timeout's
``str`` is ``""``). These tests pin the per-request budget on the client, the
budget ``RegisterTrial`` hands it, and an error that names its type.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from tests.utils.runner_requests import register_request, trial_spec_json
from tolokaforge.runner.db_client import DBServiceClient
from tolokaforge.runner.service import RunnerServiceImpl
from tolokaforge.runner.state_read_limits import StateReadDeadlines

pytestmark = pytest.mark.unit

_INIT_ANSWER = {
    "status": "initialized",
    "trial_id": "world:0",
    "tables_initialized": ["records"],
    "schemas_registered": 0,
    "unstable_fields_registered": 0,
    "initial_hash": "0" * 64,
}


class _RecordingClient:
    """An injected test client that records each POST's keyword arguments."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.posts.append((url, kwargs))
        return httpx.Response(200, json=_INIT_ANSWER, request=httpx.Request("POST", url))


def _world_task() -> dict[str, Any]:
    return {
        "task_id": "world",
        "name": "world",
        "category": "test",
        "description": "a task whose database holds its world",
        "adapter_type": "native",
        "system_prompt": "",
        "initial_state": {"tables": {"records": [{"id": 1}]}},
    }


class TestClient:
    async def test_init_sends_the_budget_it_is_given(self) -> None:
        client = DBServiceClient("http://db.test:8000", timeout=30.0)
        recorder = _RecordingClient()
        client.set_test_client(recorder)

        await client.init_trial("world:0", tables={"records": [{"id": 1}]}, timeout=300.0)

        ((url, kwargs),) = recorder.posts
        assert url == "/trials/world:0/init"
        sent = kwargs["timeout"]
        assert isinstance(sent, httpx.Timeout)
        # The wait for the answer is the request's own; reaching the service is not slower.
        assert sent.read == 300.0 and sent.connect == 30.0

    async def test_init_without_a_budget_keeps_the_clients_default(self) -> None:
        client = DBServiceClient("http://db.test:8000", timeout=30.0)
        recorder = _RecordingClient()
        client.set_test_client(recorder)

        await client.init_trial("world:0", tables={"records": []})

        ((_, kwargs),) = recorder.posts
        assert kwargs["timeout"] == 30.0


class TestRegisterTrial:
    def test_the_init_gets_the_whole_state_read_budget(self, mock_grpc_context: Any) -> None:
        db_client = MagicMock()
        db_client.init_trial = AsyncMock(side_effect=RuntimeError("stop after the init"))
        service = RunnerServiceImpl(db_client, state_read_timeout_s=300.0)
        try:
            service.RegisterTrial(
                register_request(trial_spec_json(_world_task(), trial_id="world:0"), "world:0"),
                mock_grpc_context,
            )
            (call,) = db_client.init_trial.await_args_list
            budget = StateReadDeadlines(holder_read_s=300.0).final_state_read_s
            # Cut to what the call has left before it unwinds, and far above the
            # client's 30 s default.
            assert 30.0 < call.kwargs["timeout"] <= budget
        finally:
            service.shutdown()

    def test_the_wait_for_the_init_is_the_whole_state_call_budget(
        self, mock_grpc_context: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The wait outlasts the request: a ``state_read_timeout_s`` of 300 makes the
        request's budget longer than ``_run_async``'s 300 s default."""
        db_client = MagicMock()
        db_client.init_trial = AsyncMock(side_effect=RuntimeError("stop after the init"))
        service = RunnerServiceImpl(db_client, state_read_timeout_s=300.0)
        waits: list[float] = []
        run_async = service._run_async

        def recording(coro: Any, timeout: float = 300.0) -> Any:
            waits.append(timeout)
            return run_async(coro, timeout=timeout)

        monkeypatch.setattr(service, "_run_async", recording)
        try:
            service.RegisterTrial(
                register_request(trial_spec_json(_world_task(), trial_id="world:0"), "world:0"),
                mock_grpc_context,
            )
            budget = StateReadDeadlines(holder_read_s=300.0).final_state_read_s
            assert waits[0] == budget > 300.0
            assert db_client.init_trial.await_args.kwargs["timeout"] < budget
        finally:
            service.shutdown()

    def test_a_timed_out_init_names_its_type(self, mock_grpc_context: Any) -> None:
        db_client = MagicMock()
        db_client.init_trial = AsyncMock(side_effect=httpx.ReadTimeout(""))
        service = RunnerServiceImpl(db_client)
        try:
            response = service.RegisterTrial(
                register_request(trial_spec_json(_world_task(), trial_id="world:0"), "world:0"),
                mock_grpc_context,
            )
            assert response.success is False
            assert response.error == "DB Service initialization failed: ReadTimeout"
        finally:
            service.shutdown()

    def test_an_init_error_keeps_its_message_after_its_type(self, mock_grpc_context: Any) -> None:
        db_client = MagicMock()
        db_client.init_trial = AsyncMock(side_effect=ConnectionError("refused"))
        service = RunnerServiceImpl(db_client)
        try:
            response = service.RegisterTrial(
                register_request(trial_spec_json(_world_task(), trial_id="world:0"), "world:0"),
                mock_grpc_context,
            )
            assert response.error == "DB Service initialization failed: ConnectionError: refused"
        finally:
            service.shutdown()
