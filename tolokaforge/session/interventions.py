"""Intervention union submitted by an attached participant into a trial.

Interventions are the into-trial half of the session Protocol pair. Each
variant carries a discriminating ``kind`` literal, the ``attach_to_seq`` it
responds to (for correlation and idempotency), the submitting
``participant_id``, and a ``timestamp``. Extra fields are forbidden.

See ``docs/OPEN_AGENT_LOOP.md`` §4 for the taxonomy.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ApproveTool",
    "EditState",
    "InjectMessage",
    "InterventionAck",
    "Kill",
    "Pause",
    "RejectTool",
    "Resume",
    "TrialIntervention",
]


class _InterventionBase(BaseModel):
    """Common envelope fields for every :class:`TrialIntervention` variant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trial_id: str
    attach_to_seq: int = Field(
        ge=0,
        description="The event `seq` this intervention responds to; used for idempotency.",
    )
    participant_id: str
    timestamp: datetime


class InjectMessage(_InterventionBase):
    kind: Literal["inject_message"] = "inject_message"
    content: str = Field(
        min_length=1, description="Prepended to the next agent turn as a user-role message."
    )


class ApproveTool(_InterventionBase):
    kind: Literal["approve_tool"] = "approve_tool"
    call_id: str
    reason: str | None = None


class RejectTool(_InterventionBase):
    kind: Literal["reject_tool"] = "reject_tool"
    call_id: str
    reason: str | None = None


class Pause(_InterventionBase):
    kind: Literal["pause"] = "pause"
    reason: str | None = None


class Resume(_InterventionBase):
    kind: Literal["resume"] = "resume"


class Kill(_InterventionBase):
    kind: Literal["kill"] = "kill"
    reason: str = Field(
        min_length=1, description="Recorded in the trajectory as the termination cause."
    )


class EditState(_InterventionBase):
    """Narrow, mediated request to edit sandbox state — the runner substrate
    decides whether the edit is legal for this trial and which keys are writable.
    """

    kind: Literal["edit_state"] = "edit_state"
    state_key: str = Field(min_length=1)
    new_value: Any


TrialIntervention = Annotated[
    Union[
        InjectMessage,
        ApproveTool,
        RejectTool,
        Pause,
        Resume,
        Kill,
        EditState,
    ],
    Field(discriminator="kind"),
]


class InterventionAck(BaseModel):
    """Result of :meth:`TrialInterventions.submit` — records the outcome of an
    intervention attempt so participants know whether it was accepted, deferred
    to a later pause point, superseded by a higher-priority participant, or
    rejected for role reasons.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    intervention_kind: str
    trial_id: str
    participant_id: str
    outcome: Literal["accepted", "queued", "superseded", "rejected"]
    reason: str | None = None
