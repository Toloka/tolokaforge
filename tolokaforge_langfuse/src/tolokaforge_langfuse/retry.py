"""The retry policy of the producers' writes: which refusals are waited out and posted again.

The v4 producer posts a body once (ADR-0048): a re-sent observation id is an update on that
receiver, last write wins, so a body the receiver may already hold is not sent again by default.
Some answers prove that the receiver never read the body, and only those are posted again
(ADR-0048, amendment 2026-10-07):

- the external gateway's own block page: a 403 whose body names
  ``Microsoft-Azure-Application-Gateway``. The gateway answers it without forwarding the request
  when the host's shared write limit is spent, so the request is waited out one gateway window at
  a time. A 403 Langfuse answers itself is JSON and is never posted again: a wrong key fails at
  once;
- 429 and 503, waited out on an exponential backoff that honours ``Retry-After`` up to a cap.

A lost answer, a timeout and every status the policy does not list stay one attempt. An operator
may list more statuses (502, 504); a body re-sent after such an ambiguous answer is then an
update with the same content, which is the trade-off ADR-0048 describes.

:class:`RetryPolicy` is the configuration (``options.langfuse.retry``), :class:`Retrier` runs one
request under it within a deadline, :class:`RetryStats` counts what it did for the receipt. The
sleep, the clock and the jitter's random draw are injectable, so tests never wait. Engine-free,
like the transport that uses it.
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Final

from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator

_log = logging.getLogger(__name__)

# what the external gateway's refusal page carries; a 403 Langfuse answers itself is JSON
GATEWAY_PAGE_MARKER: Final = "Microsoft-Azure-Application-Gateway"
GATEWAY_STATUS: Final = 403
# a re-post is started only when its wait leaves at least this long before the deadline
MIN_ATTEMPT_S: Final = 1.0

# how a refusal the policy names is waited out
REFUSAL_GATEWAY: Final = "gateway"
REFUSAL_BACKOFF: Final = "backoff"


def is_gateway_page(body: bytes | str | None) -> bool:
    """Whether an answer's body is the external gateway's own page."""
    if not body:
        return False
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
    return GATEWAY_PAGE_MARKER in text


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """``Retry-After`` as seconds from now (delta-seconds or an HTTP-date); None when unusable."""
    if not value:
        return None
    text = value.strip()
    if text.isdecimal():
        return float(text)
    try:
        at = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return max((at - (now or datetime.now(timezone.utc))).total_seconds(), 0.0)


@dataclass(frozen=True)
class Answer:
    """One attempt's answer, as much of it as the policy reads."""

    status: int
    body: bytes = b""
    retry_after: str | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @classmethod
    def of(cls, result: Sequence[Any]) -> Answer:
        """From an opener's ``(status, body)`` or ``(status, body, headers)``."""
        if len(result) not in (2, 3):
            raise TypeError(f"an opener answers (status, body[, headers]), not {len(result)} items")
        headers: Mapping[str, str] | None = result[2] if len(result) == 3 else None
        retry_after = None
        for name, value in (headers or {}).items():
            if name.lower() == "retry-after":
                retry_after = str(value)
        return cls(int(result[0]), bytes(result[1] or b""), retry_after)


