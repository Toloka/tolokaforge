"""``SessionInterventionHandler`` — bridge from :class:`ToolCallingLoop`'s
intervention pump into :class:`InProcessTrialSession`.

Inbound counterpart to :class:`SessionLoopObserver`. Called at every turn
boundary; drains interventions queued by attached participants, applies
each one to the loop's transcript or returns a :class:`TerminationDecision`
that terminates the loop, and updates the durable trace with the accept
/ reject verdict.

Scope today (M1 sub-5a + PR A of sub-5b):

* ``InjectMessage`` — applied as a user-role :class:`Message` on the
  transcript, ``ack_outcome="accepted"``.
* ``Kill`` — returns a :class:`TerminationDecision` with
  :data:`TerminationReason.USER_STOP` and the submitting participant's
  ``reason`` verbatim in the ``system_message``. ``ack_outcome="accepted"``.
* **Role priority** (``admin`` > ``participant`` > ``observer``,
  later-wins within a tier) applies when multiple ``Kill`` interventions
  arrive at the same pause point; the losers are marked
  ``ack_outcome="superseded"`` in the trace. Non-terminal interventions
  drained alongside a winning Kill are also marked ``superseded`` —
  a killed trial does not accept any further message injections.

Deferred kinds (PR B of sub-5b — Pause / Resume / ApproveTool /
RejectTool / EditState) still land in the trace as ``rejected`` with a
per-kind reason.
"""

from __future__ import annotations

from datetime import UTC, datetime

from tolokaforge.core.loop import TerminationDecision
from tolokaforge.core.models import Message, MessageRole, TerminationReason, TrialStatus
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
    Pause: "Pause intervention not yet applied by the pump (M1 sub-5b PR B).",
    Resume: "Resume intervention not yet applied by the pump (M1 sub-5b PR B).",
    ApproveTool: (
        "ApproveTool intervention applies at the tool-call seam, "
        "not the turn-boundary pause point (M1 sub-5b PR B)."
    ),
    RejectTool: (
        "RejectTool intervention applies at the tool-call seam, "
        "not the turn-boundary pause point (M1 sub-5b PR B)."
    ),
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
within a tier, later submission beats earlier (list order preserved
after stable-sort)."""


class SessionInterventionHandler:
    """Drains + applies interventions from an :class:`InProcessTrialSession`.

    Instantiated per trial by the orchestrator when open mode is on.
    Structurally satisfies the loop's :class:`InterventionHandler` Protocol.
    """

    def __init__(self, session: InProcessTrialSession) -> None:
        self._session = session

    def drain_and_apply(self, messages: list[Message]) -> TerminationDecision | None:
        """Drain the session's pending interventions and apply supported kinds.

        Kill wins over everything at the same pause point — if any Kill was
        submitted, it terminates the trial and every other drained
        intervention (including InjectMessages) is marked ``superseded``.
        Otherwise, InjectMessages append to ``messages`` in submit order and
        unsupported kinds land in the trace as ``rejected`` with a per-kind
        reason.

        Returns a :class:`TerminationDecision` on Kill, ``None`` otherwise.
        """
        pending = self._session.drain_pending_interventions()
        if not pending:
            return None

        kills = [queued for queued in pending if isinstance(queued.intervention, Kill)]
        if kills:
            winning_kill = self._select_kill_winner(kills)
            # Every non-winning drained intervention (including all
            # non-Kill kinds) is superseded — a killed trial does not
            # accept further pump-side effects.
            for queued in pending:
                if queued is winning_kill:
                    continue
                self._record_superseded(queued, winning_kill)
            return self._apply_kill(winning_kill)

        # No terminal intervention — apply the rest in submit order.
        for queued in pending:
            self._apply_non_terminal(queued, messages)
        return None

    # ------------------------------------------------------------------
    # Kill handling
    # ------------------------------------------------------------------

    def _select_kill_winner(self, kills: list[QueuedIntervention]) -> QueuedIntervention:
        """Pick the highest-priority Kill; within a tier, later submission wins.

        Python's ``sorted`` is stable, so preserving submit-order within a
        priority tier is automatic after sorting by role. Reversing then
        taking the first entry yields the last-submitted of the top-priority
        tier.
        """
        # Sort by role ascending (admin=0 first), then by submit order
        # (index in the drained list — original order is submit order).
        indexed = list(enumerate(kills))
        indexed.sort(
            key=lambda pair: (
                _ROLE_PRIORITY.get(pair[1].handle.role, len(_ROLE_PRIORITY)),
                -pair[0],  # later index (= later submission) beats earlier within a tier
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

    def _record_superseded(
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
                    ts=datetime.now(UTC),
                )
            )
            self._session.record_intervention_outcome(intervention, outcome="accepted", reason=None)
            return

        reason = _NOT_YET_SUPPORTED_REASONS.get(
            type(intervention),
            f"Intervention kind {intervention.kind!r} not handled by the M1 pump.",
        )
        self._session.record_intervention_outcome(intervention, outcome="rejected", reason=reason)
