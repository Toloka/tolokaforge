"""``build_coach`` — assembles a :class:`ComposedParticipant` from a
:class:`CoachConfig`.

The coach is a straightforward composition:

* one :class:`RollingEventsSink` — feeds the detector its history
* one custom :class:`_CoachController` — event-reactive: on each event,
  calls detector.check(); on trigger, calls intervener.build() and
  submits the result; enforces the cooldown
* one :class:`CoachReport` — bookkeeping for the run's audit trail

The `_CoachController` also observes events (implements ``EventSink``)
so the drain loop delivers events to it on the drain thread — no extra
thread needed.
"""

from __future__ import annotations

import threading
from collections import deque

from intervener import (
    ComposedParticipant,
    RollingEventsSink,
)
from intervener.binding import SessionBinding
from intervener.tools.base import LLMCallable

from coach.config import CoachConfig
from coach.cost_tracker import CoachReport, CostTrackingLLMCall
from coach.detectors import build_detector
from coach.interveners import build_intervener
from tolokaforge.session import (
    ParticipantRole,
    TerminalReached,
    TrialEvent,
)

__all__ = ["build_coach"]


_ROLE_LOOKUP = {
    "observer": ParticipantRole.OBSERVER,
    "participant": ParticipantRole.PARTICIPANT,
    "admin": ParticipantRole.ADMIN,
}


class _CoachController:
    """Event-reactive controller that runs the detector + intervener chain.

    Also implements EventSink so the ComposedParticipant drain loop
    delivers events to it — no separate thread needed.
    """

    def __init__(
        self,
        config: CoachConfig,
        report: CoachReport,
        llm_call: LLMCallable | None,
    ) -> None:
        self._config = config
        self._report = report
        self._detector = build_detector(config.detector, llm_call=llm_call)
        self._intervener = build_intervener(config.intervener, llm_call=llm_call)
        self._history: deque[TrialEvent] = deque(maxlen=400)
        self._cooldown_remaining = 0
        self._binding: SessionBinding | None = None

    # --- EventSink half ---
    def on_event(self, event: TrialEvent) -> None:
        self._history.append(event)
        if self._binding is None:
            return
        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1
            return
        trigger = self._detector.check(event, list(self._history))
        if trigger is None:
            return
        self._report.record_trigger(trigger.detector, trigger.reason, trigger.at_seq)
        intervention = self._intervener.build(trigger, self._binding, list(self._history))
        if intervention is None:
            return
        ack = self._binding.submit(intervention)
        self._report.record_submission(intervention.kind, ack.outcome)
        self._cooldown_remaining = self._config.cooldown_turns

    def on_terminal(self) -> None:
        return None

    # --- InputController half ---
    def start(self, binding: SessionBinding, terminal: threading.Event) -> None:
        self._binding = binding

    def stop(self) -> None:
        self._binding = None


def build_coach(
    config: CoachConfig,
    trial_id: str,
    llm_call: LLMCallable | None = None,
    llm_model_key: str = "default",
) -> tuple[ComposedParticipant, CoachReport]:
    """Assemble a coach participant + its per-trial report.

    Args:
        config: the coach's YAML-loaded spec.
        trial_id: the trial this coach is bound to (for reporting).
        llm_call: caller-supplied LLMCallable. Wrapped in a
            `CostTrackingLLMCall` internally so the coach's LLM spend
            lands in ``coach_report.yaml``. Pass ``None`` to force
            heuristic-only detectors + interveners.
        llm_model_key: pricing-table key for the coach's LLM (e.g.
            "claude-haiku-4.5"). See `CostTrackingLLMCall` for the
            table. Only used when `llm_call` is not None.

    Returns:
        ``(participant, report)`` — spawn ``participant.run(session)`` on
        a background thread; write ``report`` to disk after the trial.
    """
    report = CoachReport(
        trial_id=trial_id,
        coach_id=config.participant_id,
        detector_type=config.detector.type,
        intervener_type=config.intervener.type,
    )

    tracked_llm: LLMCallable | None = None
    if llm_call is not None:
        tracked_llm = CostTrackingLLMCall(
            inner=llm_call,
            report=report,
            model_key=llm_model_key,
            budget_usd=config.budget_usd,
        )

    controller = _CoachController(config=config, report=report, llm_call=tracked_llm)

    participant = ComposedParticipant(
        participant_id=config.participant_id,
        role=_ROLE_LOOKUP[config.role],
        sinks=[RollingEventsSink(maxlen=400)],
        controllers=[controller],
    )
    return participant, report


# For the type-check nudges around TerminalReached — imported to keep
# the symbol reachable if a future extension needs to react to it.
_ = TerminalReached
