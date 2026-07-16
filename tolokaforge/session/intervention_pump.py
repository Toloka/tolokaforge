"""``SessionInterventionHandler`` — bridge from :class:`ToolCallingLoop`'s
intervention pump into :class:`InProcessTrialSession`.

Inbound counterpart to :class:`SessionLoopObserver`. Called at every turn
boundary; drains interventions queued by attached participants, applies
each one to the loop's transcript (``InjectMessage`` today), and updates
the durable trace with the accept/reject verdict.

Sub-5a scope: **InjectMessage only.** Every other kind is recorded with
``outcome="rejected"`` and a reason noting the M1 sub-issue that will land
its handling. This keeps the schema surface honest — the trace tells the
full story of what was proposed and what actually happened.
"""

from __future__ import annotations

from datetime import UTC, datetime

from tolokaforge.core.models import Message, MessageRole
from tolokaforge.session.in_process import InProcessTrialSession
from tolokaforge.session.interventions import (
    ApproveTool,
    EditState,
    InjectMessage,
    Kill,
    Pause,
    RejectTool,
    Resume,
    TrialIntervention,
)

__all__ = ["SessionInterventionHandler"]


_NOT_YET_SUPPORTED_REASONS: dict[type[TrialIntervention], str] = {
    Kill: "Kill intervention not yet applied by the pump (M1 sub-5b).",
    Pause: "Pause intervention not yet applied by the pump (M1 sub-5b).",
    Resume: "Resume intervention not yet applied by the pump (M1 sub-5b).",
    ApproveTool: (
        "ApproveTool intervention applies at the tool-call seam, "
        "not the turn-boundary pause point (M1 sub-5b)."
    ),
    RejectTool: (
        "RejectTool intervention applies at the tool-call seam, "
        "not the turn-boundary pause point (M1 sub-5b)."
    ),
    EditState: (
        "EditState requires runner-side mediation; deferred until "
        "the runner exposes the edit surface."
    ),
}


class SessionInterventionHandler:
    """Drains + applies interventions from an :class:`InProcessTrialSession`.

    Instantiated per trial by the orchestrator when open mode is on.
    Structurally satisfies the loop's :class:`InterventionHandler` Protocol.
    """

    def __init__(self, session: InProcessTrialSession) -> None:
        self._session = session

    def drain_and_apply(self, messages: list[Message]) -> None:
        """Drain the session's pending interventions and apply supported kinds
        to ``messages``. Records the outcome of each on the session's
        durable trace history.
        """
        pending = self._session.drain_pending_interventions()
        for queued in pending:
            intervention = queued.intervention
            if isinstance(intervention, InjectMessage):
                messages.append(
                    Message(
                        role=MessageRole.USER,
                        content=intervention.content,
                        ts=datetime.now(UTC),
                    )
                )
                self._session.record_intervention_outcome(
                    intervention, outcome="accepted", reason=None
                )
                continue

            reason = _NOT_YET_SUPPORTED_REASONS.get(
                type(intervention),
                f"Intervention kind {intervention.kind!r} not handled by the M1 pump.",
            )
            self._session.record_intervention_outcome(
                intervention, outcome="rejected", reason=reason
            )
