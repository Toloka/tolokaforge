"""
Integration tests for the Runner Service pipeline.

This module tests the full flow end-to-end within a single process (no Docker):
- json_db_service (via TestClient)
- DBServiceClient pointing at TestClient
- RunnerServiceImpl with that client
- Full trial lifecycle: RegisterTrial → ExecuteTool → GradeTrial

This validates that the trial-lifecycle components work together correctly.
"""

import asyncio
import json
import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

from tests.canonical._factories import make_trial_spec
from tests.utils.runner_requests import execute_request, register_request, trial_spec_json
from tests.utils.runner_requests import simple_task_description as simple_task_description_dict
from tests.utils.servicer_runtime import (
    DUPLICATE_CALL_ID,
    ServicerBackend,
    collided_trajectory,
    register_collided_trial,
)
from tolokaforge.core.models import TerminationReason
from tolokaforge.core.trial_grader import GradingFailedError, RunnerRPCTrialGrader
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.protocol import ENGINE_PROTOCOL_VERSION
from tolokaforge.tools.registry import ToolExecutionStatus


@pytest.fixture
def simple_task_description() -> dict[str, Any]:
    return simple_task_description_dict()


async def _create_order(arguments: dict[str, Any]) -> str:
    """The tool the shared task's golden action names, which its wire form cannot carry.

    ``simple_task_description`` declares no tool ``source``, so ``RegisterTrial``
    reconstructs nothing and hash grading has no ``create_order`` to resolve its golden
    action against. Any test that grades that task registers this callable first.
    """
    return json.dumps(
        {
            "status": "created",
            "order_id": "order_001",
            "user_id": arguments.get("user_id"),
            "amount": arguments.get("amount"),
        }
    )