class RetryPolicy(BaseModel):
    """``options.langfuse.retry``: which refusals a write waits out and posts again, and how long.

    ``docs/OBSERVABILITY.md`` ("Retries") gives the reason for every default; ADR-0048's amendment
    of 2026-10-07 says when a re-send is safe.
    """

    model_config = {"extra": "forbid", "frozen": True, "allow_inf_nan": False}

    statuses: tuple[StrictInt, ...] = (403, 429, 503)
    """The HTTP statuses posted again. 403 stands for the gateway's block page alone: a 403 whose
    body is not that page is never posted again. ``[]`` turns retries off."""
    max_attempts: int = Field(default=5, ge=1, le=100, strict=True)
    """The most posts of one request, the first one included, whatever refused it."""
    initial_delay_s: float = Field(default=1.0, gt=0, strict=True)
    """The backoff's first wait (429, 503 and every listed status but the gateway's 403)."""
    multiplier: float = Field(default=2.0, ge=1.0, le=10.0, strict=True)
    """Each backoff wait is the previous one times this."""
    max_delay_s: float = Field(default=16.0, gt=0, strict=True)
    """The longest backoff wait."""
    jitter: float = Field(default=0.2, ge=0.0, le=1.0, strict=True)
    """Each wait is lengthened by a random share of itself up to this fraction, never shortened."""
    retry_after_max_s: float = Field(default=60.0, ge=0, strict=True)
    """A ``Retry-After`` longer than the schedule's wait is honoured up to this many seconds."""
    gateway_window_s: float = Field(default=65.0, gt=0, strict=True)
    """The wait after the gateway's block page: one window of its rate limit."""
    gateway_waits: int = Field(default=3, ge=0, le=100, strict=True)
    """The most gateway windows one request waits out."""
    flush_grace_s: float = Field(default=240.0, ge=0, strict=True)
    """How much longer than ``flush_timeout_s`` the run end may wait while the receiver refuses
    (in total, across the run end's flushes); also the most a transcript upload waits in all."""

    @field_validator("statuses")
    @classmethod
    def _check_statuses(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        for status in value:
            if not 400 <= status <= 599:
                raise ValueError(
                    f"{status} is not an HTTP error status (400-599): a 2xx is a success and a "
                    "3xx a refused redirect"
                )
        if len(set(value)) != len(value):
            raise ValueError(f"statuses lists a status twice: {list(value)}")
        return value

    @model_validator(mode="after")
    def _check_schedule(self) -> RetryPolicy:
        if self.max_delay_s < self.initial_delay_s:
            raise ValueError(
                f"max_delay_s={self.max_delay_s} is shorter than "
                f"initial_delay_s={self.initial_delay_s}"
            )
        return self

    def refusal(self, answer: Answer) -> str | None:
        """How this answer is waited out (:data:`REFUSAL_GATEWAY` or :data:`REFUSAL_BACKOFF`), or
        None when it is not posted again."""
        if answer.status not in self.statuses:
            return None
        if answer.status == GATEWAY_STATUS:
            return REFUSAL_GATEWAY if is_gateway_page(answer.body) else None
        return REFUSAL_BACKOFF

    def backoff_s(self, retry: int) -> float:
        """The backoff before the ``retry``-th re-post a backoff refusal causes (1-based)."""
        return min(self.initial_delay_s * self.multiplier ** (retry - 1), self.max_delay_s)

    def wait_s(self, refusal: str, retry: int, retry_after: str | None, draw: float) -> float:
        """The wait before a re-post: the gateway's window or the backoff, a longer
        ``Retry-After`` up to its cap, then lengthened by ``draw`` (in [0, 1)) times ``jitter``."""
        base = self.gateway_window_s if refusal == REFUSAL_GATEWAY else self.backoff_s(retry)
        hinted = parse_retry_after(retry_after)
        if hinted is not None:
            base = max(base, min(hinted, self.retry_after_max_s))
        return base * (1.0 + self.jitter * draw)


class RetryStats:
    """What a producer's retries did, for its receipt; shared by its routes, thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.retried_requests = 0
        self.retry_attempts = 0
        self.retries_recovered = 0
        self.retries_exhausted = 0
        self.retry_wait_s = 0.0

    def record(self, *, posts: int, waited_s: float, recovered: bool, exhausted: bool) -> None:
        with self._lock:
            if posts > 1:
                self.retried_requests += 1
                self.retry_attempts += posts - 1
            self.retries_recovered += int(recovered)
            self.retries_exhausted += int(exhausted)
            self.retry_wait_s += waited_s

    def counts(self) -> dict[str, int]:
        """The counters as whole numbers (the wait rounded up to the second)."""
        with self._lock:
            return {
                "retried_requests": self.retried_requests,
                "retry_attempts": self.retry_attempts,
                "retries_recovered": self.retries_recovered,
                "retries_exhausted": self.retries_exhausted,
                "retry_wait_s": math.ceil(self.retry_wait_s),
            }


class Retrier:
    """Runs one request at a time under a :class:`RetryPolicy`.

    :meth:`run` posts again only after a refusal the policy names, and starts a wait only when it
    ends at least :data:`MIN_ATTEMPT_S` before the deadline: the call's own (a trial's attachment
    budget) or the retrier's (:meth:`set_deadline`, the run end), whichever is earlier.
    :meth:`cancel` ends every wait for good. A lost answer or a timeout is an exception from the
    attempt and propagates at once: the receiver may have taken that body.

    ``sleep`` replaces the default wait, which wakes early when the deadline moves or the retrier
    is cancelled; ``clock`` must then be the time ``sleep`` advances.
    """

    def __init__(
        self,
        policy: RetryPolicy,
        *,
        stats: RetryStats | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] | None = None,
        draw: Callable[[], float] = random.random,
    ) -> None:
        self.policy = policy
        self.stats = stats if stats is not None else RetryStats()
        self._clock = clock
        self._sleep = sleep
        self._draw = draw
        self._condition = threading.Condition()
        # bumped whenever a wait may have to end early: the deadline moved or the run ended
        self._version = 0
        self._deadline: float | None = None
        self._cancelled = False
        self._refused_requests = 0
        self._in_schedule = 0

    # -- the run end's controls ------------------------------------------------------------------

    def set_deadline(self, at: float | None) -> None:
        """No wait may end later than ``MIN_ATTEMPT_S`` before ``at`` (``clock`` time)."""
        with self._condition:
            self._deadline = at
            self._version += 1
            self._condition.notify_all()

    def set_deadline_after(self, seconds: float) -> None:
        """:meth:`set_deadline` ``seconds`` from now: what waits may still take in all."""
        self.set_deadline(self._clock() + seconds)

    def cancel(self) -> None:
        """End every wait now and start none again: the producer is shutting down."""
        with self._condition:
            self._cancelled = True
            self._version += 1
            self._condition.notify_all()

    @property
    def refused_requests(self) -> int:
        """Requests so far that met a refusal the policy names, posted again or not."""
        with self._condition:
            return self._refused_requests

    @property
    def in_schedule(self) -> int:
        """Requests now between a refusal the policy names and their last attempt."""
        with self._condition:
            return self._in_schedule

    def retries(self, answer: Answer) -> bool:
        """Whether the policy posts this answer's request again (when time allows)."""
        return self.policy.refusal(answer) is not None

    # -- one request -----------------------------------------------------------------------------

    def run(
        self, attempt: Callable[[], Answer], *, what: str, deadline: float | None = None
    ) -> Answer:
        """Post through ``attempt`` until an answer the policy does not post again, or until the
        policy, the deadline or a cancellation stops it; returns the last answer. ``what`` names
        the request in the log (a method and a path, never a URL or a header)."""
        schedule = _Schedule()
        recovered = exhausted = scheduled = False
        try:
            while True:
                schedule.posts += 1
                answer = attempt()
                refusal = self.policy.refusal(answer)
                if refusal is None:
                    recovered = schedule.posts > 1 and answer.ok
                    return answer
                if not scheduled:
                    scheduled = True
                    self._enter_schedule()
                stop, wait = self._next_wait(schedule, refusal, answer, deadline)
                if stop is None:
                    _log.info(
                        "%s: %s; posting again in %.1f s (attempt %d of at most %d%s)",
                        what,
                        _describe(answer, refusal),
                        wait,
                        schedule.posts + 1,
                        self.policy.max_attempts,
                        (
                            f", gateway window {schedule.windows} of {self.policy.gateway_waits}"
                            if refusal == REFUSAL_GATEWAY
                            else ""
                        ),
                    )
                    started = self._clock()
                    stop = self._pause(wait, deadline)
                    schedule.waited += max(0.0, self._clock() - started)
                if stop is not None:
                    exhausted = True
                    _log.warning(
                        "%s: %s; giving up after %d attempt(s) and %.1f s of waiting: %s",
                        what,
                        _describe(answer, refusal),
                        schedule.posts,
                        schedule.waited,
                        stop,
                    )
                    return answer
        finally:
            if scheduled:
                with self._condition:
                    self._in_schedule -= 1
            self.stats.record(
                posts=schedule.posts,
                waited_s=schedule.waited,
                recovered=recovered,
                exhausted=exhausted,
            )

    def _next_wait(
        self, schedule: _Schedule, refusal: str, answer: Answer, deadline: float | None
    ) -> tuple[str | None, float]:
        """The wait before the next post, or why there is none (the policy is spent, or the wait
        does not fit before the deadline)."""
        if schedule.posts >= self.policy.max_attempts:
            return f"max_attempts ({self.policy.max_attempts}) reached", 0.0
        if refusal == REFUSAL_GATEWAY:
            if schedule.windows >= self.policy.gateway_waits:
                return f"gateway_waits ({self.policy.gateway_waits}) used up", 0.0
            schedule.windows += 1
            index = schedule.windows
        else:
            schedule.backoffs += 1
            index = schedule.backoffs
        wait = self.policy.wait_s(refusal, index, answer.retry_after, self._draw())
        return self._no_room(wait, deadline), wait

    def _no_room(self, wait: float, deadline: float | None) -> str | None:
        """Why a wait of ``wait`` seconds may not start now, or None."""
        with self._condition:
            if self._cancelled:
                return "the producer is shutting down"
            effective = self._earliest(deadline)
        if effective is None:
            return None
        left = effective - self._clock()
        if wait + MIN_ATTEMPT_S > left:
            return f"the next wait ({wait:.1f} s) does not fit the {max(0.0, left):.1f} s left"
        return None

    def _pause(self, seconds: float, deadline: float | None) -> str | None:
        """Wait ``seconds``; None when the wait passed in full, else why it ended early."""
        end = self._clock() + seconds
        remaining = seconds
        while True:
            with self._condition:
                version = self._version
                if self._cancelled:
                    return "the producer is shutting down"
                effective = self._earliest(deadline)
            if effective is not None and end + MIN_ATTEMPT_S > effective:
                return "the deadline moved before the wait's end"
            if remaining <= 0:
                return None
            self._wait(remaining, version)
            remaining = end - self._clock()

    def _wait(self, seconds: float, version: int) -> None:
        if self._sleep is not None:
            self._sleep(seconds)
            return
        with self._condition:
            self._condition.wait_for(lambda: self._version != version, timeout=seconds)

    def _earliest(self, deadline: float | None) -> float | None:
        if deadline is None:
            return self._deadline
        if self._deadline is None:
            return deadline
        return min(deadline, self._deadline)

    def _enter_schedule(self) -> None:
        with self._condition:
            self._in_schedule += 1
            self._refused_requests += 1


@dataclass
class _Schedule:
    """One request's progress through the policy."""

    posts: int = 0
    windows: int = 0
    backoffs: int = 0
    waited: float = 0.0


def _describe(answer: Answer, refusal: str) -> str:
    if refusal == REFUSAL_GATEWAY:
        return f"HTTP {answer.status}, the gateway's block page"
    return f"HTTP {answer.status}"
