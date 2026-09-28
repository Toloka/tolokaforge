"""Project Inspect AI results onto tolokaforge result types.

The delegating adapter lets Inspect execute a task on its own runtime; this module
maps Inspect's ``EvalLog`` (per-sample scores and message history) back onto
tolokaforge's ``Grade`` and ``Trajectory`` so downstream reporting and analysis are
unchanged. The mapping functions duck-type their inputs (they read attributes off
Inspect objects) so they can be unit-tested without constructing a real eval run.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from tolokaforge.core.models import Grade, GradeComponents, Trajectory
from tolokaforge.core.models.trajectory import Message, MessageRole, ToolCall

# Inspect Score.value constants -> (binary_pass, numeric score in [0, 1]).
_VALUE_PASS_SCORE: dict[str, tuple[bool, float]] = {
    "C": (True, 1.0),
    "I": (False, 0.0),
    "P": (False, 0.5),
    "N": (False, 0.0),
}


def _pass_score(value: Any) -> tuple[bool, float]:
    """Map an Inspect score value to ``(binary_pass, numeric)``."""
    key = str(value)
    if key in _VALUE_PASS_SCORE:
        return _VALUE_PASS_SCORE[key]
    try:
        num = float(value)
    except (TypeError, ValueError):
        flag = bool(value)
        return flag, 1.0 if flag else 0.0
    return num >= 1.0, num


def _first_score(sample: Any) -> Any:
    """The first scorer's ``Score`` for a sample, or ``None``."""
    return next(iter((getattr(sample, "scores", None) or {}).values()), None)


def sample_grade(sample: Any) -> Grade:
    """Build a tolokaforge ``Grade`` from one Inspect ``EvalSample``."""
    score = _first_score(sample)
    if score is None:
        return Grade(
            binary_pass=False,
            score=0.0,
            components=GradeComponents(),
            reasons="inspect: sample has no score",
        )
    binary_pass, numeric = _pass_score(score.value)
    detail = getattr(score, "explanation", None) or getattr(score, "reason", None) or ""
    return Grade(
        binary_pass=binary_pass,
        score=numeric,
        components=GradeComponents(custom_checks=numeric),
        reasons=f"inspect score={score.value!r} {detail}".strip(),
    )


def _to_message(message: Any) -> Message:
    """Translate one Inspect chat message into a tolokaforge ``Message``."""
    raw_calls = getattr(message, "tool_calls", None)
    tool_calls = None
    if raw_calls:
        tool_calls = [
            ToolCall(id=c.id, name=c.function, arguments=dict(c.arguments or {})) for c in raw_calls
        ]
    return Message(
        role=MessageRole(message.role),
        content=message.text or "",
        tool_calls=tool_calls,
        tool_call_id=getattr(message, "tool_call_id", None),
    )


def sample_trajectory(sample: Any, *, task_id: str, trial_index: int = 0) -> Trajectory:
    """Reconstruct a tolokaforge ``Trajectory`` from an Inspect ``EvalSample``."""
    now = datetime.now(timezone.utc)
    start = getattr(sample, "started_at", None) or now
    end = getattr(sample, "completed_at", None) or now
    messages = [_to_message(m) for m in (getattr(sample, "messages", None) or [])]
    trajectory = Trajectory(
        task_id=task_id,
        trial_index=trial_index,
        start_ts=start,
        end_ts=end,
        messages=messages,
    )
    trajectory.grade = sample_grade(sample)
    return trajectory


def run_grade(log: Any, *, pass_threshold: float = 1.0) -> Grade:
    """Aggregate a whole Inspect ``EvalLog`` into one tolokaforge ``Grade``.

    One tolokaforge task maps to one Inspect task (which may hold several samples);
    the aggregate score is the mean per-sample score and the pass is that mean at or
    above ``pass_threshold``.
    """
    samples = getattr(log, "samples", None) or []
    scored = [_first_score(s) for s in samples]
    numbers = [_pass_score(s.value)[1] for s in scored if s is not None]
    status = getattr(getattr(log, "status", None), "value", getattr(log, "status", "unknown"))
    if not numbers:
        return Grade(
            binary_pass=False,
            score=0.0,
            components=GradeComponents(),
            reasons=f"inspect: no scored samples (status={status})",
        )
    mean = sum(numbers) / len(numbers)
    return Grade(
        binary_pass=mean >= pass_threshold,
        score=mean,
        components=GradeComponents(custom_checks=mean),
        reasons=f"inspect: {len(numbers)}/{len(samples)} scored, mean={mean:.3f}, status={status}",
    )


def reward_from_log(log: Any, *, pass_threshold: float = 1.0) -> float:
    """The scalar reward for a run: the mean per-sample score in ``[0, 1]``."""
    return run_grade(log, pass_threshold=pass_threshold).score
