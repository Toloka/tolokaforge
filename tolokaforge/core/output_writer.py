"""Output writer for split trial files

This module handles writing trial results to multiple focused YAML files
instead of a single large trajectory file.
"""

from pathlib import Path
from typing import Any

import yaml

from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import Grade, ToolExecutionStatus, Trajectory

TRIAL_BUNDLE_SCHEMA_VERSION = 4
"""The per-trial bundle generation stamped into ``metrics.yaml``.

Version 4 bundles carry the trial's tool-call record as ``tool_log.yaml`` beside
the message view in ``trajectory.yaml``, so a bundle re-grades to the verdict its
live run produced.

They omit ``grade.yaml`` for two different trials — one the infrastructure aborted
before the agent ran, and one whose grading refused to produce a verdict. The
discriminator is ``trajectory.yaml``'s ``grading_error``: populated for the second,
``null`` for the first. An absent grade alone does not say which happened.
"""

TOOL_LOG_FILENAME = "tool_log.yaml"
"""The bundle's tool-call record. Read back with
:func:`tolokaforge.core.output.artifacts.read_recorded_tool_log`."""


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
    - tool_log.yaml: The trial's ordered tool-call record
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

        # Include model_config when present (added by orchestrator for reproducibility)
        if "model_config" in task_config:
            task_info["model_config"] = task_config["model_config"]

        with open(self.output_dir / "task.yaml", "w") as f:
            yaml.dump(task_info, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    def write_trajectory(self, trajectory: Trajectory):
        """Write trajectory.yaml — message trace + status + metrics only.

        The agent's system prompt and user-simulator system prompt are
        persisted separately as ``prompts.yaml`` (see
        :meth:`FileArtifactWriter.write_prompts`); tool schemas live in
        ``tools_schemas.yaml``. Keeping the trajectory lean means
        analysts who scan the message trace don't have to scroll past
        ~15-20 KB of system prompt text on every file open.

        ``simulator_schema_version`` stays here because it describes the
        *shape* of the message trace (when the simulator prompt was
        revised, the on-disk shape may have changed too).

        ``grading_error`` is the only record of why a trial carries no
        ``grade.yaml`` because grading refused, so it lives in the bundle
        rather than only in the run's logs.

        Args:
            trajectory: Trajectory object containing messages and metadata
        """
        traj_data = {
            "task_id": trajectory.task_id,
            "trial_index": trajectory.trial_index,
            "simulator_schema_version": trajectory.simulator_schema_version,
            "start_ts": trajectory.start_ts.isoformat(),
            "end_ts": trajectory.end_ts.isoformat(),
            "status": trajectory.status.value,
            "termination_reason": (
                trajectory.termination_reason.value if trajectory.termination_reason else None
            ),
            "grading_error": trajectory.grading_error,
            "first_user_message_source": (
                trajectory.first_user_message_source.value
                if trajectory.first_user_message_source
                else None
            ),
            "messages": [msg.model_dump(mode="json") for msg in trajectory.messages],
            "user_reply_guard_events": [
                event.model_dump(mode="json") for event in trajectory.user_reply_guard_events
            ],
        }

        with open(self.output_dir / "trajectory.yaml", "w") as f:
            yaml.dump(traj_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    def write_tool_log(self, trajectory: Trajectory):
        """Write tool_log.yaml — the trial's tool-call record, in ``sequence`` order.

        A sidecar rather than a key on ``trajectory.yaml``: the record repeats every
        tool's untruncated output, which the ``role: tool`` messages already carry,
        and on a tool-heavy trial that is most of the bundle. Whoever reads only the
        message trace pays nothing for it.

        A trial that called no tool writes an empty list. The file's *absence* means
        the bundle was written before the record was — a different state, kept apart
        by :func:`tolokaforge.core.output.artifacts.read_recorded_tool_log`.

        Args:
            trajectory: Trajectory object carrying the tool-call record
        """
        record = [
            call.model_dump(mode="json")
            for call in sorted(trajectory.tool_log, key=lambda call: call.sequence)
        ]

        with open(self.output_dir / TOOL_LOG_FILENAME, "w", encoding="utf-8") as f:
            yaml.dump(record, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

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
        metrics_data["schema_version"] = TRIAL_BUNDLE_SCHEMA_VERSION

        # Add detailed tool usage breakdown from tool_log
        # Field names match ToolUsage model: tool_name, call_count, success_count,
        # error_count, total_duration_s — so analysis tooling can model_validate()
        # the metrics.yaml round-trip.
        tool_usage: dict[str, dict[str, float | int]] = {}
        for call in trajectory.tool_log:
            if call.tool_name not in tool_usage:
                tool_usage[call.tool_name] = {
                    "call_count": 0,
                    "success_count": 0,
                    "error_count": 0,
                    "total_duration_s": 0.0,
                }

            tool_usage[call.tool_name]["call_count"] += 1
            tool_usage[call.tool_name]["total_duration_s"] += call.latency_seconds
            if call.status is ToolExecutionStatus.SUCCESS:
                tool_usage[call.tool_name]["success_count"] += 1
            else:
                tool_usage[call.tool_name]["error_count"] += 1

        # Convert to sorted list matching ToolUsage schema
        metrics_data["tool_usage"] = [
            {"tool_name": name, **stats} for name, stats in sorted(tool_usage.items())
        ]

        with open(self.output_dir / "metrics.yaml", "w") as f:
            yaml.dump(
                metrics_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False
            )

    def write_grade(self, grade: Grade):
        """Write grade.yaml with grading results (+ judge_trajectory.yaml sidecar).

        ``grade.yaml`` carries the per-criterion breakdown, the ``judge_status``,
        and the judge's token usage / cost (``judge_usage``) so a reviewer sees
        the full verdict in one scannable file. The judge's message transcript —
        often kilobytes of tool calls and inspection — is split into a sibling
        ``judge_trajectory.yaml`` (mirroring the ``trajectory.yaml`` /
        ``prompts.yaml`` split), so it never bloats the grade file. See
        docs/OUTPUT_FORMAT.md.

        Args:
            grade: Grade object with scores, reasons, judge usage / transcript
        """
        # Keep the transcript and the judge's structured inputs out of grade.yaml;
        # each lands in its own sidecar.
        grade_payload = grade.model_dump(mode="json", exclude={"judge_transcript", "judge_inputs"})
        with open(self.output_dir / "grade.yaml", "w") as f:
            yaml.dump(
                grade_payload,
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

        # Sidecar: the judge's own message transcript, only when a judge ran and
        # captured a non-empty one. Absent file ⇒ no judge transcript for this
        # trial (gate on truthiness, not ``is not None`` — an empty transcript is
        # not worth a sidecar).
        if grade.judge_transcript:
            with open(self.output_dir / "judge_trajectory.yaml", "w") as f:
                yaml.dump(
                    {"messages": grade.judge_transcript},
                    f,
                    default_flow_style=False,
                    allow_unicode=True,
                    sort_keys=False,
                )

        # Sidecar: the judge's non-derivable run() inputs (state-diff string +
        # read-tool surface), only when a judge ran. Absent file ⇒ no judge inputs
        # recorded for this trial. Kept out of grade.yaml (the diff can be large);
        # replay reconstructs the judge's opening message from it.
        if grade.judge_inputs:
            with open(self.output_dir / "judge_inputs.yaml", "w") as f:
                yaml.dump(
                    grade.judge_inputs.model_dump(mode="json"),
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
        self.write_tool_log(trajectory)
        self.write_env_state(env_state)
        self.write_metrics(trajectory)

        if trajectory.grade:
            self.write_grade(trajectory.grade)

        self.write_logs(logger)
