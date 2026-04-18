"""
Canonical tests for the GradingEngine pipeline using real trajectory data.

These tests verify that the complete grading pipeline works correctly by:
1. Loading real trajectory data from test projects
2. Running the GradingEngine with actual configurations
3. Verifying that all enabled grading components are executed
4. Asserting that the final grade includes expected components

These tests would have caught the custom_checks integration bug because they
verify that enabled components actually contribute to the final grade.
"""

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.canonical

from tests.utils.project_fixtures import (
    TEST_PROJECTS_DIR,
    list_project_tasks,
    load_project_grading,
    load_project_trajectory,
)
from tolokaforge.core.grading.combine import GradingEngine
from tolokaforge.core.models import (
    GradingConfig,
    Message,
    MessageRole,
    Metrics,
    ToolCall,
    Trajectory,
    TrialStatus,
)


def build_metrics_from_data(metrics_data: dict[str, Any]) -> Metrics:
    """Convert metrics dict from YAML to Metrics model.

    Handles field name transformations between stored data and model.
    Stored data may use 'tool' while model expects 'tool_name'.
    """
    if not metrics_data:
        return Metrics()

    # Transform tool_usage field names if present
    tool_usage_data = metrics_data.get("tool_usage", [])
    transformed_tool_usage = []
    for tu in tool_usage_data:
        transformed = {
            # Map 'tool' -> 'tool_name' if needed
            "tool_name": tu.get("tool_name") or tu.get("tool", "unknown"),
            "call_count": tu.get("call_count") or tu.get("count", 0),
            "success_count": tu.get("success_count") or tu.get("success", 0),
            "error_count": tu.get("error_count") or tu.get("fail", 0),
            "total_duration_s": tu.get("total_duration_s", 0.0),
        }
        transformed_tool_usage.append(transformed)

    return Metrics(
        latency_total_s=metrics_data.get("latency_total_s", 0.0),
        turns=metrics_data.get("turns", 0),
        api_calls=metrics_data.get("api_calls", 0),
        tokens_input=metrics_data.get("tokens_input", 0),
        tokens_output=metrics_data.get("tokens_output", 0),
        cost_usd_est=metrics_data.get("cost_usd_est", 0.0),
        tool_calls=metrics_data.get("tool_calls", 0),
        tool_success_rate=metrics_data.get("tool_success_rate", 0.0),
        stuck_detected=metrics_data.get("stuck_detected", False),
        tool_usage=transformed_tool_usage,
    )


def build_trajectory_from_data(traj_data: dict[str, Any]) -> Trajectory:
    """Convert trajectory dict from YAML to Trajectory model.

    Args:
        traj_data: Raw trajectory dict loaded from YAML

    Returns:
        Trajectory pydantic model instance
    """
    messages = []
    for msg_data in traj_data.get("messages", []):
        tool_calls = None
        if msg_data.get("tool_calls"):
            tool_calls = [
                ToolCall(
                    id=tc.get("id", ""),
                    name=tc.get("name", ""),
                    arguments=tc.get("arguments", {}),
                )
                for tc in msg_data["tool_calls"]
            ]

        # Handle role as string or enum
        role_str = msg_data.get("role", "user")
        role = MessageRole(role_str)

        messages.append(
            Message(
                role=role,
                content=msg_data.get("content", ""),
                tool_calls=tool_calls,
                tool_call_id=msg_data.get("tool_call_id"),
                reasoning=msg_data.get("reasoning"),
            )
        )

    return Trajectory(
        task_id=traj_data.get("task_id", "unknown"),
        trial_index=traj_data.get("trial_index", 0),
        start_ts=datetime.fromisoformat(traj_data.get("start_ts", datetime.now().isoformat())),
        end_ts=datetime.fromisoformat(traj_data.get("end_ts", datetime.now().isoformat())),
        status=TrialStatus(traj_data.get("status", "completed")),
        messages=messages,
        final_env_state=traj_data.get("final_env_state", {}),
        metrics=build_metrics_from_data(traj_data.get("metrics", {})),
        tool_log=traj_data.get("tool_log", []),
    )