class TestRunnerPipeline:
    """
    Integration tests for the full Runner pipeline.

    Tests the flow: proto → service → db_client → db_service
    """

    def test_register_trial_success(
        self, runner_service, mock_grpc_context, simple_task_description
    ):
        """Test RegisterTrial successfully initializes a trial."""
        trial_id = "integration_test:0"

        # Create request
        request = register_request(
            trial_spec_json(simple_task_description, trial_id=trial_id),
            trial_id=trial_id,
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
        request = register_request("not valid json {{{", trial_id="invalid_json_test:0")

        response = runner_service.RegisterTrial(request, mock_grpc_context)

        assert response.success is False
        assert "Invalid trial_spec_json" in response.error

    def test_execute_tool_trial_not_found(self, runner_service, mock_grpc_context):
        """Test ExecuteTool returns error for non-existent trial."""
        request = execute_request("nonexistent:0", "some_tool")

        response = runner_service.ExecuteTool(request, mock_grpc_context)

        assert response.status == pb2.EXECUTION_STATUS_TRIAL_NOT_FOUND
        assert "not found" in response.error_message.lower()

    def test_execute_tool_tool_not_found(
        self, runner_service, mock_grpc_context, simple_task_description
    ):
        """Test ExecuteTool returns error for non-existent tool."""
        trial_id = "tool_not_found_test:0"

        # First register the trial
        registration = register_request(
            trial_spec_json(simple_task_description, trial_id=trial_id), trial_id=trial_id
        )
        register_response = runner_service.RegisterTrial(registration, mock_grpc_context)
        assert register_response.success is True, f"Registration failed: {register_response.error}"

        # Try to execute non-existent tool
        execution = execute_request(trial_id, "nonexistent_tool")

        response = runner_service.ExecuteTool(execution, mock_grpc_context)

        assert response.status == pb2.EXECUTION_STATUS_TOOL_NOT_FOUND
        assert "not found" in response.error_message.lower()

    def test_execute_tool_with_mock_tool(
        self, runner_service, mock_grpc_context, simple_task_description
    ):
        """Test ExecuteTool with a manually injected mock tool."""
        trial_id = "mock_tool_test:0"

        # Register the trial
        registration = register_request(
            trial_spec_json(simple_task_description, trial_id=trial_id), trial_id=trial_id
        )
        register_response = runner_service.RegisterTrial(registration, mock_grpc_context)
        assert register_response.success is True, f"Registration failed: {register_response.error}"

        # Inject a mock tool into the trial context
        async def mock_get_user(args):
            user_id = args.get("user_id")
            return json.dumps({"id": user_id, "name": "Alice", "balance": 100})

        runner_service.trials[trial_id].agent_tools["get_user"] = mock_get_user

        # Execute the tool
        execution = execute_request(trial_id, "get_user", json.dumps({"user_id": "u1"}))

        response = runner_service.ExecuteTool(execution, mock_grpc_context)

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
        registration = register_request(
            trial_spec_json(task_description, trial_id=trial_id), trial_id=trial_id
        )
        runner_service.RegisterTrial(registration, mock_grpc_context)

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
        registration = register_request(
            trial_spec_json(simple_task_description, trial_id=trial_id), trial_id=trial_id
        )
        runner_service.RegisterTrial(registration, mock_grpc_context)

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
        registration = register_request(
            trial_spec_json(simple_task_description, trial_id=trial_id), trial_id=trial_id
        )
        runner_service.RegisterTrial(registration, mock_grpc_context)

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
        registration = register_request(
            trial_spec_json(simple_task_description, trial_id=trial_id), trial_id=trial_id
        )
        register_response = runner_service.RegisterTrial(registration, mock_grpc_context)

        assert register_response.success is True
        assert trial_id in runner_service.trials

        # 2. Register the tool the task's wire form cannot carry
        runner_service.trials[trial_id].agent_tools["create_order"] = _create_order

        # 3. Execute tool
        execution = execute_request(
            trial_id, "create_order", json.dumps({"user_id": "u1", "amount": 50})
        )
        execute_response = runner_service.ExecuteTool(execution, mock_grpc_context)

        assert execute_response.status == pb2.EXECUTION_STATUS_SUCCESS

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
                    {
                        "role": "assistant",
                        "content": "I'll create an order for Alice.",
                        # The turn that asked for the call executed in step 3. A
                        # transcript that omits it disagrees with the tool-call
                        # record, which grading refuses to reconcile.
                        "tool_calls": [
                            {
                                "id": "call_0",
                                "function": {
                                    "name": "create_order",
                                    "arguments": json.dumps({"user_id": "u1", "amount": 50}),
                                },
                            }
                        ],
                    },
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


class TestRegisterTrialSearchPlanes:
    """RegisterTrial decouples the two search planes.

    ``search.enabled`` and ``search.host`` are independent:

    - ``host`` set  ⇒ TypeSense init runs (the search_policy registry), gated on
      ``host`` ALONE — independent of ``enabled``.
    - ``enabled`` set ⇒ the task declares it needs rag-service; the RAG indexing
      block requires a configured ``rag_client`` and fails loud otherwise.

    Regression context (PR #102): once the core stack stopped setting
    ``RAG_SERVICE_URL`` (rag-env-honesty), ``rag_client is None`` on that stack.
    A TypeSense-only domain used to flip ``enabled=True`` only to get TypeSense
    init, which then hard-failed on the RAG requirement. The fix gates TypeSense
    init on ``host`` alone so such a domain sets ``enabled=False`` and registers.
    """

    @staticmethod
    def _search_task(search: dict[str, Any]) -> dict[str, Any]:
        """A minimal registrable task carrying a ``search`` config block."""
        return {
            "task_id": "search_plane_task",
            "name": "Search Plane Test",
            "category": "test",
            "description": "Exercises the TypeSense/RAG gate decoupling",
            "adapter_type": "tlk_mcp_core",
            "system_prompt": "You are a test assistant.",
            "initial_state": {"tables": {}, "schemas": []},
            "agent_tools": [],
            "user_tools": [],
            "search": search,
        }

    def test_typesense_only_registers_without_rag_client(self, mock_grpc_context, db_client):
        """REGRESSION: a TypeSense-only domain (enabled=False, host set) with NO
        rag_client registers successfully — it must NOT hit the RAG fail-loud.

        mcp_core is absent in this repo, so ``_init_typesense_for_trial`` warns
        and skips; the contract under test is only that registration succeeds.
        """
        from tolokaforge.runner.service import RunnerServiceImpl

        service = RunnerServiceImpl(db_client)  # rag_client defaults to None
        assert service.rag_client is None
        try:
            trial_id = "typesense_only:0"
            task = self._search_task(
                {
                    "enabled": False,
                    "host": "typesense",
                    "port": 8108,
                    "domain_name": "external_retail_v3",
                }
            )
            request = register_request(trial_spec_json(task, trial_id=trial_id), trial_id=trial_id)

            response = service.RegisterTrial(request, mock_grpc_context)

            assert response.success is True, f"Registration failed: {response.error}"
            assert "RAG service not configured" not in response.error
            assert trial_id in service.trials
        finally:
            service.shutdown()

    def test_enabled_without_rag_client_still_fails_loud(self, mock_grpc_context, db_client):
        """FAIL-LOUD preserved: enabled=True with rag_client=None still returns the
        'Search enabled but RAG service not configured' error."""
        from tolokaforge.runner.service import RunnerServiceImpl

        service = RunnerServiceImpl(db_client)  # rag_client defaults to None
        assert service.rag_client is None
        try:
            trial_id = "rag_required:0"
            task = self._search_task(
                {
                    "enabled": True,
                    "host": "typesense",
                    "port": 8108,
                    "domain_name": "external_retail_v3",
                }
            )
            request = register_request(trial_spec_json(task, trial_id=trial_id), trial_id=trial_id)

            response = service.RegisterTrial(request, mock_grpc_context)

            assert response.success is False
            assert "Search enabled but RAG service not configured" in response.error
        finally:
            service.shutdown()

    def test_domain_set_on_db_proxy_regardless_of_enabled(self, mock_grpc_context, db_client):
        """A TypeSense config with a domain_name sets the db-proxy domain even
        with enabled=False (the db-proxy plane is gated on domain_name, NOT
        enabled). Verifies registration succeeds for that path."""
        from tolokaforge.runner.service import RunnerServiceImpl

        service = RunnerServiceImpl(db_client)
        try:
            trial_id = "domain_proxy:0"
            task = self._search_task(
                {
                    "enabled": False,
                    "host": "typesense",
                    "domain_name": "external_retail_v3",
                }
            )
            request = register_request(trial_spec_json(task, trial_id=trial_id), trial_id=trial_id)

            response = service.RegisterTrial(request, mock_grpc_context)

            assert response.success is True, f"Registration failed: {response.error}"
            assert trial_id in service.trials
        finally:
            service.shutdown()


class TestCallIdCrossesTheWire:
    """The provider's tool-call id reaches the runner's recorded history.

    Drives the real ``RegisterTrial`` / ``ExecuteTool`` handlers against the real
    in-process DB service, so what is asserted is the recorded history the
    grader reads — not a stand-in for it.
    """

    @pytest.fixture
    def echo_trial(self, request, runner_service, mock_grpc_context, simple_task_description):
        """A registered trial whose only tool echoes its arguments.

        The DB service backing these handlers is process-global and rejects a
        re-registered trial id, so the id is derived from the test name.
        """
        trial_id = f"{request.node.name}:0"
        registered = runner_service.RegisterTrial(
            register_request(
                trial_spec_json(simple_task_description, trial_id=trial_id), trial_id=trial_id
            ),
            mock_grpc_context,
        )
        assert registered.success is True, registered.error

        async def echo(args):
            return json.dumps(args)

        runner_service.trials[trial_id].agent_tools["echo"] = echo
        return trial_id

    def test_recorded_call_carries_the_request_call_id(
        self, runner_service, mock_grpc_context, echo_trial
    ):
        response = runner_service.ExecuteTool(
            execute_request(echo_trial, "echo", json.dumps({"x": 1}), call_id="toolu_abc"),
            mock_grpc_context,
        )
        assert response.status == pb2.EXECUTION_STATUS_SUCCESS

        history = runner_service.trials[echo_trial].tool_call_history
        assert [(r.call_id, r.sequence) for r in history] == [("toolu_abc", 0)]

    def test_two_identical_calls_are_distinguishable_in_the_record(
        self, runner_service, mock_grpc_context, echo_trial
    ):
        """The case position cannot resolve: same tool, byte-identical arguments."""
        arguments = json.dumps({"payment_id": "PAY-1"})
        for call_id in ("toolu_A", "toolu_B"):
            response = runner_service.ExecuteTool(
                execute_request(echo_trial, "echo", arguments, call_id=call_id),
                mock_grpc_context,
            )
            assert response.status == pb2.EXECUTION_STATUS_SUCCESS

        history = runner_service.trials[echo_trial].tool_call_history
        assert [r.tool_name for r in history] == ["echo", "echo"]
        assert [r.arguments for r in history] == [{"payment_id": "PAY-1"}] * 2
        assert [r.call_id for r in history] == ["toolu_A", "toolu_B"]
        assert [r.sequence for r in history] == [0, 1]

    def test_empty_call_id_raises_rather_than_returning_a_tool_error(
        self, runner_service, mock_grpc_context, echo_trial
    ):
        """A tool-shaped failure would be survivable: the agent would read it as a
        tool error and retry until the turn budget was gone."""
        with pytest.raises(ValueError, match="carries no call_id"):
            runner_service.ExecuteTool(
                execute_request(echo_trial, "echo", call_id=""), mock_grpc_context
            )

    def test_unknown_tool_is_recorded_not_only_reported(
        self, runner_service, mock_grpc_context, echo_trial
    ):
        """The host appends a ``role: tool`` error for a rejected call, so a record
        that omits it reads as a call that was never attempted."""
        response = runner_service.ExecuteTool(
            execute_request(echo_trial, "no_such_tool", call_id="toolu_missing"),
            mock_grpc_context,
        )
        assert response.status == pb2.EXECUTION_STATUS_TOOL_NOT_FOUND

        history = runner_service.trials[echo_trial].tool_call_history
        assert [(r.call_id, r.tool_name, r.status) for r in history] == [
            ("toolu_missing", "no_such_tool", "tool_not_found")
        ]
        assert "not found" in history[0].output

    def test_unparseable_arguments_are_recorded_not_only_reported(
        self, runner_service, mock_grpc_context, echo_trial
    ):
        response = runner_service.ExecuteTool(
            execute_request(echo_trial, "echo", "{not json", call_id="toolu_bad_args"),
            mock_grpc_context,
        )
        assert response.status == pb2.EXECUTION_STATUS_INVALID_ARGUMENTS

        history = runner_service.trials[echo_trial].tool_call_history
        assert [(r.call_id, r.status, r.arguments) for r in history] == [
            ("toolu_bad_args", "invalid_arguments", {})
        ]

    def test_rejected_and_executed_calls_share_one_sequence(
        self, runner_service, mock_grpc_context, echo_trial
    ):
        """Rejections occupy sequence slots, so the index stays the trial-wide
        attempt order rather than the order of the calls that happened to run."""
        runner_service.ExecuteTool(
            execute_request(echo_trial, "no_such_tool", call_id="toolu_0"), mock_grpc_context
        )
        runner_service.ExecuteTool(
            execute_request(echo_trial, "echo", json.dumps({"x": 1}), call_id="toolu_1"),
            mock_grpc_context,
        )

        history = runner_service.trials[echo_trial].tool_call_history
        assert [(r.sequence, r.status) for r in history] == [
            (0, "tool_not_found"),
            (1, "success"),
        ]


class TestRegisterTrialVersionGate:
    """Version skew aborts at registration, before any tokens are spent."""

    @staticmethod
    def _spec_with_retired_user_simulator_key(trial_id: str, task: dict[str, Any]) -> str:
        """A trial spec as a version-1 engine still serialises it.

        Built by editing the JSON rather than the model: the key is exactly what
        this runner's models refuse, so it cannot be constructed through them.
        """
        spec = json.loads(trial_spec_json(task, trial_id=trial_id))
        spec["task"]["user_simulator"] = {
            "mode": "llm",
            "persona": "cooperative",
            "backstory": "",
            "first_message": None,
        }
        return json.dumps(spec)

    def test_engine_emitting_a_retired_trial_spec_key_is_refused_by_the_gate(
        self, runner_service, mock_grpc_context, simple_task_description
    ):
        """Version 1 is the last that emits ``user_simulator.first_message``.

        The gate has to catch that engine, because the alternative refusal — the
        wire model's own — reaches the operator as a validation failure over a key
        their engine will keep sending until the pair is rebuilt.
        """
        trial_id = "retired_key_engine:0"
        response = runner_service.RegisterTrial(
            register_request(
                self._spec_with_retired_user_simulator_key(trial_id, simple_task_description),
                trial_id=trial_id,
                engine_protocol_version=1,
            ),
            mock_grpc_context,
        )

        assert response.success is False
        assert "version-skewed" in response.error
        assert "Invalid trial_spec_json" not in response.error
        assert trial_id not in runner_service.trials

    def test_the_same_payload_from_a_current_engine_is_refused_by_the_wire_model(
        self, runner_service, mock_grpc_context, simple_task_description
    ):
        """The gate's refusal above is not vacuous: the payload really is
        unparseable, and past the gate the model names the key and its remedy."""
        trial_id = "current_engine_retired_key:0"
        response = runner_service.RegisterTrial(
            register_request(
                self._spec_with_retired_user_simulator_key(trial_id, simple_task_description),
                trial_id=trial_id,
            ),
            mock_grpc_context,
        )

        assert response.success is False
        assert "Invalid trial_spec_json" in response.error
        assert "first_message" in response.error
        assert "initial_user_message" in response.error
        assert trial_id not in runner_service.trials

    def test_engine_below_the_required_version_is_refused(
        self, runner_service, mock_grpc_context, simple_task_description
    ):
        trial_id = "skewed_engine:0"
        response = runner_service.RegisterTrial(
            register_request(
                trial_spec_json(simple_task_description, trial_id=trial_id),
                trial_id=trial_id,
                engine_protocol_version=ENGINE_PROTOCOL_VERSION - 1,
            ),
            mock_grpc_context,
        )

        assert response.success is False
        assert "version-skewed" in response.error
        assert str(ENGINE_PROTOCOL_VERSION) in response.error
        assert trial_id not in runner_service.trials

    def test_engine_that_declares_no_version_is_refused(
        self, runner_service, mock_grpc_context, simple_task_description
    ):
        """An engine predating the field sends nothing, which arrives as 0."""
        trial_id = "unversioned_engine:0"
        response = runner_service.RegisterTrial(
            pb2.RegisterTrialRequest(
                trial_id=trial_id,
                trial_spec_json=trial_spec_json(simple_task_description, trial_id=trial_id),
            ),
            mock_grpc_context,
        )

        assert response.success is False
        assert "version-skewed" in response.error
        assert trial_id not in runner_service.trials

    def test_newer_engine_is_accepted(
        self, runner_service, mock_grpc_context, simple_task_description
    ):
        """The gate is a lower bound: a newer engine still sends ``call_id``, so
        refusing it would be a version lock this runner does not need."""
        trial_id = "newer_engine:0"
        response = runner_service.RegisterTrial(
            register_request(
                trial_spec_json(simple_task_description, trial_id=trial_id),
                trial_id=trial_id,
                engine_protocol_version=ENGINE_PROTOCOL_VERSION + 1,
            ),
            mock_grpc_context,
        )

        assert response.success is True, response.error
        assert trial_id in runner_service.trials


class TestGradeTrialTerminationReason:
    """The trial's termination reason crosses to GradeTrial, typed."""

    @pytest.fixture
    def graded_trial(
        self, runner_service, mock_grpc_context, simple_task_description, request
    ) -> str:
        trial_id = f"{request.node.name}:0"
        registered = runner_service.RegisterTrial(
            register_request(
                trial_spec_json(simple_task_description, trial_id=trial_id), trial_id=trial_id
            ),
            mock_grpc_context,
        )
        assert registered.success is True, registered.error
        runner_service.trials[trial_id].agent_tools["create_order"] = _create_order
        return trial_id

    @pytest.mark.parametrize("reason", [r.value for r in TerminationReason])
    def test_every_reason_crosses_the_wire_and_parses_back(
        self, runner_service, mock_grpc_context, graded_trial, reason: str, caplog
    ):
        """The field is typed for the whole enum, not the subset the host
        currently routes to the RPC, and the runner reads back the value it was
        sent rather than some other member of the enum.

        The assertion is on the log line because nothing in grading reads the
        reason yet — #678 is the first consumer. It locks the wire round trip, not
        any grading behaviour; when a check starts reading the reason, assert on
        the verdict instead.
        """
        with caplog.at_level(logging.INFO, logger="tolokaforge.runner.service"):
            response = runner_service.GradeTrial(
                pb2.GradeTrialRequest(trial_id=graded_trial, termination_reason=reason),
                mock_grpc_context,
            )

        assert response.success is True, response.error
        assert response.grade is not None
        assert f"termination_reason={reason}" in caplog.text

    def test_absent_reason_is_accepted(
        self, runner_service, mock_grpc_context, graded_trial, caplog
    ):
        """An engine that reports no reason sends an empty string, which is a
        valid state rather than a skew."""
        with caplog.at_level(logging.INFO, logger="tolokaforge.runner.service"):
            response = runner_service.GradeTrial(
                pb2.GradeTrialRequest(trial_id=graded_trial),
                mock_grpc_context,
            )

        assert response.success is True, response.error
        assert "termination_reason=none" in caplog.text

    def test_unknown_reason_fails_the_rpc_naming_the_accepted_set(
        self, runner_service, mock_grpc_context, graded_trial
    ):
        response = runner_service.GradeTrial(
            pb2.GradeTrialRequest(trial_id=graded_trial, termination_reason="not_a_reason"),
            mock_grpc_context,
        )

        assert response.success is False
        assert "not_a_reason" in response.error
        for reason in TerminationReason:
            assert reason.value in response.error


class TestGradeTrialTimelineReconciliation:
    """GradeTrial joins the transcript to the tool-call record before it scores.

    The trial here is graded *only* on having called ``echo`` successfully, so the
    verdict can come from nowhere but the timeline's records — a substrate that
    dropped them would score 0.0 here rather than pass on some other component.
    """

    @staticmethod
    def _task(simple_task_description: dict[str, Any]) -> dict[str, Any]:
        task = dict(simple_task_description)
        task["grading"] = {
            "combine_method": "all",
            "pass_threshold": 1.0,
            "weights": {"transcript_rules": 1.0},
            "transcript_rules": {"tool_expectations": {"required_tools": ["echo"]}},
        }
        return task

    _CALLED_ECHO = {
        "role": "assistant",
        "content": "Echoing.",
        "tool_calls": [{"id": "toolu_echo", "function": {"name": "echo", "arguments": '{"x": 1}'}}],
    }

    @pytest.fixture
    def echoed_trial(self, request, runner_service, mock_grpc_context, simple_task_description):
        """A registered trial that has called ``echo`` once, as ``toolu_echo``."""
        trial_id = f"{request.node.name}:0"
        registered = runner_service.RegisterTrial(
            register_request(
                trial_spec_json(self._task(simple_task_description), trial_id=trial_id),
                trial_id=trial_id,
            ),
            mock_grpc_context,
        )
        assert registered.success is True, registered.error

        async def echo(args):
            return json.dumps(args)

        runner_service.trials[trial_id].agent_tools["echo"] = echo
        executed = runner_service.ExecuteTool(
            execute_request(trial_id, "echo", json.dumps({"x": 1}), call_id="toolu_echo"),
            mock_grpc_context,
        )
        assert executed.status == pb2.EXECUTION_STATUS_SUCCESS
        return trial_id

    @staticmethod
    def _grade(runner_service, mock_grpc_context, trial_id: str, payload: str):
        return runner_service.GradeTrial(
            pb2.GradeTrialRequest(trial_id=trial_id, llm_messages_json=payload),
            mock_grpc_context,
        )

    def test_a_transcript_that_declares_the_call_grades_off_the_record(
        self, runner_service, mock_grpc_context, echoed_trial
    ):
        response = self._grade(
            runner_service,
            mock_grpc_context,
            echoed_trial,
            json.dumps([{"role": "user", "content": "echo x"}, self._CALLED_ECHO]),
        )

        assert response.success is True, response.error
        assert response.grade.components.transcript_rules == pytest.approx(1.0)
        assert response.grade.binary_pass is True

    def test_a_record_the_transcript_omits_fails_the_rpc_naming_the_call_id(
        self, runner_service, mock_grpc_context, echoed_trial
    ):
        """The two views disagree about what the trial did, which is a harness bug.
        Scoring around it is the silent degradation the record exists to remove."""
        response = self._grade(
            runner_service,
            mock_grpc_context,
            echoed_trial,
            json.dumps(
                [
                    {"role": "user", "content": "echo x"},
                    {"role": "assistant", "content": "Echoing."},
                ]
            ),
        )

        assert response.success is False
        assert "TimelineInconsistencyError" in response.error
        assert "toolu_echo" in response.error
        assert not response.HasField("grade")

    def test_a_policy_only_payload_is_a_hash_only_trial_not_a_message_view(
        self, runner_service, mock_grpc_context, echoed_trial
    ):
        """A TlkMcpCore / Tau trial graded on state alone sends the agent policy and
        no transcript. Reading that one harness message as a message view would make
        every recorded call unlinkable and fail an entirely legitimate trial."""
        response = self._grade(
            runner_service,
            mock_grpc_context,
            echoed_trial,
            json.dumps([{"role": "system", "content": "You are a test assistant."}]),
        )

        assert response.success is True, response.error
        assert response.grade.components.transcript_rules == pytest.approx(1.0)
        assert response.grade.binary_pass is True

    def test_an_absent_payload_grades_off_the_records_alone(
        self, runner_service, mock_grpc_context, echoed_trial
    ):
        response = self._grade(runner_service, mock_grpc_context, echoed_trial, "")

        assert response.success is True, response.error
        assert response.grade.components.transcript_rules == pytest.approx(1.0)
        assert response.grade.binary_pass is True

    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ("{not json", "JSONDecodeError"),
            ('[{"role": "assistant"}]', "has no 'content'"),
            (
                '[{"role": "assistant", "content": "", "tool_calls": '
                '[{"function": {"name": "echo", "arguments": "{}"}}]}]',
                "carries no 'id'",
            ),
            (
                '[{"role": "assistant", "content": "", "tool_calls": [{"id": "toolu_echo"}]}]',
                "no 'function'",
            ),
            (
                '[{"role": "assistant", "content": "", "tool_calls": '
                '[{"id": "toolu_echo", "function": {"name": "echo"}}]}]',
                "has no 'arguments'",
            ),
        ],
    )
    def test_an_undecodable_payload_fails_the_rpc(
        self, runner_service, mock_grpc_context, echoed_trial, payload: str, expected: str
    ):
        """Grading an empty transcript because the real one would not parse reports a
        verdict on evidence that was never read."""
        response = self._grade(runner_service, mock_grpc_context, echoed_trial, payload)

        assert response.success is False
        assert expected in response.error
        assert not response.HasField("grade")


