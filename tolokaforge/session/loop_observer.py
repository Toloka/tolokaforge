"""``SessionLoopObserver`` — bridge from :class:`ToolCallingLoop` to :class:`TrialSession`.

The ``LoopObserver`` seam in :mod:`tolokaforge.core.loop` is deliberately
loop-shaped (turn start, assistant message, tool call, tool result,
terminal). This bridge translates each callback into the corresponding
:class:`~tolokaforge.session.TrialEvent` variant and publishes it onto an
:class:`~tolokaforge.session.InProcessTrialSession`, so a live trial's
event stream reaches attached participants in ``seq`` order.

The observer never blocks the producer — publishes are non-blocking queue
puts (see :class:`InProcessTrialSession.publish`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tolokaforge.core.models import TerminationReason
from tolokaforge.core.models import TrialStatus as _CoreTrialStatus
from tolokaforge.session._status import (
    TerminationReason as SessionTerminationReason,
)
from tolokaforge.session._status import (
    TrialStatus as SessionTrialStatus,
)
from tolokaforge.session.events import (
    AssistantMessage,
    TerminalReached,
    ToolCallEmitted,
    ToolResultObserved,
    TurnStarted,
)
from tolokaforge.session.in_process import InProcessTrialSession

__all__ = ["SessionLoopObserver"]

_PREVIEW_LEN = 240


class SessionLoopObserver:
    """Bridge a :class:`ToolCallingLoop`'s seams into
    :class:`InProcessTrialSession` events.

    Instantiated per trial by the code that assembles the loop (M1 sub-3
    wires this into :class:`~tolokaforge.core.runner.TrialRunner`). Holds
    a reference to the session and stamps each event with a fresh ``seq``
    at publish time.
    """

    def __init__(self, session: InProcessTrialSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # LoopObserver Protocol (satisfied structurally; no explicit inherit)
    # ------------------------------------------------------------------

    def on_turn_start(self, turn_index: int) -> None:
        self._session.publish(
            TurnStarted(
                trial_id=self._session.trial_id,
                seq=self._session.next_seq(),
                timestamp=_now(),
                turn_index=turn_index,
            )
        )

    def on_assistant_message(self, content: str, has_reasoning: bool) -> None:
        self._session.publish(
            AssistantMessage(
                trial_id=self._session.trial_id,
                seq=self._session.next_seq(),
                timestamp=_now(),
                content_preview=_preview(content),
                has_reasoning=has_reasoning,
            )
        )

    def on_tool_call(self, call_id: str, tool_name: str, arguments: dict[str, Any]) -> None:
        self._session.publish(
            ToolCallEmitted(
                trial_id=self._session.trial_id,
                seq=self._session.next_seq(),
                timestamp=_now(),
                call_id=call_id,
                tool_name=tool_name,
                arguments_preview=_preview(_stringify_arguments(arguments)),
            )
        )

    def on_tool_result(
        self, call_id: str, tool_name: str, duration_ms: int, output: str, success: bool
    ) -> None:
        # ``success`` is not part of the event payload today — it's carried
        # implicitly by the transcript. Kept as a parameter so the observer
        # protocol can grow into a richer event variant without a signature
        # change here (M4/M5).
        del success
        self._session.publish(
            ToolResultObserved(
                trial_id=self._session.trial_id,
                seq=self._session.next_seq(),
                timestamp=_now(),
                call_id=call_id,
                tool_name=tool_name,
                duration_ms=duration_ms,
                truncated_preview=_preview(output),
            )
        )

    def on_terminal(
        self, status: _CoreTrialStatus, termination_reason: TerminationReason | None
    ) -> None:
        self._session.publish(
            TerminalReached(
                trial_id=self._session.trial_id,
                seq=self._session.next_seq(),
                timestamp=_now(),
                status=_translate_status(status),
                termination_reason=_translate_reason(termination_reason),
            )
        )


def _now() -> datetime:
    return datetime.now(UTC)


def _preview(text: str) -> str:
    if len(text) <= _PREVIEW_LEN:
        return text
    return text[: _PREVIEW_LEN - 1] + "…"


def _stringify_arguments(arguments: dict[str, Any]) -> str:
    if not arguments:
        return "{}"
    import json

    try:
        return json.dumps(arguments, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(arguments)


def _translate_status(status: _CoreTrialStatus) -> SessionTrialStatus:
    return SessionTrialStatus(status.value)


def _translate_reason(reason: TerminationReason | None) -> SessionTerminationReason | None:
    if reason is None:
        return None
    return SessionTerminationReason(reason.value)
