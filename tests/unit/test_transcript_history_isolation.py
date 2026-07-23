"""Transcript grading must not observe grader-owned golden replay calls."""

import json

import pytest

from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.models import HashGradingResult, ToolCallRecord

pytestmark = pytest.mark.unit


def test_golden_replay_calls_do_not_satisfy_required_agent_actions(
    runner_service,
    mock_grpc_context,
    monkeypatch,
):
    trial_id = "transcript-history-isolation:0"
    task = {
        "task_id": "transcript-history-isolation",
        "name": "Transcript history isolation",
        "category": "test",
        "description": "Require one agent verification call.",
        "adapter_type": "native",
        "system_prompt": "Verify before acting.",
        "initial_state": {"tables": {}, "schemas": []},
        "agent_tools": [],
        "user_tools": [],
        "grading": {
            "combine_method": "weighted",
            "weights": {"state_checks": 0.5, "transcript_rules": 0.5},
            "pass_threshold": 1.0,
            "state_checks": {
                "hash_enabled": True,
                "golden_actions": [],
            },
            "transcript_rules": {
                "required_actions": [
                    {
                        "action_id": "verify_timecard",
                        "requestor": "assistant",
                        "tool_name": "get_timecard",
                        "arguments": {"employee_id": "EMP-1"},
                        "compare_args": ["employee_id"],
                    }
                ]
            },
        },
    }
    registered = runner_service.RegisterTrial(
        pb2.RegisterTrialRequest(
            trial_id=trial_id,
            task_description_json=json.dumps(task),
        ),
        mock_grpc_context,
    )
    assert registered.success

    async def polluted_hash_grading(_trial_id, trial_context, _golden_actions):
        trial_context.tool_call_history.append(
            ToolCallRecord(
                tool_name="get_timecard",
                arguments={"employee_id": "EMP-1"},
                executor="agent",
                output='{"outcome":"success"}',
                status="success",
                latency_seconds=0.01,
                timestamp="2026-07-23T00:00:00Z",
            )
        )
        return HashGradingResult(hash_match=True, hash_score=1.0)

    monkeypatch.setattr(
        runner_service,
        "_execute_hash_grading",
        polluted_hash_grading,
    )

    response = runner_service.GradeTrial(
        pb2.GradeTrialRequest(
            trial_id=trial_id,
            llm_messages_json=json.dumps(
                [{"role": "assistant", "content": "I skipped verification."}]
            ),
        ),
        mock_grpc_context,
    )

    assert response.success
    assert response.grade.components.state_checks == 1.0
    assert response.grade.components.transcript_rules == 0.0
    assert response.grade.score == 0.5
    assert response.grade.binary_pass is False
