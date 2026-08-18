"""The window a trial's assistant-turn count is graded against.

A stdlib-only predicate over the two author keys that bound the counter, so
``TranscriptRulesConfig`` states the rule once and every construction path — a
``grading.yaml`` load and ``RegisterTrial`` alike — rejects the same windows in
the same words.
"""

from __future__ import annotations


def validate_turn_window(
    *,
    min_assistant_turns: int | None,
    max_turns: int | None,
    context: str,
) -> None:
    """Raise ``ValueError`` naming ``context`` when the declared window is empty.

    A floor above the ceiling admits no assistant-turn count at all: every trial
    fails one of the two bounds, so the whole ``transcript_rules`` component is
    ``0.0`` however the agent behaves. Either key on its own bounds one side and
    always admits some count, so only a pack declaring both can close the window.
    """
    if min_assistant_turns is None or max_turns is None:
        return
    if min_assistant_turns <= max_turns:
        return
    raise ValueError(
        f"{context} declares an unsatisfiable turn window: min_assistant_turns "
        f"({min_assistant_turns}) is above max_turns ({max_turns}), so no assistant-turn "
        f"count satisfies both bounds and every trial fails the transcript component. "
        f"Lower min_assistant_turns to at most {max_turns}, or raise max_turns to at "
        f"least {min_assistant_turns}."
    )
