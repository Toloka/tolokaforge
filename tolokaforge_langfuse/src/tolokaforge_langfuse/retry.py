"""The retry policy of the producers' writes: which refusals are waited out and posted again.

The v4 producer posts a body once (ADR-0048): a re-sent observation id is an update on that
receiver, last write wins, so a body the receiver may already hold is not sent again by default.
Only a refusal that comes before the receiver reads the body is posted again (ADR-0048,
amendment 2026-10-07):

- a gateway's own refusal page: a 403 whose body carries one of ``gateway_markers`` (by default
  the Azure Application Gateway's). The gateway answers it without forwarding the request. The
  page does not say why: the usual reason is a write limit the gateway shares among its clients,
  but a block rule of its own looks the same, which is why :class:`RetryBreaker` stops the waiting
  once refusals outlast whole schedules. A 403 Langfuse answers itself is JSON and is never posted
  again;
- 429, and 503, which a service answers before it processes the request. A proxy may answer 503
  after forwarding the request; on a v4 receiver that re-send is an update with the same content.

All of them follow one schedule of waits, ``delays_s``, whose last step repeats, for at most
``max_retries`` re-sends; a longer ``Retry-After`` is honoured up to a cap. A lost answer, a
timeout and every status the policy does not list stay one attempt. An operator may list more
statuses: listing 500, 502 or 504 can re-send a body the receiver may already have read and
written, which on a v4 receiver is an update with the same content (the ADR-0048 trade-off).

:class:`RetryPolicy` is the configuration (``options.langfuse.retry``), :class:`Retrier` runs a
request under it within a deadline and a wait budget, :class:`RetryBreaker` stops the waiting for
all of a producer's routes, :class:`RetryStats` counts what they did for the receipt. The sleep,
the clock and the jitter's random draw are injectable, so tests never wait. Engine-free, like the
transport that uses it.
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
import weakref
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any, Final

from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator

_log = logging.getLogger(__name__)

GATEWAY_STATUS: Final = 403
# a re-post is started only when its wait leaves at least this long before the deadline
MIN_ATTEMPT_S: Final = 1.0

_BREAKER_OPEN: Final = (
    "the breaker is open (refusals outlasted whole schedules), so they fail at once until a "
    "write is accepted again"
)


class _Ending(str, Enum):
    """How a request ended, as far as the run-end flush and the breaker care."""

    OK = "ok"
    REFUSED = "refused"
    FAILED = "failed"


def is_gateway_page(body: bytes | str | None, markers: Sequence[str]) -> bool:
    """Whether an answer's body is a gateway's own refusal page: it carries one of ``markers``."""
    if not body:
        return False
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
    return any(marker in text for marker in markers)


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
        at = at.replace(tzinfo=UTC)
    return max((at - (now or datetime.now(UTC))).total_seconds(), 0.0)


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

    ``docs/OBSERVABILITY.md`` ("Retries") gives the reason for every default.
    """

    model_config = {"extra": "forbid", "frozen": True, "allow_inf_nan": False}

    statuses: tuple[StrictInt, ...] = (403, 429, 503)
    """The HTTP statuses posted again. 403 stands for a gateway's refusal page alone
    (``gateway_markers``): a 403 whose body is not that page is never posted again. 500, 502 and
    504 can come after the receiver read and wrote the body, so listing one can write a batch a
    second time (an update with the same content). ``[]`` turns retries off."""
    gateway_markers: tuple[str, ...] = ("Microsoft-Azure-Application-Gateway",)
    """Text that marks a 403's body as the refusal page of the gateway in front of the receiver,
    which it sends without forwarding the request; the default is the Azure Application
    Gateway's. A 403 that carries none of them is the receiver's own and fails at once."""
    max_retries: int = Field(default=6, ge=0, le=100, strict=True)
    """The most re-sends of one request after its first post. ``0`` turns retries off."""
    delays_s: tuple[float, ...] = (1.0, 3.0, 9.0, 20.0, 30.0)
    """The wait before the n-th re-post is the n-th value; the last one repeats."""
    jitter: float = Field(default=0.2, ge=0.0, le=1.0, strict=True)
    """Each wait is lengthened by a random share of itself up to this fraction, never shortened."""
    retry_after_max_s: float = Field(default=60.0, ge=0, strict=True)
    """A ``Retry-After`` longer than the schedule's wait is honoured up to this many seconds."""
    breaker_after: int = Field(default=2, ge=0, le=100, strict=True)
    """After this many requests in a row ran out their whole schedule still refused, a refusal
    fails at once, without a wait, until a write is accepted again. ``0`` never stops waiting."""
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

    @field_validator("gateway_markers")
    @classmethod
    def _check_markers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not marker.strip() for marker in value):
            raise ValueError(
                "gateway_markers holds a blank marker: almost every body carries it, Langfuse's "
                "own 403 included"
            )
        return value

    @field_validator("delays_s", mode="before")
    @classmethod
    def _numbers_only(cls, value: Any) -> Any:
        # a schedule step is a number of seconds: a boolean or a string is a mistake, not a step
        if isinstance(value, (list, tuple)):
            for step in value:
                if isinstance(step, bool) or not isinstance(step, (int, float)):
                    raise ValueError(f"delays_s holds {step!r}, not a number of seconds")
        return value

    @field_validator("delays_s")
    @classmethod
    def _check_delays(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if not value:
            raise ValueError("delays_s needs at least one wait (max_retries: 0 turns retries off)")
        if any(step <= 0 for step in value):
            raise ValueError(f"every wait in delays_s is above 0 s: {list(value)}")
        if any(later < earlier for earlier, later in zip(value, value[1:])):
            raise ValueError(f"delays_s never gets shorter: {list(value)}")
        return value

    @model_validator(mode="after")
    def _a_listed_403_has_a_marker(self) -> RetryPolicy:
        if GATEWAY_STATUS in self.statuses and not self.gateway_markers:
            raise ValueError(
                "statuses lists 403, which is posted again only when its body carries one of "
                "gateway_markers, and gateway_markers is empty"
            )
        return self

    def retries(self, answer: Answer) -> bool:
        """Whether this answer's request is posted again (when time allows)."""
        if answer.status not in self.statuses:
            return False
        return answer.status != GATEWAY_STATUS or self.is_gateway_page(answer)

    def is_gateway_page(self, answer: Answer) -> bool:
        """Whether the answer is a 403 refusal page of the gateway (``gateway_markers``)."""
        return answer.status == GATEWAY_STATUS and is_gateway_page(
            answer.body, self.gateway_markers
        )

    def delay_s(self, retry: int) -> float:
        """The schedule's wait before the ``retry``-th re-post (1-based)."""
        return self.delays_s[min(retry, len(self.delays_s)) - 1]

    def wait_s(self, retry: int, retry_after: str | None, draw: float) -> float:
        """The wait before the ``retry``-th re-post: the schedule's step, or a longer
        ``Retry-After`` up to its cap, then lengthened by ``draw`` (in [0, 1)) times ``jitter``."""
        base = self.delay_s(retry)
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
        self.breaker_trips = 0

    def record(self, *, posts: int, waited_s: float, recovered: bool, exhausted: bool) -> None:
        with self._lock:
            if posts > 1:
                self.retried_requests += 1
                self.retry_attempts += posts - 1
            self.retries_recovered += int(recovered)
            self.retries_exhausted += int(exhausted)
            self.retry_wait_s += waited_s

    def note_breaker_trip(self) -> None:
        with self._lock:
            self.breaker_trips += 1

    def counts(self) -> dict[str, int]:
        """The counters as whole numbers (the wait rounded up to the second)."""
        with self._lock:
            return {
                "retried_requests": self.retried_requests,
                "retry_attempts": self.retry_attempts,
                "retries_recovered": self.retries_recovered,
                "retries_exhausted": self.retries_exhausted,
                "retry_wait_s": math.ceil(self.retry_wait_s),
                "retry_breaker_trips": self.breaker_trips,
            }


class RetryBreaker:
    """Stops the waiting when refusals outlast it: once ``after`` requests in a row ran out their
    whole schedule still refused, a refusal fails at once until a write is accepted again.

    The gateway's page does not tell its shared limit, which lifts, from a rule of its own, which
    does not; this bounds what such a rule costs. Only a schedule run out to its end counts, and
    only from a retrier that counts towards it; a write that lands closes the breaker, and every
    route of a producer obeys the one it shares, a wait in progress included. Thread-safe;
    ``after=0`` never opens."""

    def __init__(self, after: int) -> None:
        self._after = after
        self._lock = threading.Lock()
        self._outlasted = 0
        self._open = False
        # what ends the waits in progress of the retriers that obey it, held weakly
        self._wakers: list[weakref.WeakMethod[Callable[[], None]]] = []

    @property
    def open(self) -> bool:
        with self._lock:
            return self._open

    def obeyed_by(self, wake: Callable[[], None]) -> None:
        """Register a retrier's ``wake``, which the breaker calls when it opens, to end the waits in
        progress. It must be a bound method: the breaker holds it weakly, so it never keeps the
        retrier alive, and ``weakref.WeakMethod`` refuses anything else."""
        with self._lock:
            self._wakers = [ref for ref in self._wakers if ref() is not None]
            self._wakers.append(weakref.WeakMethod(wake))

    def outlasted(self) -> bool:
        """A request ran out its whole schedule still refused; True when this opened the breaker."""
        with self._lock:
            self._outlasted += 1
            opened = bool(self._after) and not self._open and self._outlasted >= self._after
            self._open = self._open or opened
            count = self._outlasted
            wakers = [ref() for ref in self._wakers] if opened else []
        if opened:
            _log.warning(
                "%d request(s) in a row were still refused when their whole retry schedule ran "
                "out: the receiver, or the gateway in front of it, may refuse for good; refusals "
                "now fail at once, without a wait, until a write is accepted again "
                "(retry.breaker_after)",
                count,
            )
        for wake in wakers:
            if wake is not None:
                wake()
        return opened

    def accepted(self) -> None:
        """A write was accepted: the next refusal is waited out again."""
        with self._lock:
            was_open = self._open
            self._outlasted = 0
            self._open = False
        if was_open:
            _log.info("a write was accepted again: refusals are waited out again")


class Retrier:
    """Runs requests under a :class:`RetryPolicy`.

    :meth:`run` posts again only after a refusal the policy names, and starts a wait only when the
    breaker is shut, the wait fits what is left of ``wait_budget_s`` (all the retrier's waits
    together, when given) and it ends at least :data:`MIN_ATTEMPT_S` before the deadline: the
    call's own (a trial's attachment budget) or the retrier's (:meth:`set_deadline`, the run
    end), whichever is earlier. :meth:`cancel` ends every wait for good. A lost answer or a
    timeout is an exception from the attempt and propagates at once: the receiver may have taken
    that body. With ``trips_breaker`` false a schedule this retrier runs out does not count
    towards the breaker, which it still obeys (a run's parallel trial ends would otherwise open
    it within one schedule of a long refusal).

    ``sleep`` replaces the default wait, which wakes early when the deadline moves, the retrier
    is cancelled or the breaker opens; ``clock`` must then be the time ``sleep`` advances.
    """

    def __init__(
        self,
        policy: RetryPolicy,
        *,
        stats: RetryStats | None = None,
        breaker: RetryBreaker | None = None,
        trips_breaker: bool = True,
        wait_budget_s: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] | None = None,
        draw: Callable[[], float] = random.random,
    ) -> None:
        self.policy = policy
        self.stats = stats if stats is not None else RetryStats()
        self.breaker = breaker if breaker is not None else RetryBreaker(policy.breaker_after)
        self._trips_breaker = trips_breaker
        self._clock = clock
        self._sleep = sleep
        self._draw = draw
        self._condition = threading.Condition()
        # bumped whenever a wait may have to end early: the deadline moved or the run ended
        self._version = 0
        self._deadline: float | None = None
        self._cancelled = False
        self._wait_left = wait_budget_s
        # the run end's view of the receiver: requests that met a refusal (one count each), those
        # still waiting one out, and whether a request ended in another failure since the latest
        self._refused_requests = 0
        self._in_schedule = 0
        self._failed_since_refusal = False
        self.breaker.obeyed_by(self._wake)

    # -- the run end's controls ------------------------------------------------------------------

    def set_deadline(self, at: float | None) -> None:
        """No wait may end later than ``MIN_ATTEMPT_S`` before ``at`` (``clock`` time)."""
        with self._condition:
            self._deadline = at
            self._version += 1
            self._condition.notify_all()

    def cancel(self) -> None:
        """End every wait now and start none again: the producer is shutting down."""
        with self._condition:
            self._cancelled = True
            self._version += 1
            self._condition.notify_all()

    def refusal_mark(self) -> int:
        """Where a run-end flush starts looking (:meth:`refusing_since`): a request still waiting
        out a refusal here counts as refused after the mark."""
        with self._condition:
            return self._refused_requests - self._in_schedule

    def refusing_since(self, mark: int) -> bool:
        """Whether the receiver has been refusing since ``mark``: a request met a refusal the
        policy waits out after it (or was waiting one out at it), no request has ended in another
        failure since the latest such refusal, and the breaker is shut."""
        with self._condition:
            refusing = self._refused_requests > mark and not self._failed_since_refusal
        return refusing and not self.breaker.open

    def retries(self, answer: Answer) -> bool:
        """Whether the policy posts this answer's request again (when time allows)."""
        return self.policy.retries(answer)

    # -- one request -----------------------------------------------------------------------------

    def run(
        self, attempt: Callable[[], Answer], *, what: str, deadline: float | None = None
    ) -> Answer:
        """Post through ``attempt`` until an answer the policy does not post again, or until the
        policy, the deadline, the wait budget, the breaker or a cancellation stops it; returns the
        last answer. ``what`` names the request in the log (a method and a path, never a URL or a
        header)."""
        schedule = _Schedule()
        # an exception from the attempt is a failure that is not a refusal
        ending = _Ending.FAILED
        try:
            while True:
                schedule.posts += 1
                answer = attempt()
                if not self.policy.retries(answer):
                    ending = _Ending.OK if answer.ok else _Ending.FAILED
                    return answer
                self._note_refusal(first=not schedule.refused)
                schedule.refused = True
                stop, wait = self._next_wait(schedule, answer, deadline)
                if stop is None:
                    _log.info(
                        "%s: %s; posting again in %.1f s (retry %d of at most %d)",
                        what,
                        self._describe(answer),
                        wait,
                        schedule.posts,
                        self.policy.max_retries,
                    )
                    started = self._clock()
                    stop = self._pause(wait, deadline)
                    self._spend(schedule, max(0.0, self._clock() - started))
                if stop is not None:
                    ending = _Ending.REFUSED
                    self._give_up(what, answer, schedule, stop)
                    return answer
        finally:
            self._end(schedule, ending)

    def _next_wait(
        self, schedule: _Schedule, answer: Answer, deadline: float | None
    ) -> tuple[str | None, float]:
        """The wait before the next post, or why there is none: the breaker is open, the policy
        is spent, or the wait does not fit the budget or the deadline."""
        if self.breaker.open:
            return _BREAKER_OPEN, 0.0
        if schedule.posts > self.policy.max_retries:
            # a schedule run out after at least one wait is what the breaker counts
            schedule.spent = schedule.posts > 1
            return f"max_retries ({self.policy.max_retries}) used up", 0.0
        wait = self.policy.wait_s(schedule.posts, answer.retry_after, self._draw())
        return self._no_room(wait, deadline), wait

    def _no_room(self, wait: float, deadline: float | None) -> str | None:
        """Why a wait of ``wait`` seconds may not start now, or None."""
        with self._condition:
            if self._cancelled:
                return "the producer is shutting down"
            effective = self._earliest(deadline)
            budget = self._wait_left
        if budget is not None and wait > budget:
            return f"the next wait ({wait:.1f} s) exceeds the {budget:.1f} s of waiting left"
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
            if self.breaker.open:
                return _BREAKER_OPEN
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

    def _wake(self) -> None:
        """A wait in progress looks again at what ends it: the breaker opened."""
        with self._condition:
            self._version += 1
            self._condition.notify_all()

    def _earliest(self, deadline: float | None) -> float | None:
        if deadline is None:
            return self._deadline
        if self._deadline is None:
            return deadline
        return min(deadline, self._deadline)

    def _note_refusal(self, *, first: bool) -> None:
        with self._condition:
            if first:
                self._refused_requests += 1
                self._in_schedule += 1
            self._failed_since_refusal = False

    def _spend(self, schedule: _Schedule, waited: float) -> None:
        schedule.waited += waited
        with self._condition:
            if self._wait_left is not None:
                self._wait_left = max(0.0, self._wait_left - waited)

    def _give_up(self, what: str, answer: Answer, schedule: _Schedule, stop: str) -> None:
        if stop == _BREAKER_OPEN:
            # the breaker said so once at WARNING; each request it stops is a plain line
            _log.info("%s: %s; not waited out: %s", what, self._describe(answer), stop)
            return
        _log.warning(
            "%s: %s; giving up after %d post(s) and %.1f s of waiting: %s",
            what,
            self._describe(answer),
            schedule.posts,
            schedule.waited,
            stop,
        )
        if schedule.spent and self._trips_breaker and self.breaker.outlasted():
            self.stats.note_breaker_trip()

    def _end(self, schedule: _Schedule, ending: _Ending) -> None:
        with self._condition:
            if schedule.refused:
                self._in_schedule -= 1
            if ending is _Ending.FAILED:
                self._failed_since_refusal = True
        if ending is _Ending.OK:
            self.breaker.accepted()
        self.stats.record(
            posts=schedule.posts,
            waited_s=schedule.waited,
            recovered=ending is _Ending.OK and schedule.posts > 1,
            exhausted=ending is _Ending.REFUSED,
        )

    def _describe(self, answer: Answer) -> str:
        if self.policy.is_gateway_page(answer):
            return f"HTTP {answer.status}, the gateway's refusal page"
        return f"HTTP {answer.status}"


@dataclass
class _Schedule:
    """One request's progress through the policy."""

    posts: int = 0
    waited: float = 0.0
    refused: bool = False
    # the request ran out the whole schedule after waiting (what the breaker counts)
    spent: bool = False
