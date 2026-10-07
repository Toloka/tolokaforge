"""The write retry policy: which refusals are posted again, after how long, and when it stops
(ADR-0048, amendment 2026-10-07). Time is faked throughout: nothing here sleeps."""

from __future__ import annotations

import logging
import random
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fake_time import FakeTime
from otlp_receiver import GATEWAY_PAGE, LANGFUSE_403
from pydantic import ValidationError
from tolokaforge_langfuse.config import LangfuseConfig
from tolokaforge_langfuse.retry import (
    MIN_ATTEMPT_S,
    Answer,
    Retrier,
    RetryBreaker,
    RetryPolicy,
    RetryStats,
    is_gateway_page,
    parse_retry_after,
)

pytestmark = pytest.mark.unit

GATEWAY = Answer(403, GATEWAY_PAGE)
OK = Answer(200)
# the default schedule's six waits: 1 + 3 + 9 + 20 + 30 + 30 = 93 s
SCHEDULE = [1.0, 3.0, 9.0, 20.0, 30.0, 30.0]


class Script:
    """An attempt that answers from a list, in order, and counts its calls."""

    def __init__(self, *answers: Answer | Exception) -> None:
        self.answers = list(answers)
        self.calls = 0

    def __call__(self) -> Answer:
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def retrier(
    time: FakeTime,
    *,
    draw: float = 0.0,
    breaker: RetryBreaker | None = None,
    trips_breaker: bool = True,
    wait_budget_s: float | None = None,
    **policy,
) -> Retrier:
    return Retrier(
        RetryPolicy(**{"jitter": 0.0, **policy}),
        breaker=breaker,
        trips_breaker=trips_breaker,
        wait_budget_s=wait_budget_s,
        clock=time.clock,
        sleep=time.sleep,
        draw=lambda: draw,
    )


class TestWhatIsPostedAgain:
    def test_the_gateway_page_is_posted_again_and_then_succeeds(self) -> None:
        time = FakeTime()
        attempt = Script(GATEWAY, OK)
        assert retrier(time).run(attempt, what="span export") == OK
        assert attempt.calls == 2 and time.sleeps == [1.0]

    def test_a_403_langfuse_answers_itself_is_not_posted_again(self) -> None:
        """A key Langfuse rejects must fail at once: its own 403 is JSON, never the gateway's page."""
        time = FakeTime()
        refused = Answer(403, LANGFUSE_403)
        attempt = Script(refused)
        assert retrier(time).run(attempt, what="span export") == refused
        assert attempt.calls == 1 and time.sleeps == []

    @pytest.mark.parametrize("status", [400, 401, 404, 409, 413, 422, 500, 502, 504])
    def test_a_status_not_in_the_list_is_not_posted_again(self, status: int) -> None:
        time = FakeTime()
        attempt = Script(Answer(status, GATEWAY_PAGE))
        assert retrier(time).run(attempt, what="span export").status == status
        assert attempt.calls == 1 and time.sleeps == []

    def test_a_status_an_operator_lists_is_posted_again(self) -> None:
        time = FakeTime()
        attempt = Script(Answer(502), Answer(504), OK)
        assert retrier(time, statuses=[502, 504]).run(attempt, what="x") == OK
        assert attempt.calls == 3 and time.sleeps == [1.0, 3.0]

    def test_without_403_in_the_list_the_gateway_page_is_not_posted_again(self) -> None:
        time = FakeTime()
        attempt = Script(GATEWAY)
        assert retrier(time, statuses=[429, 503]).run(attempt, what="x") == GATEWAY
        assert attempt.calls == 1

    def test_an_empty_list_turns_retries_off(self) -> None:
        time = FakeTime()
        for answer in (GATEWAY, Answer(429), Answer(503)):
            attempt = Script(answer)
            assert retrier(time, statuses=[]).run(attempt, what="x") == answer
            assert attempt.calls == 1
        assert time.sleeps == []

    def test_a_lost_answer_is_not_posted_again(self) -> None:
        """An exception is an answer that never came: the receiver may hold the body."""
        time = FakeTime()
        attempt = Script(ConnectionError("reset by peer"), OK)
        with pytest.raises(ConnectionError):
            retrier(time).run(attempt, what="x")
        assert attempt.calls == 1 and time.sleeps == []

    def test_the_marker_is_what_makes_a_page_the_gateways(self) -> None:
        assert is_gateway_page(GATEWAY_PAGE) and is_gateway_page(GATEWAY_PAGE.decode())
        assert not is_gateway_page(LANGFUSE_403)
        assert not is_gateway_page(b"") and not is_gateway_page(None)


