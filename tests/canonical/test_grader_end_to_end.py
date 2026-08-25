"""End-to-end lock — real gRPC + real :class:`GraderCompositeDispatch` returns a :class:`Grade`.

Boots an in-process gRPC server carrying :class:`GraderServiceImpl` wired to
a real :class:`GraderCompositeDispatch` (with its substrate seam monkeypatched
to a canned stub — no runner needed), makes a real
:meth:`GrpcGraderClient.grade` call over the wire, and asserts the client
receives a :class:`Grade` with populated ``binary_pass`` + ``score`` and
``success=True``.

This is the load-bearing "not ``NotImplementedError`` anymore" lock. A
regression that mounts the old ``_unwired_judge_fn`` (or any stub returning
``NotImplementedError``) surfaces as ``success=False`` here.
"""

from __future__ import annotations

import json
from concurrent import futures
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock

import grpc
import pytest

from tolokaforge.grader import grader_pb2_grpc
from tolokaforge.grader.client import GrpcGraderClient
from tolokaforge.grader.composite_dispatch import GraderCompositeDispatch
from tolokaforge.grader.service import GraderServiceImpl
from tolokaforge.runner.models import (
    RunnerGradingConfig,
    RunnerInitialStateConfig,
    TaskDescription,
    TranscriptRulesConfig,
)

pytestmark = pytest.mark.canonical


_TRIAL_ID = "task:0"
_STATE = {"users": [{"id": "u1", "name": "Alice"}]}


class _StubSubstrate:
    """Canned-snapshot substrate matching ``LiveRunnerCallbackGradingSubstrate``'s
    two-positional-arg constructor. No real gRPC — the composite dispatches
    against the constants below.
    """

    def __init__(self, address: str, trial_id: str) -> None:
        self.address = address
        self.trial_id = trial_id

    def initial_state(self) -> dict[str, Any]:
        return dict(_STATE)

    def final_state(self) -> dict[str, Any]:
        return dict(_STATE)

    def final_state_stable(self) -> dict[str, Any]:
        return dict(_STATE)

    def filesystem_root(self):  # type: ignore[no-untyped-def]
        return None

    def filesystem_state(self) -> dict[str, str] | None:
        return None

    def db_reader(self) -> Any:
        reader = MagicMock()
        reader.get_state = lambda tables=None: dict(_STATE)
        reader.query = lambda jp: {"results": []}
        return reader

    def knowledge_search(self) -> Any:
        return None

    def close(self) -> None:
        return None


@contextmanager
def _running_grader(monkeypatch: pytest.MonkeyPatch):
    """Spin up an in-process gRPC server hosting :class:`GraderServiceImpl`
    wired to a real :class:`GraderCompositeDispatch`. The substrate seam is
    monkeypatched so no runner is required.
    """
    monkeypatch.setattr(
        "tolokaforge.grader.composite_dispatch.load_grading_substrate",
        lambda name: _StubSubstrate,
    )
    dispatcher = GraderCompositeDispatch(logger=MagicMock())
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    grader_pb2_grpc.add_GraderServiceServicer_to_server(
        GraderServiceImpl(judge_fn=dispatcher.grade, logger=MagicMock()), server
    )
    port = server.add_insecure_port("[::]:0")
    server.start()
    try:
        client = GrpcGraderClient(grader_address=f"localhost:{port}")
        client.connect()
        try:
            yield client
        finally:
            client.close()
    finally:
        server.stop(grace=None)


def _minimal_task() -> TaskDescription:
    grading = RunnerGradingConfig(
        weights={"transcript_rules": 1.0},
        transcript_rules=TranscriptRulesConfig(min_assistant_turns=1),
    )
    return TaskDescription.model_validate(
        {
            "task_id": "e2e",
            "name": "End-to-end",
            "category": "test",
            "description": "grader e2e",
            "adapter_type": "tau",
            "system_prompt": "You are a test assistant.",
            "initial_state": RunnerInitialStateConfig(tables=_STATE).model_dump(),
            "agent_tools": [],
            "user_tools": [],
            "grading": grading.model_dump(),
        }
    )


def test_grade_rpc_returns_real_grade_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real ``GrpcGraderClient.grade`` call reaches
    :class:`GraderCompositeDispatch` over the wire and receives a
    populated :class:`Grade` — no ``NotImplementedError`` masquerade.
    """
    task = _minimal_task()
    llm_messages = [
        {"role": "system", "content": "you are a test assistant"},
        {"role": "user", "content": "please help"},
        {"role": "assistant", "content": "done"},
    ]

    with _running_grader(monkeypatch) as client:
        result = client.grade(
            trial_id=_TRIAL_ID,
            llm_messages_json=json.dumps(llm_messages),
            termination_reason="",
            task_config_json=task.grading.model_dump_json(),
            judge_model_config_json="",
            task_description_json=task.model_dump_json(),
            runner_substrate_address="stub:50051",
        )

    assert result["success"] is True, result.get("error")
    assert result["no_verdict"] is False
    assert result["grade"] is not None
    assert result["grade"]["binary_pass"] is True
    assert result["grade"]["score"] == pytest.approx(1.0)
    assert result["grade"]["components"]["transcript_rules"] == pytest.approx(1.0)


def test_grade_rpc_translates_grading_failed_error_to_success_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grade the dispatch refuses (hash-enabled task) surfaces on the wire
    as ``success=False`` with the dispatch's error text — the same shape a
    raised :class:`NotImplementedError` or :class:`GradingFailedError`
    produces on the wire, so seam consumers see one refusal shape."""
    from tolokaforge.runner.models import RunnerStateChecksConfig

    grading = RunnerGradingConfig(
        weights={"state_checks": 1.0},
        state_checks=RunnerStateChecksConfig(hash_enabled=True),
    )
    task = TaskDescription.model_validate(
        {
            "task_id": "e2e-hash",
            "name": "End-to-end hash refusal",
            "category": "test",
            "description": "hash-enabled",
            "adapter_type": "tau",
            "system_prompt": "You are a test assistant.",
            "initial_state": RunnerInitialStateConfig(tables=_STATE).model_dump(),
            "agent_tools": [],
            "user_tools": [],
            "grading": grading.model_dump(),
        }
    )
    with _running_grader(monkeypatch) as client:
        result = client.grade(
            trial_id=_TRIAL_ID,
            llm_messages_json="[]",
            termination_reason="",
            task_config_json=task.grading.model_dump_json(),
            judge_model_config_json="",
            task_description_json=task.model_dump_json(),
            runner_substrate_address="stub:50051",
        )
    assert result["success"] is False
    assert "hash-based grading" in (result["error"] or "")
