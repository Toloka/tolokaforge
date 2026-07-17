"""``LLMIntervener`` — reference LLM participant.

Watches the event stream and, at each natural intervention seam (assistant
turn boundary, terminal), asks the drafter for a :class:`InterventionSuggestion`.
If urgency is ``high`` or ``critical`` and the participant is in
``PARTICIPANT`` / ``ADMIN`` role, submits an :class:`InjectMessage`
intervention. Otherwise the suggestion is logged as an observation.

The situation classifier and calibrated urgency head are compressed into a
single drafter call for M2; splitting them cleanly is an M3 concern.
"""

from __future__ import annotations

from datetime import UTC, datetime

from intervener.participants.base import EventReaction, Participant
from intervener.pipeline import draft_suggestion
from intervener.schema import InterventionSuggestion
from tolokaforge.session import (
    AssistantMessage,
    InjectMessage,
    ParticipantHandle,
    ParticipantRole,
    TerminalReached,
    ToolCallEmitted,
    ToolResultObserved,
    TrialEvent,
    TrialSession,
)

__all__ = ["LLMIntervener"]

_INTERVENE_URGENCIES = {"high", "critical"}


class LLMIntervener(Participant):
    """LLM intervener that proposes interventions at natural turn seams.

    ``auto_inject``: when ``True``, the intervener submits ``InjectMessage``
    interventions on high-urgency suggestions. When ``False`` (recommended
    for RecordedTrialSession demos), suggestions are logged only.
    """

    def __init__(
        self,
        participant_id: str = "llm_intervener",
        role: ParticipantRole = ParticipantRole.PARTICIPANT,
        auto_inject: bool = True,
        drafter_model: str | None = None,
    ) -> None:
        super().__init__(participant_id=participant_id, role=role)
        self._auto_inject = auto_inject
        self._drafter_model = drafter_model
        self._window: list[TrialEvent] = []

    def handle_event(
        self,
        event: TrialEvent,
        handle: ParticipantHandle,
        session: TrialSession,
    ) -> EventReaction:
        self._window.append(event)
        if not self._is_intervention_seam(event):
            return EventReaction()

        suggestion = self._draft(handle.trial_id, event.seq)
        payload = suggestion.model_dump(mode="json")
        note = f"suggestion urgency={suggestion.urgency} situation={suggestion.situation!r}"

        if self._auto_inject and suggestion.urgency in _INTERVENE_URGENCIES:
            intervention = InjectMessage(
                trial_id=handle.trial_id,
                attach_to_seq=event.seq,
                participant_id=handle.participant_id,
                timestamp=datetime.now(UTC),
                content=suggestion.suggested_message,
            )
            return EventReaction(intervention=intervention, note=note, payload=payload)

        return EventReaction(intervention=None, note=note, payload=payload)

    def _is_intervention_seam(self, event: TrialEvent) -> bool:
        return isinstance(
            event, AssistantMessage | ToolResultObserved | ToolCallEmitted | TerminalReached
        )

    def _draft(self, trial_id: str, at_seq: int) -> InterventionSuggestion:
        kwargs = {"model": self._drafter_model} if self._drafter_model else {}
        return draft_suggestion(
            trial_id=trial_id,
            at_seq=at_seq,
            recent_events=list(self._window),
            **kwargs,
        )
