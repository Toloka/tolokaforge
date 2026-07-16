"""``SessionInterventionHandler`` — bridge from :class:`ToolCallingLoop`'s
intervention pump into :class:`InProcessTrialSession`.

Inbound counterpart to :class:`SessionLoopObserver`. Called at every turn
boundary (via :meth:`drain_and_apply`) and before every tool dispatch
(via :meth:`intercept_tool_call`); drains interventions queued by
attached participants, applies each one to the loop's transcript or
returns a decision the loop respects.

Scope today (M1 sub-5a + sub-5b PR A + sub-5b PR B):

* ``InjectMessage`` — applied as a user-role :class:`Message`,
  ``ack_outcome="accepted"``.
* ``Kill`` — returns a :class:`TerminationDecision` with
  :data:`TerminationReason.USER_STOP`; ``ack_outcome="accepted"``.
* ``Pause`` / ``Resume`` — Pause enters a poll loop; Resume exits it.
  Emits ``PauseAcknowledged`` / ``ResumeAcknowledged`` events on the
  session so attached participants see the state transitions. If the
  loop's episode timeout hits while paused, the pump returns a timeout
  decision so the loop terminates cleanly.
* ``ApproveTool`` / ``RejectTool`` — consulted per tool call via
  :meth:`intercept_tool_call`. RejectTool short-circuits the executor
  and the loop synthesizes a tool-error result.
* ``EditState`` — still deferred; recorded as ``rejected`` (runner-side
  mediation not implemented).

**Role priority** (``admin`` > ``participant`` > ``observer``,
later-wins within a tier) applies to Kill selection among concurrent
Kills at the same pause point. Losing Kills are marked
``ack_outcome="superseded"``.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from tolokaforge.core.loop import TerminationDecision, ToolCallDecision
from tolokaforge.core.models import Message, MessageRole, TerminationReason, TrialStatus
from tolokaforge.session.events import PauseAcknowledged, ResumeAcknowledged
from tolokaforge.session.in_process import InProcessTrialSession, QueuedIntervention
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
from tolokaforge.session.protocols import ParticipantRole

__all__ = ["SessionInterventionHandler"]


_NOT_YET_SUPPORTED_REASONS: dict[type[TrialIntervention], str] = {
    EditState: (
        "EditState requires runner-side mediation; deferred until "
        "the runner exposes the edit surface."
    ),
}

_ROLE_PRIORITY: dict[ParticipantRole, int] = {
    ParticipantRole.ADMIN: 0,
    ParticipantRole.PARTICIPANT: 1,
    ParticipantRole.OBSERVER: 2,
}
"""Lower is higher priority. Admin beats participant beats observer;
within a tier, later submission beats earlier (stable-sort preserves
list order for equal keys)."""

_PAUSE_POLL_INTERVAL_S = 0.1
"""How often the pause loop re-drains and re-checks episode timeout.
Small enough that a Resume shows up promptly; large enough that a
5-second pause doesn't spin at 5000 Hz."""


