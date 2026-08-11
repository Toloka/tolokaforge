"""What a stub :class:`Orchestrator` publishes about its own completeness.

The CLI reads ``Orchestrator.grading_completeness`` directly after a run — no
``getattr`` default, because a default would let an orchestrator that never
computed completeness report a complete run. Every module that monkeypatches
``Orchestrator`` therefore has to satisfy that read, and does so through here so
the shape lives in one place rather than in ten copies.
"""

from __future__ import annotations

from tolokaforge.core.orchestrator import GradingCompleteness


def complete_run(total_attempts: int = 0) -> GradingCompleteness:
    """A run that produced a verdict for every attempt it made.

    The default of zero attempts is what a stub whose ``run()`` only returns a
    path honestly ran. A test asserting about the gate itself passes the count
    it means.
    """
    return GradingCompleteness(total_attempts=total_attempts, ungradeable_trial_ids=())
