"""Wire v2 for the queue-backed grader transport.

The queue path is the fourth registered ``TrialGrader`` (``queue``): the
producer packs a :class:`GradeJob`, publishes it, and blocks on a future.
:func:`_queue_worker_loop` pulls the job off the broker and forwards its
fields to :meth:`GrpcGraderClient.grade`. This test locks two properties:

- :class:`GradeJob` carries every v2 wire field the producer needs to hand
  the composite dispatcher — mirroring :class:`grader_pb2.GradeRequest`.
- ``_queue_worker_loop`` forwards each field verbatim (no defaulting to
  ``""`` when the producer populated a value, no name transposition on
  the way through the loop).

A ``GradeJob`` field addition matched by a loop that still hard-codes
``""`` would make the wire silently under-populated on the queue path,
so this test invokes the real loop against a stub broker + stub client
and asserts the kwargs the client saw.
"""

from __future__ import annotations

import dataclasses
import threading
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.trial_grader import _queue_worker_loop
from tolokaforge.grader.queue import (
    GradeJob,
    InMemoryGradeBroker,
    new_job_id,
)

pytestmark = pytest.mark.canonical


# -----------------------------------------------------------------------------
# GradeJob shape
# -----------------------------------------------------------------------------


_EXPECTED_JOB_FIELDS = (
    "job_id",
    "trial_id",
    "llm_messages_json",
    "termination_reason",
    "task_config_json",
    "judge_model_config_json",
    "task_description_json",
    "runner_substrate_address",
)


def test_grade_job_carries_every_wire_field() -> None:
    """A :class:`GradeJob` gains no field it does not publish and drops none
    the wire carries. A silent addition would make the loop's forwarding
    step under-populate; a silent drop would erase v2 context on the queue
    path."""
    got = tuple(f.name for f in dataclasses.fields(GradeJob))
    assert (
        got == _EXPECTED_JOB_FIELDS
    ), f"GradeJob field shape drifted. Expected: {_EXPECTED_JOB_FIELDS!r}. Got: {got!r}."


# -----------------------------------------------------------------------------
# _queue_worker_loop forwards every field to client.grade
# -----------------------------------------------------------------------------


class _CapturingClient:
    """Stand-in for :class:`GrpcGraderClient` that records the kwargs its
    :meth:`grade` was called with, so the loop's forwarding step is
    inspectable field-by-field. Returns a canned success response so the
    loop reaches the ``publish_result`` branch."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.closed = False

    def grade(self, **kwargs: object) -> dict:
        self.calls.append(kwargs)
        return {"success": True, "grade": None, "no_verdict": True, "error": None}

    def close(self) -> None:
        self.closed = True


def _run_loop_once(broker: InMemoryGradeBroker, client: _CapturingClient) -> None:
    """Start the loop on a daemon thread, publish one job, wait for the
    result to land, then close the broker so the loop exits on
    :class:`BrokerClosed`. The thread is joined before the caller inspects
    ``client.calls`` so no order-of-operations hazard hides a missed
    field."""
    thread = threading.Thread(
        target=_queue_worker_loop, args=(broker, client, MagicMock()), daemon=True
    )
    thread.start()
    try:
        job = GradeJob(
            job_id=new_job_id(),
            trial_id="task_id:0",
            llm_messages_json='[{"role":"user","content":"hi"}]',
            termination_reason="agent_done",
            task_config_json='{"llm_judge":{"criteria":[]}}',
            judge_model_config_json='{"provider":"litellm","name":"gpt-4"}',
            task_description_json='{"id":"task_id","tool_artifacts":{}}',
            runner_substrate_address="runner:50051",
        )
        future = broker.publish_job(job)
        assert future.result(timeout=2) is None  # no_verdict → grade=None
    finally:
        broker.close()
        thread.join(timeout=2)
        assert not thread.is_alive(), "queue worker loop failed to exit on broker close"


def test_queue_worker_forwards_every_field_verbatim_to_client_grade() -> None:
    broker = InMemoryGradeBroker()
    client = _CapturingClient()
    _run_loop_once(broker, client)

    assert len(client.calls) == 1
    kwargs = client.calls[0]
    assert kwargs == {
        "trial_id": "task_id:0",
        "llm_messages_json": '[{"role":"user","content":"hi"}]',
        "termination_reason": "agent_done",
        "task_config_json": '{"llm_judge":{"criteria":[]}}',
        "judge_model_config_json": '{"provider":"litellm","name":"gpt-4"}',
        "task_description_json": '{"id":"task_id","tool_artifacts":{}}',
        "runner_substrate_address": "runner:50051",
    }


def test_queue_worker_forwards_empty_defaults_verbatim() -> None:
    """A job the producer packs with empty-string defaults reaches
    :meth:`client.grade` as empty strings — not omitted kwargs and not
    Python ``None``. Composite dispatch's fail-loud missing-field check
    fires on the empty string; converting them to ``None`` here would
    make that check unreachable.
    """
    broker = InMemoryGradeBroker()
    client = _CapturingClient()

    thread = threading.Thread(
        target=_queue_worker_loop, args=(broker, client, MagicMock()), daemon=True
    )
    thread.start()
    try:
        job = GradeJob(
            job_id=new_job_id(),
            trial_id="task_id:0",
            llm_messages_json="[]",
            termination_reason="",
            task_config_json="",
            judge_model_config_json="",
            task_description_json="",
            runner_substrate_address="",
        )
        future = broker.publish_job(job)
        assert future.result(timeout=2) is None
    finally:
        broker.close()
        thread.join(timeout=2)

    assert client.calls[-1] == {
        "trial_id": "task_id:0",
        "llm_messages_json": "[]",
        "termination_reason": "",
        "task_config_json": "",
        "judge_model_config_json": "",
        "task_description_json": "",
        "runner_substrate_address": "",
    }
