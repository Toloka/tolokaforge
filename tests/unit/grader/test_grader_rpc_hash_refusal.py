"""Client-side pre-flight refusal for hash-enabled tasks on ``grader_rpc``.

Hash grading depends on the runner's substrate reset / replay path — the
``LiveRunnerCallbackGradingSubstrate`` the grader-side dispatcher builds
is read-only and cannot back it. Both grader-side transports
(:class:`GraderRPCTrialGrader` and :class:`QueueTrialGrader`) refuse a
hash-enabled trial at the client so the misconfiguration surfaces
without a gRPC round-trip and the error names the operator's
actionable branch (``grader: runner_rpc`` or ``hash_enabled: false``).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tests.canonical._factories import make_task_description, make_trajectory, make_trial_spec
from tolokaforge.core.models import TrialStatus
from tolokaforge.core.trial_grader import (
    GraderRPCTrialGrader,
    GradingFailedError,
    QueueTrialGrader,
)
from tolokaforge.grader.queue import InMemoryGradeBroker
from tolokaforge.runner.models import RunnerGradingConfig, RunnerStateChecksConfig

pytestmark = pytest.mark.unit


def _hash_enabled_spec():
    task = make_task_description()
    grading = RunnerGradingConfig(
        state_checks=RunnerStateChecksConfig(hash_enabled=True),
    )
    task = task.model_copy(update={"grading": grading})
    spec = make_trial_spec()
    return spec.model_copy(update={"task": task})


class _RecordingClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def grade(self, **kwargs: object) -> dict:  # pragma: no cover — must not be called
        self.calls.append(kwargs)
        return {"success": True, "grade": None, "no_verdict": False, "error": None}


class TestGraderRPCHashRefusal:
    def test_hash_enabled_task_raises_grading_failed_before_gRPC(self) -> None:
        client = _RecordingClient()
        grader = GraderRPCTrialGrader(
            grader_address="stub:0",
            logger=MagicMock(),
            runner_substrate_address="runner:50051",
            grader_client=client,  # type: ignore[arg-type]
        )
        with pytest.raises(GradingFailedError) as excinfo:
            grader.grade(
                _hash_enabled_spec(),
                make_trajectory(status=TrialStatus.COMPLETED),
                "sys",
            )
        message = str(excinfo.value)
        assert "hash" in message.lower()
        assert "runner_rpc" in message
        assert "hash_enabled" in message
        assert client.calls == []


class TestQueueGraderHashRefusal:
    def test_hash_enabled_task_raises_grading_failed_before_publish(self) -> None:
        broker = InMemoryGradeBroker()
        grader = QueueTrialGrader(
            broker=broker,
            logger=MagicMock(),
            runner_substrate_address="runner:50051",
            timeout_s=1.0,
        )
        with pytest.raises(GradingFailedError) as excinfo:
            grader.grade(
                _hash_enabled_spec(),
                make_trajectory(status=TrialStatus.COMPLETED),
                "sys",
            )
        assert "hash" in str(excinfo.value).lower()
        # The broker never saw the job — a subsequent ``next_job`` call
        # returns ``None`` on the empty queue rather than blocking on the
        # refused-but-published job.
        assert broker.next_job(timeout=0.01) is None


class TestMissingSubstrateAddressIsRefused:
    """A grader whose ``runner_substrate_address`` is empty / None must
    refuse at first ``.grade()`` — the composite dispatcher cannot build a
    ``LiveRunnerCallbackGradingSubstrate`` without one, and a silent empty
    string would land at the grader as a 30 s gRPC connect hang."""

    def test_grader_rpc_refuses_when_runner_substrate_address_is_empty(self) -> None:
        client = _RecordingClient()
        grader = GraderRPCTrialGrader(
            grader_address="stub:0",
            logger=MagicMock(),
            runner_substrate_address="",
            grader_client=client,  # type: ignore[arg-type]
        )
        with pytest.raises(GradingFailedError, match="runner_substrate_address"):
            grader.grade(
                make_trial_spec(),
                make_trajectory(status=TrialStatus.COMPLETED),
                "sys",
            )
        assert client.calls == []

    def test_queue_refuses_when_runner_substrate_address_is_empty(self) -> None:
        broker = InMemoryGradeBroker()
        grader = QueueTrialGrader(
            broker=broker,
            logger=MagicMock(),
            runner_substrate_address=None,
            timeout_s=1.0,
        )
        with pytest.raises(GradingFailedError, match="runner_substrate_address"):
            grader.grade(
                make_trial_spec(),
                make_trajectory(status=TrialStatus.COMPLETED),
                "sys",
            )
