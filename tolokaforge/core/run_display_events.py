"""Per-trial lifecycle event Protocol emitted by the runner into a display.

Lives in ``tolokaforge.core`` (not ``tolokaforge.cli``) so the orchestrator,
conductor, and trial runner import the Protocol without dragging the CLI
+ Rich dependency graph into engine-side code paths (worker container,
gRPC runner, cloud-runtime trial-plane).

The concrete Rich-bound consumer is :class:`tolokaforge.cli._run_display.LiveRunDisplay`.
The Protocol has a no-op default (:data:`_NULL_EVENTS`), so callers that
never build a display can still thread ``events`` through without
conditional branches.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class RunDisplayEvents(Protocol):
    """Per-trial lifecycle events the runner emits into a display.

    Every method is kwarg-only so a future field addition does not break
    positional callers. Implementations must not raise — a raise would
    corrupt the runner loop. :data:`_NULL_EVENTS` is the default sink.
    """

    def run_started(self, *, total_trials: int, initial_completed: int) -> None:
        """Fired once when the orchestrator has primed its trial queue."""

    def trial_started(self, *, trial_id: str, task_id: str, trial_index: int) -> None:
        """Fired when a worker leases a trial and enters provisioning."""

    def trial_progress(
        self,
        *,
        trial_id: str,
        prompt_tokens_delta: int,
        completion_tokens_delta: int,
        cost_delta_usd: float,
    ) -> None:
        """Fired after each LLM generation inside the trial's agent loop."""

    def trial_completed(self, *, trial_id: str, binary_pass: bool, score: float | None) -> None:
        """Fired on a terminal, non-retryable success."""

    def trial_failed(self, *, trial_id: str, error: str, retryable: bool) -> None:
        """Fired on terminal failure (retryable-exhausted or hard raise)."""

    def judgment_scored(self, *, trial_id: str, score: float, binary_pass: bool) -> None:
        """Fired after the rubric judge populates ``trajectory.grade``."""

    def run_finished(self, *, output_dir: Path) -> None:
        """Fired at the very end of ``Orchestrator.run()``."""


class _NullRunDisplayEvents:
    """No-op :class:`RunDisplayEvents`.

    Wired as the default on ``OrchestratorDeps.events`` so the orchestrator,
    conductor, and runner never branch on ``events is None`` — they just
    call every method.
    """

    def run_started(self, **_: object) -> None: ...
    def trial_started(self, **_: object) -> None: ...
    def trial_progress(self, **_: object) -> None: ...
    def trial_completed(self, **_: object) -> None: ...
    def trial_failed(self, **_: object) -> None: ...
    def judgment_scored(self, **_: object) -> None: ...
    def run_finished(self, **_: object) -> None: ...


_NULL_EVENTS: RunDisplayEvents = _NullRunDisplayEvents()


__all__ = ["RunDisplayEvents", "_NullRunDisplayEvents", "_NULL_EVENTS"]