class TestDuplicateCallIdPublishesNoScore:
    """A duplicate ``call_id`` runs the runner and the host together.

    Nothing rejects a duplicate at record time, so a provider or litellm
    id-synthesis collision reaches grade time and the timeline cannot join a call
    to its result. Both layers have to answer correctly for no number to be
    published: the servicer must refuse to grade, and the host must refuse to
    invent the verdict the servicer did not return.
    """

    _TRIAL_ID = "duplicate_call_id:0"

    @pytest.fixture
    def collided_trial(self, runner_service, mock_grpc_context, simple_task_description):
        return register_collided_trial(
            runner_service,
            mock_grpc_context,
            simple_task_description,
            trial_id=self._TRIAL_ID,
        )

    def test_the_host_raises_rather_than_publishing_a_zero(
        self, runner_service, mock_grpc_context, collided_trial
    ):
        grader = RunnerRPCTrialGrader(
            runner_address="in-process-servicer:0",
            logger=MagicMock(),
            runner_client=ServicerBackend(runner_service, mock_grpc_context),
        )
        trajectory = collided_trajectory(task_id="task-1")

        with pytest.raises(GradingFailedError) as excinfo:
            grader.grade(
                make_trial_spec(trial_id=collided_trial, task_id="duplicate_call_id"),
                trajectory,
                "You are a test assistant.",
            )

        assert DUPLICATE_CALL_ID in str(excinfo.value)
        assert trajectory.grade is None


