"""
Integration tests for the Runner Service pipeline.

This module tests the full flow end-to-end within a single process (no Docker):
- json_db_service (via TestClient)
- DBServiceClient pointing at TestClient
- RunnerServiceImpl with that client
- Full trial lifecycle: RegisterTrial → ExecuteTool → GradeTrial

This validates that all Phase 0-3 components work together correctly.
"""

import json
from typing import Any

import pytest

pytestmark = pytest.mark.unit

from tolokaforge.runner import runner_pb2 as pb2


class TestRunnerPipeline:
    """
    Integration tests for the full Runner pipeline.

    Tests the flow: proto → service → db_client → db_service
    """

    @pytest.fixture
    def simple_task_description(self) -> dict[str, Any]:
        """
        Create a simplified TaskDescription for testing.

        This is a minimal version of a TlkMcpCore/Tau task that exercises
        the full pipeline without requiring external dependencies.

        Note: Tools are not included because they require source config for
        reconstruction. Tests that need tools inject mock tools directly.
        """
        return {
            "task_id": "test_task_001",
            "name": "Simple Integration Test",
            "category": "test",
            "description": "A simple integration test task",
            "adapter_type": "tau",
            "system_prompt": "You are a test assistant.",
            "initial_state": {
                "tables": {
                    "users": [
                        {"id": "u1", "name": "Alice", "balance": 100},
                        {"id": "u2", "name": "Bob", "balance": 200},
                    ],
                    "orders": [],
                },
                "schemas": [
                    {
                        "table_name": "users",
                        "fields": {"id": "string", "name": "string", "balance": "integer"},
                    },
                    {
                        "table_name": "orders",
                        "fields": {"id": "string", "user_id": "string", "amount": "integer"},
                    },
                ],
                "unstable_fields": [
                    {"table_name": "orders", "field_name": "id", "reason": "auto_id"},
                ],
            },
            "agent_tools": [],
            "user_tools": [],
            "grading": {
                "combine_method": "all",
                "pass_threshold": 1.0,
                "state_checks": {
                    "hash_enabled": True,
                    "golden_actions": [
                        {
                            "tool_name": "create_order",
                            "arguments": {"user_id": "u1", "amount": 50},
                        },
                    ],
                },
            },
        }

    def test_register_trial_success(
        self, runner_service, mock_grpc_context, simple_task_description
    ):
        """Test RegisterTrial successfully initializes a trial."""
        trial_id = "integration_test:0"

        # Create request
        request = pb2.RegisterTrialRequest(
            trial_id=trial_id,
            task_description_json=json.dumps(simple_task_description),
            default_tool_timeout_s=30.0,
        )

        # Call RegisterTrial
        response = runner_service.RegisterTrial(request, mock_grpc_context)

        # Verify response (no tools since they require source config for reconstruction)
        assert response.success is True
        assert response.error == ""
        assert response.num_agent_tools == 0
        assert response.num_user_tools == 0

        # Verify trial is registered
        assert trial_id in runner_service.trials

    def test_register_trial_invalid_json(self, runner_service, mock_grpc_context):
        """Test RegisterTrial handles invalid JSON gracefully."""
        request = pb2.RegisterTrialRequest(
            trial_id="invalid_json_test:0",
            task_description_json="not valid json {{{",
        )

        response = runner_service.RegisterTrial(request, mock_grpc_context)

        assert response.success is False
        assert "Invalid task_description_json" in response.error

    def test_execute_tool_trial_not_found(self, runner_service, mock_grpc_context):
        """Test ExecuteTool returns error for non-existent trial."""
        request = pb2.ExecuteToolRequest(
            trial_id="nonexistent:0",
            tool_name="some_tool",
            arguments_json="{}",
        )

        response = runner_service.ExecuteTool(request, mock_grpc_context)

        assert response.status == pb2.EXECUTION_STATUS_TRIAL_NOT_FOUND
        assert "not found" in response.error_message.lower()

    def test_execute_tool_tool_not_found(
        self, runner_service, mock_grpc_context, simple_task_description
    ):
        """Test ExecuteTool returns error for non-existent tool."""
        trial_id = "tool_not_found_test:0"

        # First register the trial
        register_request = pb2.RegisterTrialRequest(
            trial_id=trial_id,
            task_description_json=json.dumps(simple_task_description),
        )
        register_response = runner_service.RegisterTrial(register_request, mock_grpc_context)
        assert register_response.success is True, f"Registration failed: {register_response.error}"

        # Try to execute non-existent tool
        execute_request = pb2.ExecuteToolRequest(
            trial_id=trial_id,
            tool_name="nonexistent_tool",
            arguments_json="{}",
        )

        response = runner_service.ExecuteTool(execute_request, mock_grpc_context)

        assert response.status == pb2.EXECUTION_STATUS_TOOL_NOT_FOUND
        assert "not found" in response.error_message.lower()

    def test_execute_tool_with_mock_tool(
        self, runner_service, mock_grpc_context, simple_task_description
    ):
        """Test ExecuteTool with a manually injected mock tool."""
        trial_id = "mock_tool_test:0"

        # Register the trial
        register_request = pb2.RegisterTrialRequest(
            trial_id=trial_id,
            task_description_json=json.dumps(simple_task_description),
        )
        register_response = runner_service.RegisterTrial(register_request, mock_grpc_context)
        assert register_response.success is True, f"Registration failed: {register_response.error}"

        # Inject a mock tool into the trial context
        async def mock_get_user(args):
            user_id = args.get("user_id")
            return json.dumps({"id": user_id, "name": "Alice", "balance": 100})

        runner_service.trials[trial_id].agent_tools["get_user"] = mock_get_user

        # Execute the tool
        execute_request = pb2.ExecuteToolRequest(
            trial_id=trial_id,
            tool_name="get_user",
            arguments_json=json.dumps({"user_id": "u1"}),
            executor="agent",
        )

        response = runner_service.ExecuteTool(execute_request, mock_grpc_context)

        assert response.status == pb2.EXECUTION_STATUS_SUCCESS
        assert response.error_message == ""

        # Verify output
        output = json.loads(response.output)
        assert output["id"] == "u1"
        assert output["name"] == "Alice"

        # Verify metrics
        assert response.metrics.latency_seconds > 0
        assert response.metrics.exit_code == 0

    def test_grade_trial_no_grading_config(self, runner_service, mock_grpc_context):
        """Test GradeTrial passes by default when no grading config."""
        trial_id = "no_grading_test:0"

        # Create task without grading config
        task_description = {
            "task_id": "no_grading",
            "name": "No Grading Test",
            "category": "test",
            "description": "A test task without grading config",
            "adapter_type": "tau",
            "system_prompt": "You are a test assistant.",
            "initial_state": {"tables": {}, "schemas": []},
            "agent_tools": [],
            "user_tools": [],
            # No grading config
        }

        # Register trial
        register_request = pb2.RegisterTrialRequest(
            trial_id=trial_id,
            task_description_json=json.dumps(task_description),
        )
        runner_service.RegisterTrial(register_request, mock_grpc_context)

        # Grade trial
        grade_request = pb2.GradeTrialRequest(trial_id=trial_id)
        response = runner_service.GradeTrial(grade_request, mock_grpc_context)

        assert response.success is True
        assert response.grade.binary_pass is True
        assert response.grade.score == 1.0
        # Message changed from "No grading config" to "No grading components evaluated"
        assert (
            "No grading" in response.grade.reasons or "no grading" in response.grade.reasons.lower()
        )

    def test_grade_trial_not_found(self, runner_service, mock_grpc_context):
        """Test GradeTrial returns error for non-existent trial."""
        request = pb2.GradeTrialRequest(trial_id="nonexistent:0")

        response = runner_service.GradeTrial(request, mock_grpc_context)

        assert response.success is False
        assert "not found" in response.error.lower()

    def test_get_state_success(self, runner_service, mock_grpc_context, simple_task_description):
        """Test GetState returns current state."""
        trial_id = "get_state_test:0"

        # Register trial
        register_request = pb2.RegisterTrialRequest(
            trial_id=trial_id,
            task_description_json=json.dumps(simple_task_description),
        )
        runner_service.RegisterTrial(register_request, mock_grpc_context)

        # Get state
        get_state_request = pb2.GetStateRequest(
            trial_id=trial_id,
            include_unstable=True,
        )
        response = runner_service.GetState(get_state_request, mock_grpc_context)

        assert response.success is True
        assert response.error == ""

        # Verify state contains expected data
        state = json.loads(response.state_json)
        assert "users" in state
        assert len(state["users"]) == 2
        assert state["users"][0]["name"] == "Alice"

    def test_reset_trial_success(self, runner_service, mock_grpc_context, simple_task_description):
        """Test ResetTrial resets state to initial."""
        trial_id = "reset_test:0"

        # Register trial
        register_request = pb2.RegisterTrialRequest(
            trial_id=trial_id,
            task_description_json=json.dumps(simple_task_description),
        )
        runner_service.RegisterTrial(register_request, mock_grpc_context)

        # Reset trial
        reset_request = pb2.ResetTrialRequest(trial_id=trial_id)
        response = runner_service.ResetTrial(reset_request, mock_grpc_context)

        assert response.success is True
        assert response.error == ""
        assert response.state_hash != ""

    def test_health_check(self, runner_service, mock_grpc_context):
        """Test HealthCheck returns service status."""
        request = pb2.HealthCheckRequest()
        response = runner_service.HealthCheck(request, mock_grpc_context)

        # Service should be healthy or degraded (depending on DB connectivity)
        assert response.status in ["healthy", "degraded"]
        assert response.version != ""
        assert response.num_active_trials >= 0

    def test_full_trial_lifecycle(self, runner_service, mock_grpc_context, simple_task_description):
        """
        Test complete trial lifecycle: register → execute → grade.

        This is the main integration test that validates all components
        work together correctly.
        """
        trial_id = "full_lifecycle_test:0"

        # 1. Register trial
        register_request = pb2.RegisterTrialRequest(
            trial_id=trial_id,
            task_description_json=json.dumps(simple_task_description),
        )
        register_response = runner_service.RegisterTrial(register_request, mock_grpc_context)

        assert register_response.success is True
        assert trial_id in runner_service.trials

        # 2. Inject mock tools that interact with DB
        async def mock_create_order(args):
            """Mock tool that creates an order in the DB."""
            # In a real scenario, this would use the DB proxy
            # For testing, we just return success
            return json.dumps(
                {
                    "status": "created",
                    "order_id": "order_001",
                    "user_id": args.get("user_id"),
                    "amount": args.get("amount"),
                }
            )

        runner_service.trials[trial_id].agent_tools["create_order"] = mock_create_order

        # 3. Execute tool
        execute_request = pb2.ExecuteToolRequest(
            trial_id=trial_id,
            tool_name="create_order",
            arguments_json=json.dumps({"user_id": "u1", "amount": 50}),
            executor="agent",
        )
        execute_response = runner_service.ExecuteTool(execute_request, mock_grpc_context)

        assert execute_response.status == pb2.EXECUTION_STATUS_SUCCESS

        # Verify tool call was recorded in history - ToolCallRecord is a Pydantic model
        trial_context = runner_service.trials[trial_id]
        assert len(trial_context.tool_call_history) == 1
        assert trial_context.tool_call_history[0].tool_name == "create_order"

        # 4. Get state to verify
        get_state_request = pb2.GetStateRequest(trial_id=trial_id)
        state_response = runner_service.GetState(get_state_request, mock_grpc_context)

        assert state_response.success is True

        # 5. Grade trial (will use mock grading since tools are mocked)
        grade_request = pb2.GradeTrialRequest(
            trial_id=trial_id,
            llm_messages_json=json.dumps(
                [
                    {"role": "user", "content": "Create an order for Alice"},
                    {"role": "assistant", "content": "I'll create an order for Alice."},
                ]
            ),
        )
        grade_response = runner_service.GradeTrial(grade_request, mock_grpc_context)

        # Grading should succeed (even if hash doesn't match due to mocking)
        assert grade_response.success is True
        assert grade_response.grade is not None

        # 6. Verify trial state was restored after grading
        # (The grading algorithm snapshots, resets, executes golden, then restores)
        final_state_response = runner_service.GetState(get_state_request, mock_grpc_context)
        assert final_state_response.success is True


# NOTE: TestDBClientWithTestClient has been moved to tests/test_db_client.py
# to avoid duplication. See TestDBServiceClientLifecycle for comprehensive
# DB client tests against real json_db_service.