class TestTheSchedule:
    @pytest.mark.parametrize("refusal", [GATEWAY, Answer(429), Answer(503)])
    def test_every_retried_answer_follows_the_one_schedule(self, refusal: Answer) -> None:
        time = FakeTime()
        attempt = Script(*[refusal] * 20)
        assert retrier(time).run(attempt, what="x") == refusal
        assert attempt.calls == 7  # the first post and max_retries (6) re-sends
        assert time.sleeps == SCHEDULE and sum(time.sleeps) == 93.0

    def test_refusals_of_every_kind_share_one_schedule(self) -> None:
        time = FakeTime()
        attempt = Script(GATEWAY, Answer(503), Answer(429), GATEWAY, OK)
        assert retrier(time).run(attempt, what="x") == OK
        assert time.sleeps == [1.0, 3.0, 9.0, 20.0]

    def test_the_last_step_repeats(self) -> None:
        time = FakeTime()
        attempt = Script(*[Answer(503)] * 10)
        retrier(time, delays_s=[2, 5], max_retries=5).run(attempt, what="x")
        assert time.sleeps == [2.0, 5.0, 5.0, 5.0, 5.0]

    @pytest.mark.parametrize("max_retries", [0, 1, 2, 10])
    def test_max_retries_holds(self, max_retries: int) -> None:
        time = FakeTime()
        attempt = Script(*[GATEWAY] * 20)
        retrier(time, max_retries=max_retries).run(attempt, what="x")
        assert attempt.calls == max_retries + 1
        expected = [RetryPolicy().delay_s(n) for n in range(1, max_retries + 1)]
        assert time.sleeps == expected

    def test_a_longer_retry_after_is_honoured_in_place_of_the_step(self) -> None:
        time = FakeTime()
        attempt = Script(Answer(429, retry_after="30"), Answer(503, retry_after="2"), OK)
        assert retrier(time).run(attempt, what="x") == OK
        assert time.sleeps == [30.0, 3.0]  # a shorter Retry-After never shortens the step

    def test_a_retry_after_beyond_the_cap_is_waited_only_up_to_the_cap(self) -> None:
        time = FakeTime()
        attempt = Script(Answer(429, retry_after="600"), Answer(503, retry_after="600"), OK)
        assert retrier(time, retry_after_max_s=45).run(attempt, what="x") == OK
        assert time.sleeps == [45.0, 45.0]

    def test_a_retry_after_given_as_a_date(self) -> None:
        now = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
        later = (now + timedelta(seconds=20)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        assert parse_retry_after(later, now=now) == 20.0
        assert parse_retry_after("12") == 12.0
        for unusable in (None, "", "soon", "-5", "1.5"):
            assert parse_retry_after(unusable) is None

    def test_jitter_lengthens_a_wait_within_its_bounds(self) -> None:
        policy = RetryPolicy(jitter=0.2)
        assert policy.wait_s(3, None, 0.0) == 9.0
        assert policy.wait_s(3, None, 0.999999) == pytest.approx(10.8, abs=1e-5)
        draws = random.Random(7)
        for _ in range(1000):
            draw = draws.random()
            for retry in range(1, 12):
                step = policy.delay_s(retry)
                assert step <= policy.wait_s(retry, None, draw) < step * 1.2
            # a Retry-After is never cut short, only spread
            assert 30.0 <= policy.wait_s(1, "30", draw) < 36.0

    def test_the_jitter_draw_reaches_the_wait(self) -> None:
        time = FakeTime()
        attempt = Script(GATEWAY, Answer(503), OK)
        retrier(time, jitter=0.2, draw=0.5).run(attempt, what="x")
        assert time.sleeps == [pytest.approx(1.1), pytest.approx(3.3)]


class TestTheDeadline:
    def test_a_wait_that_does_not_fit_before_the_deadline_is_not_started(self) -> None:
        time = FakeTime()
        attempt = Script(GATEWAY, OK)
        answer = retrier(time).run(attempt, what="x", deadline=time.now + 1.5)
        assert answer == GATEWAY and attempt.calls == 1 and time.sleeps == []

    def test_waits_go_on_while_they_fit_and_leave_room_for_the_request(self) -> None:
        """A trial's budget cuts the schedule: 1, 3 and 9 s fit in 14 s with a second to spare
        for the request after each; the 20 s step does not."""
        time = FakeTime()
        attempt = Script(*[GATEWAY] * 10)
        assert retrier(time).run(attempt, what="x", deadline=time.now + 14) == GATEWAY
        assert time.sleeps == [1.0, 3.0, 9.0] and attempt.calls == 4

    def test_the_earlier_of_the_calls_deadline_and_the_retriers_wins(self) -> None:
        time = FakeTime()
        policy = retrier(time)
        policy.set_deadline(time.now + 5)
        attempt = Script(*[Answer(503)] * 10)
        policy.run(attempt, what="x", deadline=time.now + 1000)
        assert time.sleeps == [1.0, 3.0] and attempt.calls == 3

    def test_a_deadline_moved_during_a_wait_ends_it(self) -> None:
        time = FakeTime()
        policy = retrier(time, delays_s=[60])

        def sleep(seconds: float) -> None:
            policy.set_deadline(time.now + 5)  # the run end arrives while the batch waits
            time.sleep(seconds)

        policy._sleep = sleep
        attempt = Script(GATEWAY, OK)
        assert policy.run(attempt, what="x") == GATEWAY
        assert attempt.calls == 1

    def test_cancel_ends_every_wait_and_starts_none(self) -> None:
        time = FakeTime()
        policy = retrier(time)

        def sleep(seconds: float) -> None:
            policy.cancel()
            time.sleep(seconds)

        policy._sleep = sleep
        first = Script(GATEWAY, OK)
        assert policy.run(first, what="x") == GATEWAY and first.calls == 1
        second = Script(Answer(503), OK)
        assert policy.run(second, what="x").status == 503 and second.calls == 1
        assert time.sleeps == [1.0]  # the one wait cancel cut short; none after it

    @pytest.mark.parametrize("ending", ["cancel", "deadline"])
    def test_the_default_wait_wakes_when_the_run_end_arrives(self, ending: str) -> None:
        """No injected sleep: the retrier's own minute-long wait returns as soon as it is
        cancelled or its deadline moves before the wait's end."""
        import time

        policy = Retrier(RetryPolicy(jitter=0.0, delays_s=[60]))
        answers = [GATEWAY, OK]
        posted = threading.Event()

        def attempt() -> Answer:
            posted.set()
            return answers.pop(0)

        result: list[Answer] = []
        worker = threading.Thread(target=lambda: result.append(policy.run(attempt, what="x")))
        worker.start()
        assert posted.wait(5)
        if ending == "cancel":
            policy.cancel()
        else:
            policy.set_deadline(time.monotonic() + 2)
        worker.join(5)
        assert not worker.is_alive() and result == [GATEWAY]


class TestTheWaitBudget:
    """``wait_budget_s`` bounds all of a retrier's waits together (a transcript upload's)."""

    def test_the_waits_of_all_requests_together_stay_within_it(self) -> None:
        time = FakeTime()
        policy = retrier(time, wait_budget_s=10)
        assert policy.run(Script(*[GATEWAY] * 10), what="x") == GATEWAY  # 1 + 3, then 9 > 6
        assert policy.run(Script(GATEWAY, OK), what="x") == OK  # 1 more fits in the 6 left
        # 1 and 3 fit in the 5 left, 9 does not
        assert policy.run(Script(GATEWAY, GATEWAY, GATEWAY, OK), what="x") == GATEWAY
        assert time.sleeps == [1.0, 3.0, 1.0, 1.0, 3.0] and sum(time.sleeps) <= 10

    def test_only_the_time_spent_waiting_counts(self) -> None:
        time = FakeTime()
        policy = retrier(time, wait_budget_s=5)
        time.now += 1000  # a long upload without a refusal
        assert policy.run(Script(GATEWAY, OK), what="x") == OK
        assert time.sleeps == [1.0]


class TestTheBreaker:
    """The gateway's page does not say why: once refusals outlast whole schedules the waiting
    stops until a write is accepted again (``breaker_after``)."""

    def test_it_opens_after_requests_in_a_row_ran_out_their_schedule(self, caplog) -> None:
        time = FakeTime()
        stats = RetryStats()
        policy = Retrier(
            RetryPolicy(jitter=0.0, max_retries=2, breaker_after=2),
            stats=stats,
            clock=time.clock,
            sleep=time.sleep,
        )
        with caplog.at_level(logging.INFO, logger="tolokaforge_langfuse.retry"):
            policy.run(Script(*[GATEWAY] * 3), what="x")
            assert not policy.breaker.open
            policy.run(Script(*[GATEWAY] * 3), what="x")
            assert policy.breaker.open
            waits = list(time.sleeps)
            stopped = Script(GATEWAY, OK)
            assert policy.run(stopped, what="x") == GATEWAY
            assert policy.run(Script(Answer(503), OK), what="x").status == 503
        assert stopped.calls == 1 and time.sleeps == waits  # no wait once it is open
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        opened = [message for message in warnings if "refusals now fail at once" in message]
        assert len(opened) == 1 and opened[0].startswith("2 request(s) in a row")
        assert stats.counts()["retry_breaker_trips"] == 1
        assert stats.counts()["retries_exhausted"] == 4

    def test_a_write_accepted_closes_it(self, caplog) -> None:
        time = FakeTime()
        policy = retrier(time, max_retries=1, breaker_after=1)
        policy.run(Script(GATEWAY, GATEWAY), what="x")
        assert policy.breaker.open
        with caplog.at_level(logging.INFO, logger="tolokaforge_langfuse.retry"):
            assert policy.run(Script(OK), what="x") == OK
        assert not policy.breaker.open
        assert "a write was accepted again" in caplog.text
        assert policy.run(Script(GATEWAY, OK), what="x") == OK  # waited out again

    def test_a_request_cut_short_by_its_deadline_does_not_count(self) -> None:
        """A trial-end call that ran out of budget did not outlast a whole schedule."""
        time = FakeTime()
        policy = retrier(time, breaker_after=1)
        for _ in range(5):
            policy.run(Script(*[GATEWAY] * 10), what="x", deadline=time.now + 14)
        assert not policy.breaker.open

    def test_breaker_after_zero_never_opens(self) -> None:
        time = FakeTime()
        policy = retrier(time, max_retries=1, breaker_after=0)
        for _ in range(5):
            policy.run(Script(GATEWAY, GATEWAY), what="x")
        assert not policy.breaker.open and len(time.sleeps) == 5

    def test_one_breaker_stops_every_route_that_shares_it(self) -> None:
        time = FakeTime()
        shared = RetryBreaker(1)
        spans = retrier(time, max_retries=1, breaker=shared)
        trial_end = retrier(time, breaker=shared, trips_breaker=False)
        spans.run(Script(GATEWAY, GATEWAY), what="span export")
        waits = list(time.sleeps)
        stopped = Script(GATEWAY, OK)
        assert trial_end.run(stopped, what="POST /api/public/ingestion") == GATEWAY
        assert stopped.calls == 1 and time.sleeps == waits

    def test_a_route_that_does_not_trip_it_only_obeys_it(self) -> None:
        """The trial ends run whole schedules in parallel during a long refusal: they must not
        open the breaker the span export relies on."""
        time = FakeTime()
        shared = RetryBreaker(1)
        trial_end = retrier(time, max_retries=1, breaker=shared, trips_breaker=False)
        for _ in range(5):
            trial_end.run(Script(GATEWAY, GATEWAY), what="POST /api/public/ingestion")
        assert not shared.open and len(time.sleeps) == 5


class TestTheRunEndsView:
    """``refusal_mark`` and ``refusing_since``: what the run-end flush asks before it goes on past
    its timeout."""

    def test_a_request_waiting_out_a_refusal_at_the_mark_counts_after_it(self) -> None:
        time = FakeTime()
        policy = retrier(time)
        marks: list[int] = []

        def sleep(seconds: float) -> None:
            marks.append(policy.refusal_mark())  # the flush starts while the batch waits
            time.sleep(seconds)

        policy._sleep = sleep
        assert policy.run(Script(GATEWAY, OK), what="x") == OK
        assert policy.refusing_since(marks[0])  # it ended well, the receiver was refusing

    def test_a_refusal_after_the_mark_counts_and_none_does_not(self) -> None:
        time = FakeTime()
        policy = retrier(time)
        mark = policy.refusal_mark()
        policy.run(Script(OK), what="x")
        assert not policy.refusing_since(mark)
        policy.run(Script(Answer(429), OK), what="x")
        assert policy.refusing_since(mark)
        assert not policy.refusing_since(policy.refusal_mark())

    def test_a_failure_that_is_not_a_refusal_ends_it_and_a_new_refusal_starts_it(self) -> None:
        time = FakeTime()
        policy = retrier(time)
        mark = policy.refusal_mark()
        policy.run(Script(Answer(429), OK), what="x")
        with pytest.raises(ConnectionError):
            policy.run(Script(ConnectionError("timed out")), what="x")
        assert not policy.refusing_since(mark)
        policy.run(Script(Answer(500)), what="x")
        assert not policy.refusing_since(mark)
        policy.run(Script(GATEWAY, OK), what="x")
        assert policy.refusing_since(mark)

    def test_an_open_breaker_ends_it(self) -> None:
        time = FakeTime()
        policy = retrier(time, max_retries=1, breaker_after=1)
        mark = policy.refusal_mark()
        policy.run(Script(GATEWAY, GATEWAY), what="x")
        assert policy.breaker.open and not policy.refusing_since(mark)


class TestTheCounts:
    def test_retried_recovered_exhausted_and_waited(self) -> None:
        time = FakeTime()
        stats = RetryStats()
        policy = Retrier(RetryPolicy(jitter=0.0), stats=stats, clock=time.clock, sleep=time.sleep)
        policy.run(Script(OK), what="x")  # no retry
        policy.run(Script(GATEWAY, Answer(503), OK), what="x")  # recovered after two
        policy.run(Script(*[Answer(503)] * 7), what="x")  # exhausted after six
        policy.run(Script(Answer(503), Answer(500)), what="x")  # retried, then refused
        policy.run(Script(GATEWAY), what="x", deadline=time.now + 1.5)  # no room: exhausted
        with pytest.raises(ConnectionError):
            policy.run(Script(Answer(429), ConnectionError()), what="x")  # retried, then lost
        assert stats.counts() == {
            "retried_requests": 4,
            "retry_attempts": 2 + 6 + 1 + 1,
            "retries_recovered": 1,
            "retries_exhausted": 2,
            "retry_wait_s": (1 + 3) + 93 + 1 + 1,
            "retry_breaker_trips": 0,
        }

    def test_the_wait_is_rounded_up_to_the_second(self) -> None:
        stats = RetryStats()
        stats.record(posts=2, waited_s=0.25, recovered=True, exhausted=False)
        assert stats.counts()["retry_wait_s"] == 1


class TestTheLog:
    def test_each_retry_is_info_and_each_give_up_a_warning_with_the_status_and_the_wait(
        self, caplog
    ) -> None:
        time = FakeTime()
        with caplog.at_level(logging.INFO, logger="tolokaforge_langfuse.retry"):
            retrier(time).run(Script(GATEWAY, OK), what="POST /api/public/ingestion")
            retrier(time, max_retries=4).run(Script(*[Answer(503)] * 5), what="span export")
        infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        # the page names the gateway, not a reason: the line asserts no rate limit
        assert infos[0] == (
            "POST /api/public/ingestion: HTTP 403, the gateway's refusal page; posting again in "
            "1.0 s (retry 1 of at most 6)"
        )
        assert len(infos) == 1 + 4
        assert warnings == [
            "span export: HTTP 503; giving up after 5 post(s) and 33.0 s of waiting: "
            "max_retries (4) used up"
        ]


class TestTheConfiguration:
    """``options.langfuse.retry``: strict, validated before any receiver is contacted."""

    def test_the_defaults(self) -> None:
        policy = LangfuseConfig().retry
        assert policy == RetryPolicy()
        assert policy.model_dump() == {
            "statuses": (403, 429, 503),
            "max_retries": 6,
            "delays_s": (1.0, 3.0, 9.0, 20.0, 30.0),
            "jitter": 0.2,
            "retry_after_max_s": 60.0,
            "breaker_after": 2,
            "flush_grace_s": 240.0,
        }

    def test_a_block_as_yaml_writes_it(self) -> None:
        config = LangfuseConfig.model_validate(
            {
                "retry": {
                    "statuses": [403, 429, 502, 503, 504],
                    "max_retries": 5,
                    "delays_s": [2, 2, 10],
                    "jitter": 0,
                    "breaker_after": 0,
                    "flush_grace_s": 0,
                }
            }
        )
        assert config.retry.statuses == (403, 429, 502, 503, 504)
        assert config.retry.max_retries == 5 and config.retry.delays_s == (2.0, 2.0, 10.0)
        assert config.retry.retry_after_max_s == 60.0  # unnamed keys keep their defaults
        assert LangfuseConfig.model_validate_json(config.model_dump_json()) == config

    @pytest.mark.parametrize(
        ("block", "message"),
        [
            ({"statuses": [200]}, "not an HTTP error status"),
            ({"statuses": [307]}, "not an HTTP error status"),
            ({"statuses": [600]}, "not an HTTP error status"),
            ({"statuses": [503, 503]}, "lists a status twice"),
            ({"statuses": ["503"]}, "valid integer"),
            ({"statuses": [True]}, "valid integer"),
            ({"statuses": 503}, "valid tuple"),
            ({"max_retries": -1}, "greater than or equal to 0"),
            ({"max_retries": 101}, "less than or equal to 100"),
            ({"max_retries": 2.5}, "valid integer"),
            ({"max_retries": "5"}, "valid integer"),
            ({"max_retries": True}, "valid integer"),
            ({"delays_s": []}, "at least one wait"),
            ({"delays_s": [0]}, "finite number above 0"),
            ({"delays_s": [1, -3]}, "finite number above 0"),
            ({"delays_s": [1, 3, 2]}, "never gets shorter"),
            ({"delays_s": [float("inf")]}, "finite number"),
            ({"delays_s": [float("nan")]}, "finite number"),
            ({"delays_s": [True]}, "not a number of seconds"),
            ({"delays_s": ["9"]}, "not a number of seconds"),
            ({"delays_s": 9}, "valid tuple"),
            ({"jitter": -0.1}, "greater than or equal to 0"),
            ({"jitter": 1.5}, "less than or equal to 1"),
            ({"jitter": True}, "valid number"),
            ({"retry_after_max_s": -1}, "greater than or equal to 0"),
            ({"breaker_after": -1}, "greater than or equal to 0"),
            ({"breaker_after": False}, "valid integer"),
            ({"flush_grace_s": -5}, "greater than or equal to 0"),
            ({"flush_grace_s": float("inf")}, "finite number"),
            ({"retries": 3}, "Extra inputs are not permitted"),
            ({"gateway_window_s": 65}, "Extra inputs are not permitted"),
            ({"max_attempts": 5}, "Extra inputs are not permitted"),
            ({"initial_delay_s": 1}, "Extra inputs are not permitted"),
        ],
    )
    def test_a_bad_value_is_refused(self, block: dict, message: str) -> None:
        with pytest.raises(ValidationError, match=message):
            LangfuseConfig.model_validate({"retry": block})

    def test_the_policy_is_frozen(self) -> None:
        with pytest.raises(ValidationError):
            RetryPolicy().max_retries = 9  # type: ignore[misc]


def test_a_wait_fits_only_with_room_for_the_request() -> None:
    """The margin is what keeps a re-post from starting with no time left for its answer."""
    time = FakeTime()
    attempt = Script(GATEWAY, OK)
    assert retrier(time).run(attempt, what="x", deadline=time.now + 1 + MIN_ATTEMPT_S) == OK
    assert time.sleeps == [1.0]
