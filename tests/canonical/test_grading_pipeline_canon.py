"""Canonical tests for the GradingEngine pipeline using synthetic trajectories.

These tests verify the GradingEngine's component-execution behaviour without
depending on any specific project fixture data. They cover:

- ``custom_checks`` is skipped when no ``task_dir`` is provided
- ``custom_checks`` is handled gracefully when the ``checks.py`` file is
  missing from a provided ``task_dir``

The previous ``TestGradingEnginePipeline`` (which ran the engine over real
``food_delivery_2`` recorded trajectories) was tau-style data tied to a
fixture project that lives outside tolokaforge after the three-repo split;
the corresponding coverage now lives in the private tools repo.
"""

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.canonical

from tolokaforge.core.grading.combine import GradingEngine
from tolokaforge.core.models import GradingConfig, Message, MessageRole, Trajectory, TrialStatus


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
