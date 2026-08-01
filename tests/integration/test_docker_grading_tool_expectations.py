"""End-to-end integration tests for ``tool_expectations`` grading over real gRPC.

The flow under test is the whole production wiring, not the evaluator in isolation:

    RegisterTrial (validates TranscriptRulesConfig.tool_expectations on the wire)
        -> ExecuteTool (the runner records the call in tool_call_history)
            -> GradeTrial RPC
                -> grading.evaluate_transcript_rules decomposes tool_expectations
                   into one sub-check per declared tool
                -> grade.reasons names every failing sub-check

``tests/unit/grading/test_grading_correctness.py`` covers the evaluator directly.
These tests prove the key survives adapter-independent wire validation, that the
runner's own tool-call history is the evidence the check reads, and that the grade
the container returns reflects it. They are deterministic: ``calculator`` is a
builtin needing no environment service, and no LLM is involved.
"""

from __future__ import annotations

from typing import Any

import pytest

from tolokaforge.core.models import ModelConfig
from tolokaforge.core.shared_stack_runtime import GrpcRunnerClient
from tolokaforge.core.trial import EnvEndpoints, TrialSpec
from tolokaforge.runner.models import TaskDescription

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]

_CALCULATOR = "calculator"


def _task_description(tool_expectations: dict[str, list[str]]) -> dict[str, Any]:
    """A trial whose only grading component is ``transcript_rules``.

    ``pass_threshold`` sits at 0.99 so a single failing sub-check fails the trial,
    making the expected verdict unambiguous.
    """
    return {
        "task_id": "tool_expectations_e2e",
        "name": "Tool Expectations Grading E2E",
        "category": "test",
        "description": "Grade transcript_rules.tool_expectations through GradeTrial",
        "adapter_type": "native",
        "system_prompt": "You are a test assistant.",
        "initial_state": {"tables": {}, "schemas": [], "unstable_fields": []},
        "agent_tools": [
            {
                "name": _CALCULATOR,
                "description": "Evaluate an arithmetic expression",
                "parameters": {
                    "type": "object",
                    "properties": {"expression": {"type": "string"}},
                    "required": ["expression"],
                },
            }
        ],
        "user_tools": [],
        "grading": {
            "combine_method": "weighted",
            "weights": {"transcript_rules": 1.0},
            "pass_threshold": 0.99,
            "transcript_rules": {"tool_expectations": tool_expectations},
        },
    }


def _trial_spec_json(trial_id: str, tool_expectations: dict[str, list[str]]) -> str:
    """Build a valid ``TrialSpec`` JSON wrapping the tool-expectations task.

    ``RegisterTrial`` validates the full spec, and the runner-side
    ``TranscriptRulesConfig`` is ``extra="forbid"``, so this call is also the
    wire-compatibility check: an older runner image rejects the new key here.
    """
    return TrialSpec(
        trial_id=trial_id,
        run_id="tool_expectations_e2e_run",
        task=TaskDescription.model_validate(_task_description(tool_expectations)),
        agent_model_config=ModelConfig(name="test-model", provider="test"),
        env_endpoints=EnvEndpoints(
            db_url="http://db.test:8000",
            runner_url="http://runner.test:50051",
        ),
    ).model_dump_json()


@pytest.fixture
def runner_client(runner_container) -> GrpcRunnerClient:
    """RunnerClient connected to the testcontainer Runner over gRPC."""
    host = runner_container.get_container_host_ip()
    port = runner_container.get_exposed_port(50051)
    client = GrpcRunnerClient(runner_address=f"{host}:{port}")
    client.connect()
    yield client
    client.close()


def _register_call_and_grade(
    runner_client: GrpcRunnerClient,
    trial_id: str,
    tool_expectations: dict[str, list[str]],
) -> dict[str, Any]:
    """Register, execute ``calculator`` over gRPC, grade, clean up, return the grade."""
    spec_json = _trial_spec_json(trial_id, tool_expectations)
    registered = runner_client.register_trial(trial_id=trial_id, trial_spec_json=spec_json)
    assert registered["success"] is True, registered["error"]
    try:
        executed = runner_client.execute_tool(
            trial_id=trial_id,
            tool_name=_CALCULATOR,
            arguments={"expression": "2 + 2"},
            call_id="toolu_calculator_0",
        )
        assert executed.success is True, executed.error
        assert "4" in executed.output, executed.output
        result = runner_client.grade_trial(trial_id=trial_id)
    finally:
        runner_client.cleanup_trial(trial_id=trial_id)
    assert result["success"] is True, result["error"]
    assert result["grade"] is not None, "grade missing from successful GradeTrial response"
    return result["grade"]


class TestToolExpectationsGradingOverGrpc:
    """``tool_expectations`` produces real grading signal on the production path."""

    def test_calling_a_disallowed_tool_fails_the_trial(
        self, runner_client: GrpcRunnerClient
    ) -> None:
        """A forbidden tool the agent actually called fails the runner-backed grade,
        and the reasons name the tool so the author can act on it."""
        grade = _register_call_and_grade(
            runner_client,
            "tool_expectations_e2e:0",
            {"required_tools": [], "disallowed_tools": [_CALCULATOR]},
        )

        assert grade["binary_pass"] is False
        assert grade["components"]["transcript_rules"] == pytest.approx(0.0)
        reasons = grade["reasons"]
        assert "Disallowed tool" in reasons, reasons
        assert _CALCULATOR in reasons, reasons

    def test_satisfied_expectations_pass_the_trial(self, runner_client: GrpcRunnerClient) -> None:
        """The mirror case: a required tool that was called and a disallowed tool that
        was not must pass, so the failing case above discriminates on the key."""
        grade = _register_call_and_grade(
            runner_client,
            "tool_expectations_e2e:1",
            {"required_tools": [_CALCULATOR], "disallowed_tools": ["delete_customer"]},
        )

        assert grade["binary_pass"] is True
        assert grade["components"]["transcript_rules"] == pytest.approx(1.0)
        assert "all 2 rules passed" in grade["reasons"], grade["reasons"]

    def test_required_tool_that_was_never_called_fails(
        self, runner_client: GrpcRunnerClient
    ) -> None:
        """A required tool absent from a NON-empty history fails and is named —
        proving the sub-check ran rather than transcript grading being skipped."""
        grade = _register_call_and_grade(
            runner_client,
            "tool_expectations_e2e:2",
            {"required_tools": ["write_file"], "disallowed_tools": []},
        )

        assert grade["binary_pass"] is False
        assert grade["components"]["transcript_rules"] == pytest.approx(0.0)
        reasons = grade["reasons"]
        assert "Required tool" in reasons, reasons
        assert "write_file" in reasons, reasons