class SessionInterventionHandler:
    """Drains + applies interventions from an :class:`InProcessTrialSession`.

    Instantiated per trial by the orchestrator (via
    :class:`OpenAgentLoopManager`) when open mode is on. Structurally
    satisfies the loop's :class:`InterventionHandler` Protocol.

    Holds a pending set of pre-approved tool-call decisions keyed by
    ``call_id`` — populated when a matching ``ApproveTool`` / ``RejectTool``
    is drained at the turn-boundary pause point. Consulted (and drained
    from) inside :meth:`intercept_tool_call`. This lets participants
    submit tool decisions ahead of when the tool actually fires.
    """

    def __init__(self, session: InProcessTrialSession) -> None:
        self._session = session
        # call_id → (ToolCallDecision, backing intervention). Filled
        # opportunistically when ApproveTool / RejectTool is drained at
        # the turn-boundary pump point; consumed inside intercept_tool_call.
        self._pending_tool_decisions: dict[str, tuple[ToolCallDecision, TrialIntervention]] = {}

    # ------------------------------------------------------------------
    # InterventionHandler Protocol — drain_and_apply
    # ------------------------------------------------------------------

    def drain_and_apply(
        self,
        messages: list[Message],
        check_timeout=lambda: None,
    ) -> TerminationDecision | None:
        """Drain pending interventions, apply supported kinds.

        Kill wins over everything at the same pause point. Pause enters
        the poll loop and only returns when Resume arrives, Kill supersedes,
        or ``check_timeout`` returns a timeout decision.
        """
        return self._drain_and_apply_once(messages, check_timeout)

    def _drain_and_apply_once(
        self,
        messages: list[Message],
        check_timeout,
    ) -> TerminationDecision | None:
        pending = self._session.drain_pending_interventions()
        if not pending:
            return None

        kills = [queued for queued in pending if isinstance(queued.intervention, Kill)]
        if kills:
            winning_kill = self._select_kill_winner(kills)
            for queued in pending:
                if queued is winning_kill:
                    continue
                self._record_superseded_by_kill(queued, winning_kill)
            return self._apply_kill(winning_kill)

        # No Kill — check for Pause. Pause enters poll loop.
        pauses = [queued for queued in pending if isinstance(queued.intervention, Pause)]
        if pauses:
            # Apply any non-terminal, non-Pause interventions first so the
            # trial's transcript reflects everything queued alongside Pause.
            for queued in pending:
                if isinstance(queued.intervention, Pause):
                    continue
                self._apply_non_terminal(queued, messages)
            # Then enter the pause loop with the first-in-order Pause.
            first_pause = pauses[0]
            for extra_pause in pauses[1:]:
                self._record_superseded(
                    extra_pause,
                    reason=(
                        f"Superseded by earlier Pause at the same pause point "
                        f"(participant {first_pause.intervention.participant_id!r})."
                    ),
                )
            return self._enter_pause_loop(first_pause, messages, check_timeout)

        # No terminal or pause intervention — apply the rest in submit order.
        for queued in pending:
            self._apply_non_terminal(queued, messages)
        return None

    # ------------------------------------------------------------------
    # InterventionHandler Protocol — intercept_tool_call
    # ------------------------------------------------------------------

    def intercept_tool_call(
        self,
        call_id: str,
        tool_name: str,
        arguments: dict,
    ) -> ToolCallDecision | None:
        """Consult pending Approve/Reject for ``call_id``.

        If a matching ``ApproveTool`` or ``RejectTool`` was drained at a
        prior pause point, apply it and record ``ack_outcome="accepted"``.
        Otherwise return ``None`` — default approve (executor runs).
        """
        del tool_name, arguments  # not used today; part of the Protocol contract
        entry = self._pending_tool_decisions.pop(call_id, None)
        if entry is None:
            return None
        decision, intervention = entry
        self._session.record_intervention_outcome(intervention, outcome="accepted", reason=None)
        return decision

    # ------------------------------------------------------------------
    # Kill handling
    # ------------------------------------------------------------------

    def _select_kill_winner(self, kills: list[QueuedIntervention]) -> QueuedIntervention:
        indexed = list(enumerate(kills))
        indexed.sort(
            key=lambda pair: (
                _ROLE_PRIORITY.get(pair[1].handle.role, len(_ROLE_PRIORITY)),
                -pair[0],
            )
        )
        return indexed[0][1]

    def _apply_kill(self, queued: QueuedIntervention) -> TerminationDecision:
        assert isinstance(queued.intervention, Kill)
        self._session.record_intervention_outcome(
            queued.intervention, outcome="accepted", reason=None
        )
        return TerminationDecision(
            reason=TerminationReason.USER_STOP,
            system_message=(
                f"Trial killed by participant {queued.intervention.participant_id!r}: "
                f"{queued.intervention.reason}"
            ),
            status=TrialStatus.FAILED,
        )

    def _record_superseded_by_kill(
        self, queued: QueuedIntervention, winning_kill: QueuedIntervention
    ) -> None:
        self._session.record_intervention_outcome(
            queued.intervention,
            outcome="superseded",
            reason=(
                f"Superseded by Kill from participant "
                f"{winning_kill.intervention.participant_id!r} at the same pause point."
            ),
        )

    def _record_superseded(self, queued: QueuedIntervention, reason: str) -> None:
        self._session.record_intervention_outcome(
            queued.intervention, outcome="superseded", reason=reason
        )

    # ------------------------------------------------------------------
    # Pause / Resume state machine
    # ------------------------------------------------------------------

    def _enter_pause_loop(
        self,
        pause_queued: QueuedIntervention,
        messages: list[Message],
        check_timeout,
    ) -> TerminationDecision | None:
        """Block the loop's producer thread until Resume, Kill, or timeout.

        Publishes ``PauseAcknowledged`` on entry and ``ResumeAcknowledged``
        on normal exit. On Kill mid-pause, publishes ResumeAcknowledged
        (the pause ended, just not on the participant's terms) then returns
        the Kill's termination decision. On timeout, terminates with the
        timeout decision.
        """
        pause_intervention = pause_queued.intervention
        self._session.record_intervention_outcome(
            pause_intervention, outcome="accepted", reason=None
        )
        self._session.publish(
            PauseAcknowledged(
                trial_id=self._session.trial_id,
                seq=self._session.next_seq(),
                timestamp=_now(),
                triggered_by_participant=pause_intervention.participant_id,
            )
        )

        while True:
            time.sleep(_PAUSE_POLL_INTERVAL_S)

            timeout_decision = check_timeout()
            if timeout_decision is not None:
                self._publish_resume_ack(pause_intervention.participant_id)
                return timeout_decision

            pending = self._session.drain_pending_interventions()
            if not pending:
                continue

            # Kill wins even inside a pause.
            kills = [q for q in pending if isinstance(q.intervention, Kill)]
            if kills:
                winning_kill = self._select_kill_winner(kills)
                for q in pending:
                    if q is winning_kill:
                        continue
                    self._record_superseded_by_kill(q, winning_kill)
                self._publish_resume_ack(pause_intervention.participant_id)
                return self._apply_kill(winning_kill)

            resumes = [q for q in pending if isinstance(q.intervention, Resume)]
            if resumes:
                # First Resume wins; extras (unlikely) marked superseded.
                first_resume = resumes[0]
                self._session.record_intervention_outcome(
                    first_resume.intervention, outcome="accepted", reason=None
                )
                for extra in resumes[1:]:
                    self._record_superseded(
                        extra,
                        reason="Superseded by an earlier Resume at the same wake-up.",
                    )
                # Apply any non-Resume, non-Kill interventions that came in
                # alongside the Resume (e.g. an Inject the operator queued
                # to fire on wake-up).
                for q in pending:
                    if isinstance(q.intervention, (Resume, Kill)):
                        continue
                    self._apply_non_terminal(q, messages)
                self._publish_resume_ack(first_resume.intervention.participant_id)
                return None

            # No Kill, no Resume — buffer everything else as superseded
            # (a paused trial shouldn't accept mutations until Resume
            # brings it back).
            for q in pending:
                if isinstance(q.intervention, Pause):
                    self._record_superseded(q, reason="Already paused; extra Pause ignored.")
                else:
                    self._record_superseded(
                        q,
                        reason=(
                            f"Trial is paused (by participant "
                            f"{pause_intervention.participant_id!r}); "
                            "intervention buffered as superseded."
                        ),
                    )

    def _publish_resume_ack(self, triggered_by: str) -> None:
        self._session.publish(
            ResumeAcknowledged(
                trial_id=self._session.trial_id,
                seq=self._session.next_seq(),
                timestamp=_now(),
                triggered_by_participant=triggered_by,
            )
        )

    # ------------------------------------------------------------------
    # Non-terminal intervention application
    # ------------------------------------------------------------------

    def _apply_non_terminal(self, queued: QueuedIntervention, messages: list[Message]) -> None:
        intervention = queued.intervention
        if isinstance(intervention, InjectMessage):
            messages.append(
                Message(
                    role=MessageRole.USER,
                    content=intervention.content,
                    ts=_now(),
                )
            )
            self._session.record_intervention_outcome(intervention, outcome="accepted", reason=None)
            return

        if isinstance(intervention, ApproveTool):
            self._pending_tool_decisions[intervention.call_id] = (
                ToolCallDecision(action="approve", reason=intervention.reason),
                intervention,
            )
            # Outcome recorded when intercept_tool_call fires; queued for now
            return

        if isinstance(intervention, RejectTool):
            self._pending_tool_decisions[intervention.call_id] = (
                ToolCallDecision(action="reject", reason=intervention.reason),
                intervention,
            )
            return

        if isinstance(intervention, Resume):
            # A Resume outside a pause loop is a no-op — nothing to resume from.
            self._session.record_intervention_outcome(
                intervention,
                outcome="rejected",
                reason="Resume received while trial is not paused; no state to change.",
            )
            return

        reason = _NOT_YET_SUPPORTED_REASONS.get(
            type(intervention),
            f"Intervention kind {intervention.kind!r} not handled by the M1 pump.",
        )
        self._session.record_intervention_outcome(intervention, outcome="rejected", reason=reason)


def _now() -> datetime:
    return datetime.now(UTC)
