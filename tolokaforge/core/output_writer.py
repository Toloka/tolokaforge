"""Output writer for split trial files

This module handles writing trial results to multiple focused YAML files
instead of a single large trajectory file.
"""

from pathlib import Path
from typing import Any

import yaml

from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import Grade, Trajectory


def _represent_multiline_str(dumper, data):
    """Custom YAML representer for multiline strings

    Uses literal block scalar (|) for strings containing newlines
    """
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


# Register custom representer for multiline strings
yaml.add_representer(str, _represent_multiline_str)


class OutputWriter:
    """Writes split output files for a trial

    Splits trajectory data into focused files:
    - task.yaml: Task metadata and grading configuration
    - trajectory.yaml: Conversation messages only
    - env.yaml: Final environment state
    - metrics.yaml: Performance metrics with tool usage breakdown
    - grade.yaml: Grading results with detailed diff
    - logs.yaml: Structured trial logs
    """

    def __init__(self, output_dir: Path):
        """Initialize output writer

        Args:
            output_dir: Directory to write output files
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def write_task_info(self, task_config: dict[str, Any]):
        """Write task.yaml with task metadata and grading config

        Args:
            task_config: Dictionary containing:
                - task_id: Task identifier
                - trial_index: Trial index
                - category: Task category
                - description: Task description
                - grading_config: Grading configuration dict
                - tools: Tools configuration dict
                - policies: Task policies dict
        """
        task_info = {
            "task_id": task_config.get("task_id"),
            "trial_index": task_config.get("trial_index"),
            "category": task_config.get("category"),
            "description": task_config.get("description"),
            "grading_config": task_config.get("grading_config", {}),
            "tools": task_config.get("tools", {}),
            "policies": task_config.get("policies", {}),
        }

        with open(self.output_dir / "task.yaml", "w") as f:
            yaml.dump(task_info, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    def write_trajectory(self, trajectory: Trajectory):
        """Write trajectory.yaml with messages only

        Args:
            trajectory: Trajectory object containing messages and metadata
        """
        traj_data = {
            "task_id": trajectory.task_id,
            "trial_index": trajectory.trial_index,
            "start_ts": trajectory.start_ts.isoformat(),
            "end_ts": trajectory.end_ts.isoformat(),
            "status": trajectory.status.value,
            "termination_reason": (
                trajectory.termination_reason.value if trajectory.termination_reason else None
            ),
            "messages": [msg.model_dump(mode="json") for msg in trajectory.messages],
        }

        with open(self.output_dir / "trajectory.yaml", "w") as f:
            yaml.dump(traj_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    def write_env_state(self, env_state: dict[str, Any]):
        """Write env.yaml with final environment state

        Args:
            env_state: Final environment state dictionary
        """
        with open(self.output_dir / "env.yaml", "w") as f:
            yaml.dump(env_state, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    def write_metrics(self, trajectory: Trajectory):
        """Write metrics.yaml with performance metrics and tool usage

        Args:
            trajectory: Trajectory object containing metrics
        """
        metrics_data = trajectory.metrics.model_dump(mode="json")

        # Add detailed tool usage breakdown from tool_log
        # Field names must match ToolUsage model: tool_name, call_count, success_count, error_count
        tool_usage: dict[str, dict[str, int]] = {}
        for log in trajectory.tool_log:
            tool_name = log.get("tool")
            if not tool_name:
                continue

            if tool_name not in tool_usage:
                tool_usage[tool_name] = {"call_count": 0, "success_count": 0, "error_count": 0}

            tool_usage[tool_name]["call_count"] += 1
            if log.get("success"):
                tool_usage[tool_name]["success_count"] += 1
            else:
                tool_usage[tool_name]["error_count"] += 1

        # Convert to sorted list matching ToolUsage schema
        metrics_data["tool_usage"] = [
            {"tool_name": name, "total_duration_s": 0.0, **stats}
            for name, stats in sorted(tool_usage.items())
        ]

        with open(self.output_dir / "metrics.yaml", "w") as f:
            yaml.dump(
                metrics_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False
            )

    def write_grade(self, grade: Grade):
        """Write grade.yaml with grading results

        Args:
            grade: Grade object with scores and reasons
        """
        with open(self.output_dir / "grade.yaml", "w") as f:
            yaml.dump(
                grade.model_dump(mode="json"),
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

    def write_logs(self, logger: StructuredLogger):
        """Write logs.yaml from structured logger

        Args:
            logger: StructuredLogger instance with collected logs
        """
        logger.save_to_file(self.output_dir / "logs.yaml")

    def write_all(
        self,
        trajectory: Trajectory,
        task_config: dict[str, Any],
        env_state: dict[str, Any],
        logger: StructuredLogger,
    ):
        """Write all output files at once

        Convenience method to write all files in one call.

        Args:
            trajectory: Trajectory object
            task_config: Task configuration dictionary
            env_state: Final environment state
            logger: StructuredLogger instance
        """
        self.write_task_info(task_config)
        self.write_trajectory(trajectory)
        self.write_env_state(env_state)
        self.write_metrics(trajectory)

        if trajectory.grade:
            self.write_grade(trajectory.grade)

        self.write_logs(logger)
