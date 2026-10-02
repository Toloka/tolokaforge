"""Optional half-duplex transition and environment-error budget for a trial.

One participant message is one step. A whole batch of tool replies is one
environment step, regardless of how many calls it contains. The budget is
checked only after that batch, matching the pinned Sierra orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass

from tolokaforge.core.models.trial_status import TerminationReason


@dataclass
class SimulationBudget:
    max_steps: int | None
    max_errors: int | None
    steps: int = 0
    errors: int = 0
    awaiting_environment: bool = False

    def __post_init__(self) -> None:
        if self.max_steps is not None and self.max_steps < 1:
            raise ValueError("max_simulation_steps must be positive")
        if self.max_errors is not None and self.max_errors < 1:
            raise ValueError("max_environment_errors must be positive")
        if self.max_steps is None and self.max_errors is None:
            raise ValueError("simulation budget needs at least one limit")

    def participant(self, *, calls_environment: bool) -> TerminationReason | None:
        if self.awaiting_environment:
            raise RuntimeError("a participant replied before the pending environment batch")
        self.steps += 1
        self.awaiting_environment = calls_environment
        return None if calls_environment else self.reason()

    def environment(self, *, errors: int) -> TerminationReason | None:
        if not self.awaiting_environment:
            raise RuntimeError("an environment batch answered no participant call")
        if errors < 0:
            raise ValueError("environment error count cannot be negative")
        self.steps += 1
        self.errors += errors
        self.awaiting_environment = False
        return self.reason()

    def reason(self) -> TerminationReason | None:
        if self.awaiting_environment:
            return None
        # Check errors first so they win when both limits are reached together.
        if self.max_errors is not None and self.errors >= self.max_errors:
            return TerminationReason.TOO_MANY_ERRORS
        if self.max_steps is not None and self.steps >= self.max_steps:
            return TerminationReason.MAX_STEPS
        return None


class SimulationBudgetReached(Exception):
    """The bootstrap completed a transition at the native budget boundary."""

    def __init__(self, reason: TerminationReason):
        super().__init__(f"simulation ended at {reason.value}")
        self.reason = reason
