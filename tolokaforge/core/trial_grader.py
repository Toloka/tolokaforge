"""``TrialGrader`` Protocol — the per-trial grading seam.

The conductor's job (per ``docs/CLOUD_RUNTIME_ARCHITECTURE.md`` §6.3) is to
*trigger* grading, not own it. This module defines the swappable seam:

* :class:`TrialGrader` — Protocol with a single method ``grade`` that maps a
  completed :class:`Trajectory` to a :class:`Grade`. Any conductor holds a
  ``TrialGrader`` and delegates the phase to it.
* :class:`RunnerRPCTrialGrader` — production implementation. Encapsulates
  the three grading strategies the conductor previously carried inline:

  1. ``TrialStatus.ERROR`` / ``TrialStatus.TIMEOUT`` — auto-fail without
     touching the runner (a 429 or similar terminated the trial before any
     work could happen; running the judge on an empty transcript would
     produce a false positive).
  2. ``TerminationReason.STUCK_DETECTED`` — auto-fail. A stuck agent fails
     even if the state hash happens to match the golden.
  3. Otherwise — the runner's ``grade_trial`` gRPC computes state / rule /
     judge components against the golden state and returns a raw dict that
     is parsed into :class:`Grade`.

The Protocol is deliberately narrow. A future :class:`TrialGrader`
implementation may live inside the runner sandbox (per §6.4), speak to a
remote grader service, or route to an entirely different Judge component
(GH #131). None of those variants require touching the conductor.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from tolokaforge.core.models import (
    CriterionResult,
    Grade,
    GradeComponents,
    JudgeStatus,
    JudgeUsage,
    TerminationReason,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.runtime import RuntimeBackend
from tolokaforge.core.trial import TrialSpec

if TYPE_CHECKING:
    from tolokaforge.core.logging import StructuredLogger

__all__ = [
    "RunnerRPCTrialGrader",
    "TrialGrader",
]


@runtime_checkable
class TrialGrader(Protocol):
    """Trial-plane seam for computing a :class:`Grade` from a completed
    :class:`Trajectory`.

    Any conductor holds a ``TrialGrader`` and delegates the grading phase
    to it. The Protocol is intentionally narrow: one method, immutable
    inputs, a value return.

    Implementations may short-circuit on trajectory shape (auto-fail on
    error / timeout / stuck) or dispatch to any grading substrate — an
    in-process rule engine, a runner-side gRPC, or a remote judge
    service. Callers do not need to know which.
    """

    def grade(
        self,
        spec: TrialSpec,
        trajectory: Trajectory,
        agent_system_prompt: str,
    ) -> Grade:
        """Return the :class:`Grade` for a completed trial.

        ``spec`` carries trial identity, per-trial metadata, and the
        runner-side ``spec.task`` projection needed for dispatch.
        ``trajectory`` carries the full message trace, tool log, status
        and termination reason. ``agent_system_prompt`` is the
        post-policy system prompt the judge receives as the agent's
        policy for rubric evaluation.
        """
        ...


class RunnerRPCTrialGrader:
    """Production :class:`TrialGrader`. Dispatches to the runner's
    ``grade_trial`` gRPC for real grading, and short-circuits with an
    auto-fail :class:`Grade` when the trajectory shape rules out a
    meaningful judge result.

    Instantiated per-run with a bound ``runtime_backend`` and the
    per-run :class:`StructuredLogger`. The orchestrator constructs one
    and injects it into every conductor.
    """

    def __init__(self, runtime_backend: RuntimeBackend, logger: StructuredLogger) -> None:
        self.runtime_backend = runtime_backend
        self.logger = logger

    def grade(
        self,
        spec: TrialSpec,
        trajectory: Trajectory,
        agent_system_prompt: str,
    ) -> Grade:
        task_id, trial_idx = _split_trial_id(spec.trial_id)

        if trajectory.status in (TrialStatus.ERROR, TrialStatus.TIMEOUT):
            self.logger.info(
                "Trial did not complete successfully - automatic fail",
                task_id=task_id,
                trial_index=trial_idx,
                status=trajectory.status.value,
            )
            return Grade(
                binary_pass=False,
                score=0.0,
                components=GradeComponents(state_checks=0.0),
                reasons=f"Trial failed with status: {trajectory.status.value}",
            )

        if trajectory.termination_reason == TerminationReason.STUCK_DETECTED:
            self.logger.info(
                "Trial stuck - automatic fail",
                task_id=task_id,
                trial_index=trial_idx,
                termination_reason=trajectory.termination_reason.value,
            )
            return Grade(
                binary_pass=False,
                score=0.0,
                components=GradeComponents(state_checks=0.0),
                reasons="Agent got stuck (repeated actions without progress)",
            )

        llm_messages_json = _build_judge_messages_json(trajectory, agent_system_prompt)
        grade_result = self.runtime_backend.grade_trial(
            trial_id=spec.trial_id, llm_messages_json=llm_messages_json
        )

        if not (grade_result["success"] and grade_result["grade"]):
            error_msg = grade_result.get("error", "Unknown grading error")
            self.logger.error(
                "Grading RPC failed",
                task_id=task_id,
                trial_index=trial_idx,
                error=error_msg,
            )
            return Grade(
                binary_pass=False,
                score=0.0,
                components=GradeComponents(state_checks=0.0),
                reasons=f"Grading RPC failed: {error_msg}",
            )

        grade = _parse_grade_result(grade_result["grade"])
        self.logger.info(
            "Grading via Runner RPC",
            task_id=task_id,
            trial_index=trial_idx,
            score=grade.score,
            binary_pass=grade.binary_pass,
        )
        return grade


def _split_trial_id(trial_id: str) -> tuple[str, int]:
    """Return ``(task_id, trial_index)`` from a canonical ``"{task_id}:{idx}"`` id."""
    task_id, idx_s = trial_id.rsplit(":", 1)
    return task_id, int(idx_s)


def _build_judge_messages_json(
    trajectory: Trajectory,
    agent_system_prompt: str,
) -> str | None:
    """Serialise the transcript + agent policy for the runner-side grading.

    The runner decides whether to actually run the rubric judge based on
    its own grading config; this always serialises the transcript when
    there is one and returns ``None`` for an empty trace.
    """
    if not trajectory.messages and not agent_system_prompt:
        return None

    messages: list[dict[str, Any]] = []
    if agent_system_prompt:
        messages.append({"role": "system", "content": agent_system_prompt})
    for msg in trajectory.messages:
        entry: dict[str, Any] = {
            "role": msg.role.value,
            "content": msg.content or "",
        }
        if msg.tool_calls:
            entry["tool_calls"] = [
                {"function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                for tc in msg.tool_calls
            ]
        if msg.tool_call_id:
            entry["tool_call_id"] = msg.tool_call_id
        messages.append(entry)
    return json.dumps(messages)


def _parse_grade_result(raw_grade: dict[str, Any]) -> Grade:
    """Materialise a :class:`Grade` from the runner's ``grade_trial`` dict.

    Handles optional sub-payloads: ``state_diff_json`` (post-mortem
    diagnostic), ``criterion_results`` (per-criterion breakdown),
    ``judge_report`` (usage + audit transcript when the rubric judge
    ran).
    """
    state_diff_parsed: dict[str, Any] | None = None
    if raw_grade.get("state_diff_json"):
        try:
            state_diff_parsed = json.loads(raw_grade["state_diff_json"])
        except (json.JSONDecodeError, TypeError):
            pass

    criterion_results = None
    raw_criterion_results = raw_grade.get("criterion_results")
    if raw_criterion_results:
        criterion_results = [CriterionResult(**cr) for cr in raw_criterion_results]

    judge_usage: JudgeUsage | None = None
    judge_transcript: list[dict[str, Any]] | None = None
    raw_report = raw_grade.get("judge_report")
    if raw_report:
        judge_usage = JudgeUsage(
            calls=raw_report.get("calls", 0),
            prompt_tokens=raw_report.get("prompt_tokens", 0),
            completion_tokens=raw_report.get("completion_tokens", 0),
            reasoning_tokens=raw_report.get("reasoning_tokens", 0),
            cost_usd=raw_report.get("cost_usd", 0.0),
            tool_calls=raw_report.get("tool_calls", 0),
        )
        raw_transcript = raw_report.get("transcript_json")
        if raw_transcript:
            try:
                parsed = json.loads(raw_transcript)
                if isinstance(parsed, list):
                    judge_transcript = parsed
            except (json.JSONDecodeError, TypeError):
                pass

    return Grade(
        binary_pass=raw_grade["binary_pass"],
        score=raw_grade["score"],
        components=GradeComponents(
            state_checks=raw_grade["components"].get("state_checks", -1.0),
            transcript_rules=raw_grade["components"].get("transcript_rules", -1.0),
            llm_judge=raw_grade["components"].get("llm_judge", -1.0),
            custom_checks=raw_grade["components"].get("custom_checks", -1.0),
        ),
        reasons=raw_grade.get("reasons", ""),
        state_diff=state_diff_parsed,
        criterion_results=criterion_results,
        judge_status=JudgeStatus.from_proto(raw_grade.get("judge_status", 0)),
        judge_usage=judge_usage,
        judge_transcript=judge_transcript,
    )
