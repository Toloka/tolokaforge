"""One run directory holding every trial shape the harness writes today.

A judged trial, a state-only one, one the run refused to grade, one aborted after
the agent ran, and one whose environment never came up — the last written by the
production path (:mod:`tests.utils.provision_failure`). The four that reached the
conductor carry a ``task.yaml`` declaring both an ``llm_judge`` rubric and a
``trace_checks`` block, so the same directory is a complete corpus for both
offline commands and the two can be compared bundle for bundle.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests.canonical._factories import make_trajectory, make_trial_messages
from tests.utils.provision_failure import write_provision_failure_bundle
from tests.utils.recorded_calls import recorded_call
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    JudgeStatus,
    TerminationReason,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.output.artifacts import FileArtifactWriter

RUBRIC: dict[str, Any] = {
    "reference": "The refund must be issued for $328.50.",
    "criteria": [{"id": "refund_amount", "description": "Refund quotes $328.50", "kind": "binary"}],
}
TRACE_CHECKS: dict[str, Any] = {
    "constraints": [
        {
            "id": "the_order_was_looked_up",
            "description": "the agent read the order before answering",
            "require": {
                "present": {"match": {"kind": "tool_call", "tool": {"equals": "get_order"}}}
            },
        }
    ]
}
JUDGE_MODEL = {"provider": "openrouter", "name": "openai/gpt-4.1-mini", "temperature": 0.0}
GRADING_REFUSAL = "judge returned no verdict after 3 attempts"
_TURNS = ("I want a refund for order O-1.", "Reading the order.")


@dataclass(frozen=True)
class FiveShapeRun:
    """The five bundles, named by the shape each one is."""

    judged: Path
    state_only: Path
    ungradeable: Path
    aborted: Path
    provision_failure: Path

    @property
    def bundles(self) -> list[Path]:
        return sorted(
            [self.judged, self.state_only, self.ungradeable, self.aborted, self.provision_failure]
        )


def _trajectory(task_id: str, **recorded: Any) -> Trajectory:
    call = recorded_call(
        "get_order", sequence=0, arguments={"id": "O-1"}, output='{"total": 328.5}'
    )
    return make_trajectory(
        task_id=task_id,
        messages=make_trial_messages([call], _TURNS),
        tool_log=[call],
        **recorded,
    )


def _write(root: Path, name: str, trajectory: Trajectory, grade: Grade | None) -> Path:
    bundle = root / "trials" / name / "0"
    writer = FileArtifactWriter()
    writer.write_trajectory(bundle, trajectory)
    writer.write_tool_log(bundle, trajectory)
    writer.write_metrics(bundle, trajectory)
    writer.write_prompts(bundle, "You are the agent.", "user-sim prompt")
    writer.write_task(
        bundle,
        {
            "task_id": trajectory.task_id,
            "trial_index": 0,
            "grading_config": {"llm_judge": {"rubric": RUBRIC}, "trace_checks": TRACE_CHECKS},
            "model_config": {"judge": JUDGE_MODEL},
        },
    )
    if grade is not None:
        writer.write_grade(bundle, grade)
    return bundle


def _grade(judge_status: JudgeStatus) -> Grade:
    return Grade(
        binary_pass=True,
        score=1.0,
        components=GradeComponents(llm_judge=1.0),
        judge_status=judge_status,
    )


def write_five_shape_run(root: Path) -> FiveShapeRun:
    """Write the five shapes under ``root`` and return where each one landed."""
    return FiveShapeRun(
        judged=_write(root, "judged", _trajectory("judged"), _grade(JudgeStatus.COMPLETED)),
        state_only=_write(
            root, "state_only", _trajectory("state_only"), _grade(JudgeStatus.UNSPECIFIED)
        ),
        ungradeable=_write(
            root, "ungradeable", _trajectory("ungradeable", grading_error=GRADING_REFUSAL), None
        ),
        aborted=_write(
            root,
            "aborted",
            _trajectory(
                "aborted",
                status=TrialStatus.ERROR,
                termination_reason=TerminationReason.API_TIMEOUT,
            ),
            None,
        ),
        provision_failure=write_provision_failure_bundle(root, task_id="provision_failure"),
    )
