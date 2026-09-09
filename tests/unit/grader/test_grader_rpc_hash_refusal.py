"""Hash-family refusals — client-side pre-flight and runner-side broken-replay.

Hash grading has two failure surfaces the runner and the grader-side transports
must both surface fail-loud rather than by fabricating a score:

- **Client-side pre-flight** — ``grader_rpc`` and ``queue`` cannot back
  hash grading (the ``LiveRunnerCallbackGradingSubstrate`` the grader-side
  dispatcher builds is read-only), so :class:`GraderRPCTrialGrader` and
  :class:`QueueTrialGrader` refuse a hash-enabled trial at the client before
  any gRPC round-trip. The error names the operator's actionable branch
  (``grader: runner_rpc`` or ``hash_enabled: false``).

- **Runner-side broken replay** — ``runner_rpc`` executes hash grading over
  the runner's substrate, and a per-action failure during golden replay leaves
  ``golden_replay.failures`` non-empty. :attr:`HashGradingResult.hash_unscorable`
  reads ``True``, the runner call site keeps ``components.hash_score`` at the
  ``-1.0`` not-evaluated sentinel, and the fold's declared-but-unscored refusal
  fires downstream: ``GradeTrial`` returns ``success=False`` naming the missing
  ``state_checks`` component.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.canonical._factories import make_task_description, make_trajectory, make_trial_spec
from tests.utils.runner_requests import (
    register_request,
    simple_task_description,
    trial_spec_json,
)
from tolokaforge.core.grading.golden_replay import (
    FailedGoldenAction,
    GoldenActionFailure,
    GoldenReplayRecord,
)
from tolokaforge.core.models import TrialStatus
from tolokaforge.core.trial_grader import (
    GraderRPCTrialGrader,
    GradingFailedError,
    QueueTrialGrader,
)
from tolokaforge.grader.queue import InMemoryGradeBroker
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.models import (
    HashComparisonBasis,
    HashGradingResult,
    RunnerGradingConfig,
    RunnerStateChecksConfig,
)

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


class TestRunnerRPCGoldenReplayRefusal:
    """The runner's own ``GradeTrial`` refuses a hash-enabled trial whose golden replay
    left the trial's state hashable against a world no author asked for.

    A per-action failure during replay populates ``golden_replay.failures``;
    :attr:`HashGradingResult.hash_unscorable` reads ``True`` and the runner keeps
    ``components.hash_score`` at the ``-1.0`` not-evaluated sentinel. The fold's
    declared-but-unscored refusal fires downstream because ``state_checks`` is in
    the config's requested set but the component slot is empty — ``GradeTrial``
    returns ``success=False`` naming the missing component.
    """

    def test_hash_enabled_task_refuses_when_golden_replay_errors(
        self, runner_service, mock_grpc_context
    ) -> None:
        trial_id = "hash_replay_errors:0"

        registration = register_request(
            trial_spec_json(simple_task_description(), trial_id=trial_id),
            trial_id=trial_id,
        )
        register_response = runner_service.RegisterTrial(registration, mock_grpc_context)
        assert register_response.success is True

        broken_result = HashGradingResult(
            hash_match=False,
            basis=HashComparisonBasis.GOLDEN_REPLAY,
            golden_replay=GoldenReplayRecord(
                authored=1,
                failures=(
                    FailedGoldenAction(
                        index=0,
                        name="create_order",
                        kind=GoldenActionFailure.RAISED,
                        error="RuntimeError: substrate lost mid-replay",
                    ),
                ),
            ),
        )

        async def _stubbed_hash_grading(
            _trial_id: str,
            _trial_context: Any,
            _state_checks: Any,
        ) -> HashGradingResult:
            return broken_result

        runner_service._execute_hash_grading = _stubbed_hash_grading  # type: ignore[method-assign]

        grade_request = pb2.GradeTrialRequest(
            trial_id=trial_id,
            llm_messages_json=json.dumps(
                [{"role": "assistant", "content": "attempted to create the order"}]
            ),
        )
        response = runner_service.GradeTrial(grade_request, mock_grpc_context)

        error = response.error
        grade = response.grade
        assert response.success is False, f"broken replay must refuse; got grade={grade}"
        assert "state_checks" in error, f"refusal must name the component: {error!r}"
