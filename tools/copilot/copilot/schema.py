"""Output schema for the LLM copilot.

``CopilotSuggestion`` is what the LLM participant produces per attempt — a
situation classification, an urgency band, a suggested next message, and any
alternatives. Consumed by the demo driver for display and by any downstream
process that wants to log or route copilot outputs.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["CopilotSuggestion", "Urgency"]

Urgency = Literal["none", "low", "medium", "high", "critical"]


class CopilotSuggestion(BaseModel):
    """Structured recommendation the LLM copilot emits when it observes a
    situation worth intervening on. Matches the shape shown in
    ``docs/OPEN_AGENT_LOOP.md`` §7 and the DS413 pitch preview.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    trial_id: str
    at_seq: int = Field(
        ge=0,
        description="The event seq the copilot reacted to when producing this suggestion.",
    )
    situation: str = Field(
        min_length=1,
        description="One-sentence characterisation of what the agent is doing.",
    )
    urgency: Urgency
    urgency_score: float = Field(ge=0.0, le=1.0)
    suggested_message: str = Field(
        min_length=1,
        description="Message the copilot would inject as the next user-role turn.",
    )
    alternative_suggestions: list[str] = Field(default_factory=list)
    rationale: str = Field(
        default="",
        description="Free-form model reasoning behind the suggestion. May be empty.",
    )
