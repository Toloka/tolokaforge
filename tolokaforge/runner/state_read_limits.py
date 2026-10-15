"""The one bound on reading a trial's state back from the process that holds it.

A trial whose tools keep its state in a state holder — an MCP server subprocess
or an app world service (ADR-0062) — has that state read back into the
db-service mirror before anything reads the mirror: ``GetState`` (the engine's
final-state capture among its callers), the grader's ``SubstrateService``
final-state reads, ``GradeTrial`` and the judge's DB tools. One such read may
take at most ``compute.state_read_timeout_s``; a holder that has not answered
by then fails the read like a dead one, with an error naming the setting.

Every runner-side deadline that waits on such a read is derived from the same
value (:class:`StateReadDeadlines`), so raising it lets a slow read through end
to end instead of tripping a fixed bound further up, and the caller keeps
seeing the read's own cause rather than its own timeout.

The engine passes the value to every runner container it starts or materialises
from a task-declared compose file as :data:`STATE_READ_TIMEOUT_ENV_VAR`; a runner
deployed outside the engine sets that variable itself.

This module imports nothing beyond the standard library so the run-config
schema and the runner image can both share it.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

__all__ = [
    "DEFAULT_STATE_READ_TIMEOUT_S",
    "STATE_READ_SETTING",
    "STATE_READ_TIMEOUT_ENV_VAR",
    "StateReadDeadlines",
    "state_read_timeout_s_from_env",
    "validate_state_read_timeout_s",
]

DEFAULT_STATE_READ_TIMEOUT_S = 20.0
"""Default bound in seconds on one read of a state holder's live state."""

STATE_READ_TIMEOUT_ENV_VAR = "TOLOKAFORGE_STATE_READ_TIMEOUT_S"
"""Environment variable the runner container reads the bound from."""

STATE_READ_SETTING = "compute.state_read_timeout_s"
"""The run-config path an operator raises when a state holder answers too slowly."""

_JUDGE_READ_AFTER_SYNC_S = 10.0
"""Budget of the judge's DB read itself, after the state holder's read it waits on."""

_FINAL_STATE_READ_AFTER_SYNC_S = 280.0
"""Budget of a whole-state mirror read itself, after the state holder's read."""

_GRADE_TRIAL_AFTER_HOLDER_CALLS_S = 540.0
"""Budget of ``GradeTrial`` beyond its bounded state holder calls (golden replay, judging)."""

_BOUNDED_HOLDER_CALLS_PER_GRADE_TRIAL = 3
"""Bounded state holder calls one ``GradeTrial`` makes at most: the read of the
live state before grading and, for hash grading, the read of the state the golden
replay left and — when grading fails after the replay — the restore of the
trial's own state into the holder."""

_ENGINE_GRADE_WAIT_MARGIN_S = 60.0
"""Head start an engine-side wait on a whole grade keeps over the runner's
``GradeTrial`` budget, for the transport and the queue around it."""


def validate_state_read_timeout_s(value: float) -> float:
    """Return ``value`` as a float when it is a positive, finite number of seconds.

    Raises:
        ValueError: ``value`` is not a number (``bool`` included), not finite,
            or not positive.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"State read timeout must be a number of seconds; got {value!r}.")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(
            f"State read timeout must be a positive, finite number of seconds; got {value}."
        )
    return float(value)


def state_read_timeout_s_from_env() -> float:
    """Read the bound from :data:`STATE_READ_TIMEOUT_ENV_VAR` inside the runner container.

    Unset (or empty) means :data:`DEFAULT_STATE_READ_TIMEOUT_S`. A value that is
    set but not a positive, finite number raises instead of falling back, so a
    misconfigured container fails at startup rather than serving with a bound
    the operator did not set.

    Raises:
        ValueError: the variable is set to something other than a positive,
            finite number; the message names the variable.
    """
    raw = os.environ.get(STATE_READ_TIMEOUT_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_STATE_READ_TIMEOUT_S
    try:
        return validate_state_read_timeout_s(float(raw))
    except ValueError as exc:
        raise ValueError(
            f"{STATE_READ_TIMEOUT_ENV_VAR}={raw!r} is invalid: expected a positive, "
            f"finite number of seconds."
        ) from exc


@dataclass(frozen=True)
class StateReadDeadlines:
    """The runner's deadlines on the paths that wait on a state holder read.

    Each one is the holder read's own bound plus the budget of the work around
    it, so it always outlasts the holder read: a slow holder fails with
    :meth:`holder_timeout_error`'s cause instead of the waiting caller's
    timeout. At the default bound they are the runner's long-standing values
    (30 s, 300 s, 300 s, 600 s); :attr:`engine_grade_wait_s` is the engine's side.
    """

    holder_read_s: float = DEFAULT_STATE_READ_TIMEOUT_S

    def __post_init__(self) -> None:
        validate_state_read_timeout_s(self.holder_read_s)

    @property
    def judge_db_read_s(self) -> float:
        """One read through the judge's DB tools (``get_db_state`` / ``query_db``)."""
        return self.holder_read_s + _JUDGE_READ_AFTER_SYNC_S

    @property
    def final_state_read_s(self) -> float:
        """One whole-state read: ``GetState``, a substrate final-state read, a grading factory."""
        return self.holder_read_s + _FINAL_STATE_READ_AFTER_SYNC_S

    @property
    def reset_trial_s(self) -> float:
        """One whole ``ResetTrial`` call, whose holder reset may be the holder's first contact."""
        return self.holder_read_s + _FINAL_STATE_READ_AFTER_SYNC_S

    @property
    def grade_trial_s(self) -> float:
        """One whole ``GradeTrial`` call."""
        return (
            _BOUNDED_HOLDER_CALLS_PER_GRADE_TRIAL * self.holder_read_s
            + _GRADE_TRIAL_AFTER_HOLDER_CALLS_S
        )

    @property
    def engine_grade_wait_s(self) -> float:
        """An engine-side wait on one whole grade of a trial (the ``queue`` trial grader's).

        It outlasts :attr:`grade_trial_s`, so a grade that runs long fails with the
        grading side's own cause rather than the engine's wait.
        """
        return self.grade_trial_s + _ENGINE_GRADE_WAIT_MARGIN_S

    def holder_timeout_error(self, holder_kind: str) -> TimeoutError:
        """The error a holder read that outlived :attr:`holder_read_s` fails with.

        It names the setting that raises the bound, since a holder that is slow
        rather than hung — a large world loading on first contact — is fixed by
        configuration, not by the pack.
        """
        return TimeoutError(
            f"the {holder_kind} did not answer within {self.holder_read_s:g}s; if its "
            f"state takes longer to read, raise `{STATE_READ_SETTING}` in the run config "
            f"(a runner deployed outside the engine reads {STATE_READ_TIMEOUT_ENV_VAR})"
        )
