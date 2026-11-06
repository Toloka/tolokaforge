"""A trial's DB init gets the whole-state budget, not the HTTP client's default.

``RegisterTrial`` sends the trial's whole initial state to the database service in
one ``POST /trials/{id}/init``, which for a large world takes as long to send and
load as a whole-state read takes to answer. An ``httpx`` timeout's ``str`` is ``""``,
so the error names the exception's type. The client side of the budget is pinned in
``test_db_client_request_timeouts.py``.
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


class _RecordingClient:
    """Records each POST's ``timeout``, then ends the registration at its DB init."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.posts.append((url, kwargs.get("timeout")))
        raise RuntimeError("stop after the init")


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


def _register(service: RunnerServiceImpl, context: Any) -> Any:
    return service.RegisterTrial(
        register_request(trial_spec_json(_world_task(), trial_id="world:0"), "world:0"),
        context,
    )


class TestRegisterTrial:
    def test_the_init_request_carries_the_whole_state_budget(self, mock_grpc_context: Any) -> None:
        db_client = DBServiceClient("http://db.test:8000", timeout=30.0)
        recorder = _RecordingClient()
        db_client.set_test_client(recorder)
        service = RunnerServiceImpl(db_client, state_read_timeout_s=300.0)
        try:
            _register(service, mock_grpc_context)
        finally:
            service.shutdown()

        ((url, timeout),) = recorder.posts
        assert url == "/trials/world:0/init"
        assert isinstance(timeout, httpx.Timeout)
        budget = StateReadDeadlines(holder_read_s=300.0).final_state_read_s
        # The whole-state bound, cut to what its call has left.
        assert budget - 11.0 < timeout.read <= budget
        assert timeout.connect == 30.0

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
            _register(service, mock_grpc_context)
            budget = StateReadDeadlines(holder_read_s=300.0).final_state_read_s
            assert waits[0] == budget > 300.0
            assert db_client.init_trial.await_args.kwargs["timeout"] < budget
        finally:
            service.shutdown()

    @pytest.mark.parametrize(
        "timed_out",
        [
            pytest.param(httpx.ReadTimeout(""), id="the_request"),
            pytest.param(TimeoutError(), id="the_wait_for_it"),
        ],
    )
    def test_a_timed_out_init_names_its_type_its_budget_and_the_setting(
        self, mock_grpc_context: Any, timed_out: Exception
    ) -> None:
        db_client = MagicMock()
        db_client.init_trial = AsyncMock(side_effect=timed_out)
        service = RunnerServiceImpl(db_client, state_read_timeout_s=20.0)
        try:
            response = _register(service, mock_grpc_context)
        finally:
            service.shutdown()

        assert response.success is False
        assert response.error == (
            f"DB Service initialization failed: {type(timed_out).__name__} (the init's budget "
            "is 300 s; raise compute.state_read_timeout_s for a world slower than that to load)"
        )

    def test_an_init_error_keeps_its_message_after_its_type(self, mock_grpc_context: Any) -> None:
        db_client = MagicMock()
        db_client.init_trial = AsyncMock(side_effect=ConnectionError("refused"))
        service = RunnerServiceImpl(db_client)
        try:
            response = _register(service, mock_grpc_context)
        finally:
            service.shutdown()

        assert response.error == "DB Service initialization failed: ConnectionError: refused"

    def test_a_failed_init_logs_its_traceback(
        self, mock_grpc_context: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        db_client = MagicMock()
        db_client.init_trial = AsyncMock(side_effect=ValueError("bad table"))
        service = RunnerServiceImpl(db_client)
        try:
            with caplog.at_level("ERROR"):
                _register(service, mock_grpc_context)
        finally:
            service.shutdown()

        # In the formatted text: the secret-redaction filter moves ``exc_info`` to ``exc_text``.
        assert "Traceback (most recent call last)" in caplog.text
