"""The write retry policy: which refusals are posted again, after how long, and when it stops
(ADR-0048, amendment 2026-10-07). Time is faked throughout: nothing here sleeps."""

from __future__ import annotations

import logging
import random
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
    RetryPolicy,
    RetryStats,
    is_gateway_page,
    parse_retry_after,
)

pytestmark = pytest.mark.unit

GATEWAY = Answer(403, GATEWAY_PAGE)
OK = Answer(200)


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


def retrier(time: FakeTime, *, draw: float = 0.0, **policy) -> Retrier:
    return Retrier(
        RetryPolicy(**{"jitter": 0.0, **policy}),
        clock=time.clock,
        sleep=time.sleep,
        draw=lambda: draw,
    )


class TestWhatIsPostedAgain:
    def test_the_gateway_page_is_posted_again_and_then_succeeds(self) -> None:
        time = FakeTime()
        attempt = Script(GATEWAY, OK)
        assert retrier(time).run(attempt, what="span export") == OK
        assert attempt.calls == 2
        assert time.sleeps == [65.0]  # one window of the gateway's rate limit

    def test_a_403_langfuse_answers_itself_is_not_posted_again(self) -> None:
        """A wrong key must fail at once: Langfuse's own 403 is JSON, never the gateway's page."""
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
        assert attempt.calls == 3 and time.sleeps == [1.0, 2.0]

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
    def test_the_exact_backoff_sequence_with_jitter_off(self) -> None:
        time = FakeTime()
        attempt = Script(*[Answer(503)] * 5)
        assert retrier(time).run(attempt, what="x").status == 503
        assert attempt.calls == 5
        assert time.sleeps == [1.0, 2.0, 4.0, 8.0]

    def test_the_multiplier_and_the_longest_wait_shape_the_backoff(self) -> None:
        time = FakeTime()
        attempt = Script(*[Answer(429)] * 7)
        policy = {"max_attempts": 7, "initial_delay_s": 0.5, "multiplier": 3.0, "max_delay_s": 10}
        retrier(time, **policy).run(attempt, what="x")
        assert time.sleeps == [0.5, 1.5, 4.5, 10.0, 10.0, 10.0]

    @pytest.mark.parametrize("max_attempts", [1, 2, 3])
    def test_max_attempts_holds(self, max_attempts: int) -> None:
        time = FakeTime()
        attempt = Script(*[Answer(503)] * 10)
        retrier(time, max_attempts=max_attempts).run(attempt, what="x")
        assert attempt.calls == max_attempts
        assert len(time.sleeps) == max_attempts - 1

    def test_max_attempts_counts_every_refusal_whatever_refused_it(self) -> None:
        time = FakeTime()
        attempt = Script(GATEWAY, Answer(503), GATEWAY, Answer(429), GATEWAY, OK)
        assert retrier(time, max_attempts=4).run(attempt, what="x").status == 429
        assert attempt.calls == 4
        assert time.sleeps == [65.0, 1.0, 65.0]

    def test_the_gateway_is_waited_out_one_window_at_a_time_at_most_gateway_waits_times(
        self,
    ) -> None:
        time = FakeTime()
        attempt = Script(*[GATEWAY] * 10)
        assert retrier(time).run(attempt, what="x") == GATEWAY
        assert attempt.calls == 4  # the first post and one after each of three windows
        assert time.sleeps == [65.0, 65.0, 65.0]

    def test_a_longer_retry_after_is_honoured(self) -> None:
        time = FakeTime()
        attempt = Script(Answer(429, retry_after="30"), OK)
        assert retrier(time).run(attempt, what="x") == OK
        assert time.sleeps == [30.0]

    def test_a_retry_after_beyond_the_cap_is_waited_only_up_to_the_cap(self) -> None:
        time = FakeTime()
        attempt = Script(Answer(429, retry_after="600"), Answer(503, retry_after="600"), OK)
        assert retrier(time, retry_after_max_s=45).run(attempt, what="x") == OK
        assert time.sleeps == [45.0, 45.0]

    def test_a_shorter_retry_after_never_shortens_the_schedule(self) -> None:
        time = FakeTime()
        attempt = Script(Answer(503), Answer(503, retry_after="0"), GATEWAY, OK)
        retrier(time).run(attempt, what="x")
        assert time.sleeps == [1.0, 2.0, 65.0]

    def test_a_retry_after_given_as_a_date(self) -> None:
        now = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
        later = (now + timedelta(seconds=20)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        assert parse_retry_after(later, now=now) == 20.0
        assert parse_retry_after("12") == 12.0
        for unusable in (None, "", "soon", "-5", "1.5"):
            assert parse_retry_after(unusable) is None

    def test_jitter_lengthens_a_wait_within_its_bounds(self) -> None:
        policy = RetryPolicy(jitter=0.2)
        assert policy.wait_s("backoff", 3, None, 0.0) == 4.0
        assert policy.wait_s("backoff", 3, None, 0.999999) == pytest.approx(4.8, abs=1e-5)
        draws = random.Random(7)
        for _ in range(1000):
            draw = draws.random()
            for refusal, index, base in (("backoff", 1, 1.0), ("backoff", 5, 16.0)):
                assert base <= policy.wait_s(refusal, index, None, draw) < base * 1.2
            # a window or a Retry-After is never cut short, only spread
            assert 65.0 <= policy.wait_s("gateway", 1, None, draw) < 78.0
            assert 30.0 <= policy.wait_s("backoff", 1, "30", draw) < 36.0

    def test_the_jitter_draw_reaches_the_wait(self) -> None:
        time = FakeTime()
        attempt = Script(GATEWAY, Answer(503), OK)
        retrier(time, jitter=0.2, draw=0.5).run(attempt, what="x")
        assert time.sleeps == [pytest.approx(71.5), pytest.approx(1.1)]


class TestTheDeadline:
    def test_a_wait_that_does_not_fit_before_the_deadline_is_not_started(self) -> None:
        time = FakeTime()
        attempt = Script(GATEWAY, OK)
        answer = retrier(time).run(attempt, what="x", deadline=time.now + 60)
        assert answer == GATEWAY and attempt.calls == 1 and time.sleeps == []

    def test_waits_go_on_while_they_fit_and_leave_room_for_the_request(self) -> None:
        time = FakeTime()
        attempt = Script(GATEWAY, GATEWAY, OK)
        deadline = time.now + 65 + MIN_ATTEMPT_S
        assert retrier(time).run(attempt, what="x", deadline=deadline) == GATEWAY
        assert attempt.calls == 2 and time.sleeps == [65.0]

    def test_the_earlier_of_the_calls_deadline_and_the_retriers_wins(self) -> None:
        time = FakeTime()
        policy = retrier(time)
        policy.set_deadline(time.now + 10)
        attempt = Script(Answer(503), Answer(503), Answer(503), Answer(503), OK)
        policy.run(attempt, what="x", deadline=time.now + 1000)
        # 1, 2 and 4 s fit before the retrier's deadline with a second to spare; 8 s does not
        assert time.sleeps == [1.0, 2.0, 4.0] and attempt.calls == 4

    def test_a_deadline_moved_during_a_wait_ends_it(self) -> None:
        time = FakeTime()
        policy = retrier(time)

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
        assert time.sleeps == [65.0]  # the one wait cancel cut short; none after it

    @pytest.mark.parametrize("ending", ["cancel", "deadline"])
    def test_the_default_wait_wakes_when_the_run_end_arrives(self, ending: str) -> None:
        """No injected sleep: the retrier's own 65 s wait returns as soon as it is cancelled or
        its deadline moves before the wait's end."""
        import threading
        import time

        policy = Retrier(RetryPolicy(jitter=0.0))
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


class TestTheCounts:
    def test_retried_recovered_exhausted_and_waited(self) -> None:
        time = FakeTime()
        stats = RetryStats()
        policy = Retrier(RetryPolicy(jitter=0.0), stats=stats, clock=time.clock, sleep=time.sleep)
        policy.run(Script(OK), what="x")  # no retry
        policy.run(Script(GATEWAY, Answer(503), OK), what="x")  # recovered after two
        policy.run(Script(*[Answer(503)] * 5), what="x")  # exhausted after four
        policy.run(Script(Answer(503), Answer(500)), what="x")  # retried, then refused
        policy.run(Script(GATEWAY), what="x", deadline=time.now + 10)  # no room: exhausted
        with pytest.raises(ConnectionError):
            policy.run(Script(Answer(429), ConnectionError()), what="x")  # retried, then lost
        assert stats.counts() == {
            "retried_requests": 4,
            "retry_attempts": 2 + 4 + 1 + 1,
            "retries_recovered": 1,
            "retries_exhausted": 2,
            "retry_wait_s": 65 + 1 + 15 + 1 + 1,
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
            retrier(time).run(Script(*[Answer(503)] * 5), what="span export")
        infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert infos[0] == (
            "POST /api/public/ingestion: HTTP 403, the gateway's block page; posting again in "
            "65.0 s (attempt 2 of at most 5, gateway window 1 of 3)"
        )
        assert len(infos) == 1 + 4
        assert warnings == [
            "span export: HTTP 503; giving up after 5 attempt(s) and 15.0 s of waiting: "
            "max_attempts (5) reached"
        ]


class TestTheConfiguration:
    """``options.langfuse.retry``: strict, validated before any receiver is contacted."""

    def test_the_defaults(self) -> None:
        policy = LangfuseConfig().retry
        assert policy == RetryPolicy()
        assert policy.model_dump() == {
            "statuses": (403, 429, 503),
            "max_attempts": 5,
            "initial_delay_s": 1.0,
            "multiplier": 2.0,
            "max_delay_s": 16.0,
            "jitter": 0.2,
            "retry_after_max_s": 60.0,
            "gateway_window_s": 65.0,
            "gateway_waits": 3,
            "flush_grace_s": 240.0,
        }

    def test_a_block_as_yaml_writes_it(self) -> None:
        config = LangfuseConfig.model_validate(
            {
                "retry": {
                    "statuses": [403, 429, 502, 503, 504],
                    "max_attempts": 6,
                    "initial_delay_s": 2,
                    "max_delay_s": 30,
                    "jitter": 0,
                    "gateway_waits": 4,
                    "flush_grace_s": 0,
                }
            }
        )
        assert config.retry.statuses == (403, 429, 502, 503, 504)
        assert config.retry.max_attempts == 6 and config.retry.initial_delay_s == 2.0
        assert config.retry.gateway_window_s == 65.0  # unnamed keys keep their defaults
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
            ({"max_attempts": 0}, "greater than or equal to 1"),
            ({"max_attempts": 101}, "less than or equal to 100"),
            ({"max_attempts": 2.5}, "valid integer"),
            ({"max_attempts": "5"}, "valid integer"),
            ({"initial_delay_s": 0}, "greater than 0"),
            ({"initial_delay_s": -1}, "greater than 0"),
            ({"multiplier": 0.5}, "greater than or equal to 1"),
            ({"multiplier": 11}, "less than or equal to 10"),
            ({"max_delay_s": 0.5, "initial_delay_s": 1}, "shorter than initial_delay_s"),
            ({"jitter": -0.1}, "greater than or equal to 0"),
            ({"jitter": 1.5}, "less than or equal to 1"),
            ({"retry_after_max_s": -1}, "greater than or equal to 0"),
            ({"gateway_window_s": 0}, "greater than 0"),
            ({"gateway_waits": -1}, "greater than or equal to 0"),
            ({"flush_grace_s": -5}, "greater than or equal to 0"),
            ({"flush_grace_s": float("inf")}, "finite number"),
            ({"gateway_window_s": float("nan")}, "finite number"),
            ({"jitter": True}, "valid number"),
            ({"max_retries": 3}, "Extra inputs are not permitted"),
        ],
    )
    def test_a_bad_value_is_refused(self, block: dict, message: str) -> None:
        with pytest.raises(ValidationError, match=message):
            LangfuseConfig.model_validate({"retry": block})

    def test_the_policy_is_frozen(self) -> None:
        with pytest.raises(ValidationError):
            RetryPolicy().max_attempts = 9  # type: ignore[misc]
