"""Functional tests for golden set (hash) grading using test projects

This approach uses complete project snapshots in tests/data/projects/ instead of mocks.

Advantages:
- Tests use real MCP servers and tool implementations
- No need to maintain separate mocks
- Easier to reproduce bugs from production
- Projects can be re-run to generate fresh data
"""

import copy

import pytest

from tolokaforge.core.grading.state_checks import (
    consistent_hash,
    to_hashable,
)

pytestmark = pytest.mark.canonical


@pytest.mark.canonical
@pytest.mark.grading
class TestProjectBasedHashGrading:
    """Test hash grading using real project data"""

    def test_load_trajectory(self, food_delivery_2_trajectory_051fa6cb):
        """Test loading trajectory data"""
        assert "task_id" in food_delivery_2_trajectory_051fa6cb
        assert "final_env_state" in food_delivery_2_trajectory_051fa6cb
        assert "grade" in food_delivery_2_trajectory_051fa6cb
        assert "messages" in food_delivery_2_trajectory_051fa6cb

    def test_load_mcp_server(self, food_delivery_2_mcp_server):
        """Test loading MCP server module"""
        assert hasattr(food_delivery_2_mcp_server, "TOOLS")
        tools = food_delivery_2_mcp_server.TOOLS
        assert "get_user_details" in tools
        assert "create_order" in tools


@pytest.mark.canonical
@pytest.mark.grading
class TestGoldenSetWithRealTools:
    """Test golden set grading using real MCP tools from project"""

    def test_execute_golden_actions_with_real_tools(
        self,
        food_delivery_2_initial_state,
        food_delivery_2_grading_051fa6cb,
        food_delivery_2_mcp_server,
    ):
        """Test executing golden actions using real MCP server tools"""
        # Get golden actions
        golden_actions = food_delivery_2_grading_051fa6cb["state_checks"]["hash"]["golden_actions"]

        # Get tools from MCP server
        TOOLS = food_delivery_2_mcp_server.TOOLS

        # Execute golden actions on fresh state
        state = copy.deepcopy(food_delivery_2_initial_state)

        errors = []
        for action in golden_actions:
            action_name = action["name"]
            action_kwargs = action["kwargs"]

            if action_name in TOOLS:
                tool_class = TOOLS[action_name]
                try:
                    tool_class.invoke(data=state, **action_kwargs)
                except Exception as e:
                    errors.append(f"Tool '{action_name}' failed: {e}")

        if errors:
            pytest.fail(f"Golden replay had {len(errors)} tool failures:\n" + "\n".join(errors))

        # Compute final hash
        final_hash = consistent_hash(to_hashable(state))

        assert final_hash is not None
        assert len(final_hash) == 64  # SHA256 hex

    def test_reproduce_bug_with_real_tools(
        self,
        food_delivery_2_initial_state,
        food_delivery_2_grading_051fa6cb,
        food_delivery_2_trajectory_051fa6cb,
        food_delivery_2_mcp_server,
    ):
        """
        CRITICAL TEST: Reproduce hash mismatch bug using real MCP tools

        This test:
        1. Executes golden actions with REAL tools from mcp_server.py
        2. Computes expected hash from golden execution
        3. Extracts actual state from trajectory
        4. Compares hashes
        5. Should reproduce the exact bug
        """
        # Step 1: Execute golden actions with real tools
        golden_actions = food_delivery_2_grading_051fa6cb["state_checks"]["hash"]["golden_actions"]
        TOOLS = food_delivery_2_mcp_server.TOOLS

        expected_state = copy.deepcopy(food_delivery_2_initial_state)

        errors = []
        for action in golden_actions:
            action_name = action["name"]
            action_kwargs = action["kwargs"]

            if action_name in TOOLS:
                tool_class = TOOLS[action_name]
                try:
                    tool_class.invoke(data=expected_state, **action_kwargs)
                except Exception as e:
                    errors.append(f"Tool '{action_name}' failed: {e}")

        if errors:
            pytest.fail(f"Golden replay had {len(errors)} tool failures:\n" + "\n".join(errors))

        # Step 2: Compute expected hash
        expected_hash = consistent_hash(to_hashable(expected_state))

        # Step 3: Extract actual state from trajectory
        final_env_state = food_delivery_2_trajectory_051fa6cb["final_env_state"]

        # Current extraction logic: prefer db over agent
        actual_db_state = final_env_state.get("db", final_env_state.get("agent", final_env_state))

        # Step 4: Compute actual hash
        actual_hash = consistent_hash(to_hashable(actual_db_state))

        # Step 5: Verify bug is reproduced
        assert (
            expected_hash != actual_hash
        ), "Bug should be reproduced - if this fails, bug was fixed!"


@pytest.mark.canonical
@pytest.mark.grading
class TestStateExtraction:
    """Test state extraction from trajectories"""

    def test_trajectory_has_required_fields(self, food_delivery_2_trajectory_051fa6cb):
        """Verify trajectory has all required fields for testing"""
        assert "final_env_state" in food_delivery_2_trajectory_051fa6cb
        assert "grade" in food_delivery_2_trajectory_051fa6cb

        final_state = food_delivery_2_trajectory_051fa6cb["final_env_state"]
        assert "agent" in final_state
        assert "db" in final_state
        assert "user" in final_state

    def test_agent_and_db_states_identical(self, food_delivery_2_trajectory_051fa6cb):
        """Test if agent and db states are identical"""
        final_state = food_delivery_2_trajectory_051fa6cb["final_env_state"]

        agent_state = final_state.get("agent", {})
        db_state = final_state.get("db", {})

        agent_hash = consistent_hash(to_hashable(agent_state))
        db_hash = consistent_hash(to_hashable(db_state))

        assert (
            agent_hash == db_hash
        ), f"Agent and DB states differ: agent={agent_hash[:16]}… db={db_hash[:16]}…"
