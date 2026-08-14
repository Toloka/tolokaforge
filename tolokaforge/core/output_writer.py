"""Output writer for split trial files

This module handles writing trial results to multiple focused YAML files
instead of a single large trajectory file.
"""

from pathlib import Path
from typing import Any

import yaml

from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import Grade, ToolExecutionStatus, Trajectory
from tolokaforge.core.redaction import (
    NoRedaction,
    RedactionPolicy,
    RedactionPolicyName,
    RedactionStamp,
)

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

METRICS_FILENAME = "metrics.yaml"
"""The bundle's header — what it carries, alongside the metrics themselves.
:func:`tolokaforge.core.output.artifacts.bundle_redaction` reads its
``redaction`` key."""

TRAJECTORY_FILENAME = "trajectory.yaml"
"""The bundle's message trace."""

ENV_FILENAME = "env.yaml"
"""The trial's final environment state."""

JUDGE_TRAJECTORY_FILENAME = "judge_trajectory.yaml"
"""The rubric judge's own transcript. Withheld under a redacting policy — it
renders the agent's arguments into prose a key-name rule cannot reach."""

JUDGE_INPUTS_FILENAME = "judge_inputs.yaml"
"""The judge's non-derivable ``run()`` inputs. Withheld under a redacting policy
for the same reason as the transcript — ``state_diff_text`` renders values."""


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

    def __init__(self, output_dir: Path, redaction: RedactionPolicy = NoRedaction()):
        """Initialize output writer

        Args:
            output_dir: Directory to write output files
            redaction: Applied to tool-call arguments on their way to disk.
                The default writes what the agent sent. Anything else rewrites
                the bundle, which stamps itself so no offline command grades it.
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.redaction = redaction
        # Trial-scoped: one writer per trial directory, so a second trial's
        # stamp cannot name the first trial's artifacts.
        self._rewritten: set[str] = set()
        self._omitted: set[str] = set()

    @property
    def _redacting(self) -> bool:
        return self.redaction.name is not RedactionPolicyName.NONE

    def _note_rewritten(self, artifact: str) -> None:
        """Record that *artifact* was written with the policy applied, and declare it.

        Accumulated rather than declared statically: the provision-failure path
        writes no ``tool_log.yaml``, and a stamp naming a file the bundle lacks
        is a stamp a reader cannot check.
        """
        if self._redacting:
            self._rewritten.add(artifact)
            self._declare()

    def _note_withheld(self, artifact: str) -> None:
        """Record that the policy suppressed *artifact* rather than rewriting it."""
        if self._redacting:
            self._omitted.add(artifact)
            self._declare()

    def _declare(self) -> None:
        """Put the current stamp into ``metrics.yaml``, creating the file if needed.

        The declaration belongs to the writer rather than to :meth:`write_metrics`:
        a caller writing only some of the bundle — the replay path writes a grade
        and nothing else — must still leave a bundle that says a policy touched it,
        or an unstamped rewritten bundle reads as faithful. Merged into whatever
        the header already holds, because the stamp grows as the bundle is written
        and ``write_grade`` runs after ``write_metrics``.
        """
        path = self.output_dir / METRICS_FILENAME
        metrics: dict[str, Any] = {}
        if path.exists():
            carried = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(carried, dict):
                raise ValueError(
                    f"{path} holds {type(carried).__name__} where a mapping belongs, so this "
                    "bundle cannot declare what the redaction policy did to it"
                )
            metrics = carried
        metrics["redaction"] = self._stamp().model_dump(mode="json")
        with open(path, "w") as f:
            yaml.dump(metrics, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    def _stamp(self) -> RedactionStamp:
        return RedactionStamp(
            policy=self.redaction.name,
            artifacts=sorted(self._rewritten),
            omitted=sorted(self._omitted),
        )

    def write_task_info(self, task_config: dict[str, Any]):
        """Write task.yaml — the caller's task snapshot, serialised whole

        Args:
            task_config: The trial's task snapshot. Every key is written, in
                the caller's insertion order; the snapshot the conductor
                composes is the sole definition of the file's shape.
        """
        with open(self.output_dir / "task.yaml", "w") as f:
            yaml.dump(task_config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

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
            "messages": self._redacted_messages(trajectory),
            "user_reply_guard_events": [
                event.model_dump(mode="json") for event in trajectory.user_reply_guard_events
            ],
        }

        with open(self.output_dir / TRAJECTORY_FILENAME, "w") as f:
            yaml.dump(traj_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        self._note_rewritten(TRAJECTORY_FILENAME)

    def _redacted_messages(self, trajectory: Trajectory) -> list[dict[str, Any]]:
        """The message trace as it goes to disk, arguments through the policy.

        The policy runs over the dumped mapping, never over the model: the run
        still holds this trajectory, and the grader has already read it.
        """
        dumped = [message.model_dump(mode="json") for message in trajectory.messages]
        for message in dumped:
            for call in message.get("tool_calls") or []:
                call["arguments"] = self.redaction.redact_arguments(call["arguments"])
        return dumped

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
        record = []
        for call in sorted(trajectory.tool_log, key=lambda call: call.sequence):
            dumped = call.model_dump(mode="json")
            dumped["arguments"] = self.redaction.redact_arguments(dumped["arguments"])
            record.append(dumped)

        with open(self.output_dir / TOOL_LOG_FILENAME, "w", encoding="utf-8") as f:
            yaml.dump(record, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        self._note_rewritten(TOOL_LOG_FILENAME)

    def write_env_state(self, env_state: dict[str, Any]):
        """Write env.yaml with final environment state, through the policy

        The snapshot is a plain mapping the adapter composed, so the same
        key-name rule reaches it — an environment that ends holding the
        credential the agent was issued names it under a key the rule reads.

        Args:
            env_state: Final environment state dictionary
        """
        with open(self.output_dir / ENV_FILENAME, "w") as f:
            yaml.dump(
                self.redaction.redact_arguments(env_state),
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )
        self._note_rewritten(ENV_FILENAME)

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

        if self._redacting:
            metrics_data["redaction"] = self._stamp().model_dump(mode="json")

        with open(self.output_dir / METRICS_FILENAME, "w") as f:
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
        ``prompts.yaml`` split), so it never bloats the grade file. Both judge
        sidecars are withheld under a redacting policy, which is what the *grade
        handed here* determines — the replay path grades a bundle with a grade
        object of its own, so a stamp read off the trajectory would name the
        wrong files. See docs/OUTPUT_FORMAT.md.

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
        # captured a non-empty one. Absent file ⇒ either no judge transcript for
        # this trial, or a redacting policy withheld it; ``metrics.yaml``'s
        # redaction stamp is the discriminator, and names it under ``omitted``.
        # Gate on truthiness, not ``is not None`` — an empty transcript is not
        # worth a sidecar.
        if grade.judge_transcript:
            self._write_judge_sidecar(
                JUDGE_TRAJECTORY_FILENAME, {"messages": grade.judge_transcript}
            )

        # Sidecar: the judge's non-derivable run() inputs (state-diff string +
        # read-tool surface), only when a judge ran. Absent file ⇒ no judge inputs
        # recorded for this trial, or a redacting policy withheld it — same
        # discriminator. Kept out of grade.yaml (the diff can be large); replay
        # reconstructs the judge's opening message from it.
        if grade.judge_inputs:
            self._write_judge_sidecar(
                JUDGE_INPUTS_FILENAME, grade.judge_inputs.model_dump(mode="json")
            )

    def _write_judge_sidecar(self, filename: str, payload: dict[str, Any]) -> None:
        """Write a judge sidecar, or withhold it and declare that instead.

        Both sidecars render the agent's arguments into prose — the transcript
        quotes the calls, ``state_diff_text`` the values — which a key-name rule
        cannot reach, so a redacting policy suppresses them whole.
        """
        if self._redacting:
            self._note_withheld(filename)
            return
        with open(self.output_dir / filename, "w") as f:
            yaml.dump(payload, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

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