class TestGradingEnginePipeline:
    """Tests for the complete GradingEngine pipeline."""

    def test_all_enabled_components_contribute_to_grade(self):
        """Verify that ALL enabled grading components contribute to the final grade.

        For each component that's enabled in grading.yaml, verify:
        1. The component appears in grade.components
        2. The component has a non-null score
        3. If weighted, the component affects the final score
        """
        project_name = "food_delivery_2"
        task_ids = list_project_tasks(project_name)
        if not task_ids:
            pytest.skip(f"No tasks found for project '{project_name}' — project data not available")

        for task_id in task_ids:
            task_dir = TEST_PROJECTS_DIR / project_name / "tasks" / task_id
            grading_config_dict = load_project_grading(project_name, task_id)

            # Skip if no trajectory exists
            try:
                traj_data = load_project_trajectory(project_name, task_id, 0)
            except FileNotFoundError:
                continue

            trajectory = build_trajectory_from_data(traj_data)
            final_state = traj_data.get("final_env_state", {})

            grading_config = GradingConfig(**grading_config_dict)
            grading_engine = GradingEngine(
                grading_config=grading_config,
                task_dir=task_dir,
                task_domain=project_name,
            )

            grade = grading_engine.grade_trajectory(trajectory, final_state)

            # Check each potentially enabled component
            weights = grading_config_dict.get("combine", {}).get("weights", {})

            # state_checks
            if grading_config_dict.get("state_checks") and "state_checks" in weights:
                assert (
                    grade.components.state_checks is not None
                ), f"Task {task_id}: state_checks enabled but not in grade"

            # transcript_rules
            if grading_config_dict.get("transcript_rules") and "transcript_rules" in weights:
                assert (
                    grade.components.transcript_rules is not None
                ), f"Task {task_id}: transcript_rules enabled but not in grade"

            # custom_checks
            custom_checks_config = grading_config_dict.get("custom_checks", {})
            if custom_checks_config.get("enabled") and "custom_checks" in weights:
                assert grade.components.custom_checks is not None, (
                    f"Task {task_id}: custom_checks enabled but not in grade. "
                    f"This indicates the custom_checks integration is broken!"
                )


class TestGradingEngineWithMockedTrajectory:
    """Tests using constructed trajectories to verify specific behaviors."""

    @pytest.fixture
    def minimal_trajectory(self) -> Trajectory:
        """Create a minimal valid trajectory for testing."""
        return Trajectory(
            task_id="test-task",
            trial_index=0,
            start_ts=datetime.now(),
            end_ts=datetime.now(),
            status=TrialStatus.COMPLETED,
            messages=[
                Message(
                    role=MessageRole.USER,
                    content="Hello",
                ),
                Message(
                    role=MessageRole.ASSISTANT,
                    content="Hi there!",
                ),
            ],
        )

    @pytest.fixture
    def grading_config_with_custom_checks(self) -> dict[str, Any]:
        """Create grading config with custom_checks enabled."""
        return {
            "combine": {
                "method": "weighted",
                "weights": {
                    "state_checks": 0.5,
                    "custom_checks": 0.5,
                },
                "pass_threshold": 0.8,
            },
            "state_checks": {
                "jsonpaths": [],
            },
            "custom_checks": {
                "enabled": True,
                "file": "checks.py",
                "timeout_seconds": 30,
                "interface_version": "1.0",
                "relative_imports": ["../.."],
            },
        }

    def test_custom_checks_runs_when_task_dir_provided(
        self,
        minimal_trajectory: Trajectory,
    ):
        """Verify custom checks execute when task_dir is provided."""
        # Use a real task with checks.py
        project_name = "food_delivery_2"
        task_id = "order_modify_with_checks"
        task_dir = TEST_PROJECTS_DIR / project_name / "tasks" / task_id

        if not (task_dir / "checks.py").exists():
            pytest.skip("No checks.py found for test task")

        grading_config_dict = load_project_grading(project_name, task_id)

        # Load real final state for meaningful check execution
        try:
            traj_data = load_project_trajectory(project_name, task_id, 0)
            final_state = traj_data.get("final_env_state", {})
        except FileNotFoundError:
            final_state = {}

        grading_config = GradingConfig(**grading_config_dict)
        grading_engine = GradingEngine(
            grading_config=grading_config,
            task_dir=task_dir,
        )

        # Use minimal trajectory but real final state
        minimal_trajectory.task_id = task_id
        grade = grading_engine.grade_trajectory(minimal_trajectory, final_state)

        # custom_checks should be populated
        assert grade.components.custom_checks is not None

    def test_custom_checks_skipped_when_task_dir_not_provided(
        self,
        minimal_trajectory: Trajectory,
        grading_config_with_custom_checks: dict[str, Any],
    ):
        """Verify custom checks are skipped when task_dir is None."""
        grading_config = GradingConfig(**grading_config_with_custom_checks)

        # Create engine WITHOUT task_dir
        grading_engine = GradingEngine(
            grading_config=grading_config,
            task_dir=None,  # No task_dir
        )

        grade = grading_engine.grade_trajectory(minimal_trajectory, {})

        # custom_checks should NOT be populated (condition: task_dir required)
        # Based on the code: `if self.config.custom_checks and self.task_dir:`
        assert grade.components.custom_checks is None

    def test_custom_checks_skipped_when_checks_file_missing(
        self,
        minimal_trajectory: Trajectory,
        grading_config_with_custom_checks: dict[str, Any],
        tmp_path: Path,
    ):
        """Verify graceful handling when checks.py doesn't exist."""
        grading_config = GradingConfig(**grading_config_with_custom_checks)

        # Create engine with task_dir that has no checks.py
        grading_engine = GradingEngine(
            grading_config=grading_config,
            task_dir=tmp_path,  # Empty directory
        )

        grade = grading_engine.grade_trajectory(minimal_trajectory, {})

        # Should handle missing file gracefully with 0 score
        assert grade.components.custom_checks == 0.0