class TestTheToolBudgetIsTheOneThatFires:
    """``ExecuteTool`` enforces the tool budget twice — once on the coroutine's
    ``asyncio.wait_for``, once on the thread-bridge future that awaits it. Only
    the inner one knows the outcome is a timeout; the outer one surfaces as a
    bare ``TimeoutError`` the handler can only report as ``ERROR``, and its
    cancellation drops the call before it is recorded. So an overrun has to be
    the inner deadline's, and the trial's history has to show it.
    """

    @pytest.fixture
    def budget_trial(
        self, runner_service, mock_grpc_context, simple_task_description, request
    ) -> str:
        trial_id = f"{request.node.name}:0"
        registration = register_request(
            trial_spec_json(simple_task_description, trial_id=trial_id), trial_id=trial_id
        )
        registered = runner_service.RegisterTrial(registration, mock_grpc_context)
        assert registered.success is True, registered.error

        async def sleeps_past_any_budget(args):
            await asyncio.sleep(30)
            return json.dumps(args)

        async def returns_immediately(args):
            return json.dumps({"ok": True})

        trial = runner_service.trials[trial_id]
        trial.agent_tools["sleeps_past_any_budget"] = sleeps_past_any_budget
        trial.agent_tools["returns_immediately"] = returns_immediately
        return trial_id

    def test_an_overrun_is_reported_as_timeout_and_recorded(
        self, runner_service, mock_grpc_context, budget_trial
    ):
        response = runner_service.ExecuteTool(
            execute_request(
                budget_trial,
                "sleeps_past_any_budget",
                call_id="call_slow",
                timeout_seconds=0.05,
            ),
            mock_grpc_context,
        )

        assert response.status == pb2.EXECUTION_STATUS_TIMEOUT
        assert "timed out after" in response.error_message
        recorded = runner_service.trials[budget_trial].tool_call_history[-1]
        assert recorded.call_id == "call_slow"
        assert recorded.status is ToolExecutionStatus.TIMEOUT

    def test_a_call_inside_its_budget_is_left_alone(
        self, runner_service, mock_grpc_context, budget_trial
    ):
        response = runner_service.ExecuteTool(
            execute_request(
                budget_trial,
                "returns_immediately",
                call_id="call_fast",
                timeout_seconds=10.0,
            ),
            mock_grpc_context,
        )

        assert response.status == pb2.EXECUTION_STATUS_SUCCESS
        assert response.error_message == ""
        assert json.loads(response.output) == {"ok": True}


# NOTE: TestDBClientWithTestClient has been moved to tests/test_db_client.py
# to avoid duplication. See TestDBServiceClientLifecycle for comprehensive
# DB client tests against real json_db_service.
