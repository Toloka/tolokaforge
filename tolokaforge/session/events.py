"""Event union emitted by a trial to attached participants.

Events are the out-of-trial half of the session Protocol pair. Each variant
carries a discriminating ``kind`` literal plus per-trial ``seq``, ``trial_id``,
and ``timestamp``. Extra fields are forbidden — schemas cross the recorded
transport boundary and land in the trajectory trace under open mode.

See ``docs/OPEN_AGENT_LOOP.md`` §4 for the taxonomy.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from tolokaforge.session._status import TerminationReason, TrialStatus

__all__ = [
    "AssistantMessage",
    "BudgetUpdate",
    "PauseAcknowledged",
    "ResumeAcknowledged",
    "TerminalReached",
    "ToolCallEmitted",
    "ToolResultObserved",
    "TrialEvent",
    "TrialEventEnvelope",
    "TurnStarted",
]


class _EventBase(BaseModel):
    """Common envelope fields for every :class:`TrialEvent` variant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trial_id: str
    seq: int = Field(ge=0, description="Monotonic per-trial event sequence number.")
    timestamp: datetime


class TurnStarted(_EventBase):
    kind: Literal["turn_started"] = "turn_started"
    turn_index: int = Field(ge=0)


class ToolCallEmitted(_EventBase):
    kind: Literal["tool_call_emitted"] = "tool_call_emitted"
    call_id: str
    tool_name: str
    arguments_preview: str = Field(description="Truncated JSON preview of arguments.")


class ToolResultObserved(_EventBase):
    kind: Literal["tool_result_observed"] = "tool_result_observed"
    call_id: str
    tool_name: str
    duration_ms: int = Field(ge=0)
    truncated_preview: str


class AssistantMessage(_EventBase):
    kind: Literal["assistant_message"] = "assistant_message"
    content_preview: str
    has_reasoning: bool = False


class BudgetUpdate(_EventBase):
    kind: Literal["budget_update"] = "budget_update"
    spent_usd: float
    spent_ms: int = Field(ge=0)
    remaining_turns: int


class PauseAcknowledged(_EventBase):
    kind: Literal["pause_acknowledged"] = "pause_acknowledged"
    triggered_by_participant: str


class ResumeAcknowledged(_EventBase):
    kind: Literal["resume_acknowledged"] = "resume_acknowledged"
    triggered_by_participant: str


class TerminalReached(_EventBase):
    kind: Literal["terminal_reached"] = "terminal_reached"
    status: TrialStatus
    termination_reason: TerminationReason | None = None
    final_grade_summary: dict[str, Any] | None = None


TrialEvent = Annotated[
    Union[
        TurnStarted,
        ToolCallEmitted,
        ToolResultObserved,
        AssistantMessage,
        BudgetUpdate,
        PauseAcknowledged,
        ResumeAcknowledged,
        TerminalReached,
    ],
    Field(discriminator="kind"),
]


class TrialEventEnvelope(BaseModel):
    """Wire envelope for :class:`TrialEvent` — used by transports that need
    a single Pydantic model at the boundary (recorded YAML, JSON-lines, WS).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event: TrialEvent
