"""Rate-limit probe mode: the retry controller and the default-path lock.

Locks the behaviour the mode exists for:

- **Regression lock** — with the mode off, ``_build_retrying`` returns
  ``stop_after_attempt(5)`` + ``wait_exponential(multiplier=2, min=4, max=60)``
  and ``_build_probe_retrying`` is never reached.
- 429s retry at ``retry_interval_s``, no exponential growth — exactly that
  interval with ``jitter_fraction=0``, and mean-preserving jitter otherwise.
- 429s stop once ``per_call_budget_s`` of wall time is spent, reraising the
  last 429 rather than a ``RetryError``.
- A non-429 error keeps today's five-attempt exponential bound even after a
  long 429 stretch, so one dead upstream cannot inherit the multi-hour budget.
  That bound depends on the 429 classifier not firing on incidental digits, so
  the false-positive surface is pinned here too.
- The counters that reach ``Metrics`` come from real controller runs, not from
  a hand-driven accumulator, and they are keyed by ``(role, model)`` so an
  agent's and a simulator's 429s can never be blended.
- There is no env activation channel: the mode arms only from a passed config
  block.

Every case drives the real ``LLMClient.generate`` outer controller with
``tolokaforge.core.llm.client.completion`` patched — no provider is contacted.
Wall time is faked by swapping ``tenacity``'s clock for a counter that only
advances when the injected ``sleep`` hook runs, so budget exhaustion is exact
and the suite stays instant.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import tenacity
import tenacity.wait
from litellm.exceptions import RateLimitError
from tenacity import stop_after_attempt, wait_exponential, wait_fixed

from tolokaforge.core.llm.client import (
    LLMClient,
    _build_rate_limit_wait,
    _is_rate_limit_exception,
)
from tolokaforge.core.models import (
    Message,
    MessageRole,
    ModelConfig,
    RateLimitProbeConfig,
)
from tolokaforge.core.run_display_events import (
    _NULL_EVENTS,
    LLMCallObservation,
    RateLimitProbeStats,
)

pytestmark = pytest.mark.unit


class _FakeClock:
    """``tenacity``'s monotonic clock, advanced only by the sleep hook.

    ``tenacity/__init__.py`` reads wall time exclusively through
    ``time.monotonic`` (call start, outcome timestamps), and
    ``RetryCallState.seconds_since_start`` — which the probe's ``stop``
    consumes — is derived from those two reads. Swapping the module's ``time``
    binding therefore makes the probe's budget arithmetic exact without any
    real sleeping.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    fake = _FakeClock()
    monkeypatch.setattr(tenacity, "time", fake)
    return fake


def _probe(
    *,
    retry_interval_s: float = 15.0,
    per_call_budget_s: float = 3600.0,
    jitter_fraction: float = 0.0,
) -> RateLimitProbeConfig:
    """An enabled probe config, jitter OFF by default.

    Jitter defaults ON in production; these cases assert exact wait arithmetic,
    so they opt out. ``TestJitter`` covers the shipped default.
    """
    return RateLimitProbeConfig(
        enabled=True,
        retry_interval_s=retry_interval_s,
        per_call_budget_s=per_call_budget_s,
        jitter_fraction=jitter_fraction,
    )


def _rate_limit_error(message: str = "Rate limit exceeded") -> RateLimitError:
    return RateLimitError(message=message, llm_provider="openrouter", model="anthropic/claude")


def _server_error() -> Exception:
    """A 5xx with no 429 fingerprint in its type, status, or text."""
    return ValueError("upstream returned 503 service unavailable")


def _context_length_error() -> Exception:
    """The production shape of a deterministic, permanent non-429 failure.

    ``_call_with_key_rotation`` wraps a provider context-length error like this.
    Its text contains ``429`` inside a token count — an unanchored substring
    match would hand it the multi-hour 429 budget and pollute the census.
    """
    return RuntimeError(
        "LLM API call failed: This model's maximum context length is 8192 tokens, "
        "however you requested 4429 tokens"
    )


def _completion_response(content: str = "ok") -> MagicMock:
    response = MagicMock()
    choice = MagicMock()
    message = MagicMock()
    message.content = content
    message.tool_calls = None
    message.thinking_blocks = None
    message.reasoning_content = None
    message.provider_specific_fields = None
    choice.message = message
    choice.finish_reason = "stop"
    choice.provider_specific_fields = None
    response.choices = [choice]
    response.usage = MagicMock(
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        prompt_tokens_details=None,
        completion_tokens_details=None,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    return response


def _make_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    probe: RateLimitProbeConfig | None = None,
    clock: _FakeClock | None = None,
    model: str = "anthropic/claude-3-haiku",
) -> LLMClient:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-sk-rate-limit-probe")
    client = LLMClient(
        ModelConfig(provider="openrouter", name=model),
        rate_limit_probe=probe,
    )
    # Inner read-timeout retry must not multiply the outer attempt count; a 429
    # is not a timeout so it never reaches that controller, but pinning the
    # budget to 0 keeps a mis-classification loud instead of slow.
    client._api_timeout_retries = 0
    client._retry_sleep = clock.sleep if clock is not None else (lambda _s: None)
    return client


def _generate(client: LLMClient, *, observation: LLMCallObservation | None = None) -> Any:
    return client.generate(
        system="s",
        messages=[Message(role=MessageRole.USER, content="hi")],
        observation=observation,
    )


# ``wait_exponential(multiplier=2, min=4, max=60)`` over attempts 1..4, which
# is every wait the default five-attempt controller can schedule.
_DEFAULT_BACKOFF = [4.0, 4.0, 8.0, 16.0]


class TestDefaultPathUnchanged:
    """The controller built when probe mode is off, pinned field by field."""

    def test_default_controller_keeps_five_attempts_and_exponential_backoff(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client(monkeypatch)
        retrying = client._build_retrying(None)

        expected_stop = stop_after_attempt(5)
        expected_wait = wait_exponential(multiplier=2, min=4, max=60)

        assert isinstance(retrying.stop, type(expected_stop))
        assert retrying.stop.max_attempt_number == expected_stop.max_attempt_number
        assert isinstance(retrying.wait, type(expected_wait))
        assert retrying.wait.multiplier == expected_wait.multiplier
        assert retrying.wait.min == expected_wait.min
        assert retrying.wait.max == expected_wait.max
        assert retrying.reraise is True

    def test_probe_controller_is_never_built_when_mode_is_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client(monkeypatch, probe=RateLimitProbeConfig(enabled=False))
        with patch.object(LLMClient, "_build_probe_retrying") as build_probe:
            client._build_retrying(None)
        build_probe.assert_not_called()

    def test_disabled_config_is_equivalent_to_no_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        explicit_off = _make_client(monkeypatch, probe=RateLimitProbeConfig(enabled=False))
        assert explicit_off._rate_limit_probe is None

    def test_persistent_429_still_dies_after_five_attempts_with_mode_off(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock
    ) -> None:
        """Today's behaviour: a 429 storm exhausts the five-attempt budget."""
        client = _make_client(monkeypatch, clock=clock)

        with (
            patch(
                "tolokaforge.core.llm.client.completion",
                side_effect=[_rate_limit_error()] * 10,
            ),
            patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
            pytest.raises(RuntimeError),
        ):
            _generate(client)

        assert clock.sleeps == _DEFAULT_BACKOFF


class TestFixedIntervalRetry:
    def test_every_429_wait_is_exactly_the_configured_interval(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock
    ) -> None:
        probe = _probe()
        client = _make_client(monkeypatch, probe=probe, clock=clock)

        with (
            patch(
                "tolokaforge.core.llm.client.completion",
                side_effect=[_rate_limit_error()] * 7 + [_completion_response("recovered")],
            ),
            patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
        ):
            result = _generate(client)

        assert result.text == "recovered"
        # Seven 429s past the default five-attempt cap, every wait identical.
        assert clock.sleeps == [15.0] * 7

    def test_retry_count_is_not_capped_at_the_default_five_attempts(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock
    ) -> None:
        probe = _probe(retry_interval_s=5.0)
        client = _make_client(monkeypatch, probe=probe, clock=clock)

        with (
            patch(
                "tolokaforge.core.llm.client.completion",
                side_effect=[_rate_limit_error()] * 40 + [_completion_response()],
            ),
            patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
        ):
            _generate(client)

        assert len(clock.sleeps) == 40


class TestBudgetExhaustion:
    def test_429s_past_the_budget_stop_and_reraise_the_last_429(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock
    ) -> None:
        probe = _probe(retry_interval_s=10.0, per_call_budget_s=45.0)
        client = _make_client(monkeypatch, probe=probe, clock=clock)

        with (
            patch(
                "tolokaforge.core.llm.client.completion",
                side_effect=[_rate_limit_error("Rate limit exceeded")] * 20,
            ),
            patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
            pytest.raises(RuntimeError) as excinfo,
        ):
            _generate(client)

        # Attempts fail at t = 0, 10, 20, 30, 40, 50; the t=50 outcome is the
        # first at or past the 45 s budget, so five waits precede the stop.
        assert clock.sleeps == [10.0] * 5
        # ``reraise=True`` surfaces the provider error, not a tenacity RetryError.
        assert not isinstance(excinfo.value, tenacity.RetryError)
        assert "Rate limit exceeded" in str(excinfo.value)


class TestNonRateLimitErrorsKeepTheirBound:
    def test_429s_then_persistent_500_stops_after_five_non_429_attempts(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock
    ) -> None:
        probe = _probe()
        client = _make_client(monkeypatch, probe=probe, clock=clock)

        side_effect = [_rate_limit_error()] * 3 + [_server_error()] * 20
        with (
            patch("tolokaforge.core.llm.client.completion", side_effect=side_effect) as completion,
            patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
            pytest.raises(RuntimeError) as excinfo,
        ):
            _generate(client)

        # 3 x 429 (fixed) + 5 non-429 attempts, the fifth of which stops.
        assert completion.call_count == 8
        assert "503 service unavailable" in str(excinfo.value)
        # The 429 waits are fixed; the 500 waits are exponential, never fixed.
        # The exponential reads the *global* attempt number, so the 500s resume
        # the curve at attempt 4 rather than restarting it — waits only ever get
        # longer, and the five-attempt cap is unaffected.
        assert clock.sleeps[:3] == [15.0] * 3
        assert clock.sleeps[3:] == [16.0, 32.0, 60.0, 60.0]

    def test_500_alone_under_probe_mode_matches_the_default_path(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock
    ) -> None:
        probe = _probe()
        probe_client = _make_client(monkeypatch, probe=probe, clock=clock)

        with (
            patch("tolokaforge.core.llm.client.completion", side_effect=[_server_error()] * 10),
            patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
            pytest.raises(RuntimeError),
        ):
            _generate(probe_client)
        probe_sleeps = list(clock.sleeps)

        default_clock = _FakeClock()
        monkeypatch.setattr(tenacity, "time", default_clock)
        default_client = _make_client(monkeypatch, clock=default_clock)
        with (
            patch("tolokaforge.core.llm.client.completion", side_effect=[_server_error()] * 10),
            patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
            pytest.raises(RuntimeError),
        ):
            _generate(default_client)

        assert probe_sleeps == default_clock.sleeps == _DEFAULT_BACKOFF


class TestRateLimitClassification:
    """``_is_rate_limit_exception`` decides which budget an error inherits.

    A false positive is the expensive direction: a *deterministic* failure would
    be retried at the fixed interval for the whole multi-hour budget and would
    also land in the 429 census the mode exists to produce.
    """

    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param(
                ValueError(
                    "This model's maximum context length is 8192 tokens, you requested 4429"
                ),
                id="token-count-containing-429",
            ),
            pytest.param(
                ValueError("upstream 503, request_id=req_8f429ab2"), id="request-id-containing-429"
            ),
            pytest.param(ValueError("bad envelope: {'total_tokens': 429}"), id="json-value-429"),
            pytest.param(
                ValueError("401 invalid api key - see https://docs/rate limit and quotas"),
                id="auth-error-mentioning-rate-limits",
            ),
            pytest.param(_context_length_error(), id="wrapped-context-length-prod-shape"),
            pytest.param(_server_error(), id="plain-503"),
        ],
    )
    def test_incidental_digits_and_unrelated_phrasing_are_not_rate_limits(
        self, exc: BaseException
    ) -> None:
        assert _is_rate_limit_exception(exc) is False

    @pytest.mark.parametrize(
        "exc",
        [
            pytest.param(
                ValueError("Error code: 429 - {'error': {'message': 'slow down'}}"),
                id="openai-status-line",
            ),
            pytest.param(ValueError("HTTP/1.1 429 Too Many Requests"), id="http-reason-phrase"),
            pytest.param(ValueError("status_code=429 provider=openrouter"), id="status-code-kv"),
            pytest.param(
                ValueError("litellm.RateLimitError: OpenrouterException"), id="stringified-type"
            ),
            pytest.param(ValueError("Rate limit exceeded for requests"), id="provider-prose"),
        ],
    )
    def test_anchored_status_and_prose_shapes_are_rate_limits(self, exc: BaseException) -> None:
        assert _is_rate_limit_exception(exc) is True

    def test_typed_429_with_innocuous_text_classifies_on_the_type_not_the_text(self) -> None:
        """Pins the type-first claim: delete the ``isinstance`` check and this
        fails, because nothing in the message looks like a rate limit."""
        exc = _rate_limit_error("quota for tier 2 is used up")
        assert _is_rate_limit_exception(exc) is True

    def test_status_code_429_alone_classifies(self) -> None:
        class _Opaque(Exception):
            status_code = 429

        assert _is_rate_limit_exception(_Opaque("opaque upstream payload")) is True

    def test_typed_429_reached_through_the_cause_chain(self) -> None:
        """The production shape: the outer controller only ever sees the wrap."""
        try:
            raise _rate_limit_error("quota for tier 2 is used up")
        except RateLimitError as inner:
            wrapped = RuntimeError(f"LLM API call failed: {inner}")
            wrapped.__cause__ = inner
        assert _is_rate_limit_exception(wrapped) is True

    def test_a_deterministic_non_429_never_inherits_the_probe_budget(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock
    ) -> None:
        """The failure scenario end to end: a context-length error mentioning
        ``4429`` must die on the five-attempt exponential, not spend an hour of
        fixed-interval retries, and must not touch the 429 counters."""
        client = _make_client(monkeypatch, probe=_probe(), clock=clock)
        stats = RateLimitProbeStats()
        observation = LLMCallObservation(
            events=_NULL_EVENTS, trial_id="task_x:0", role="agent", probe_stats=stats
        )

        with (
            patch(
                "tolokaforge.core.llm.client.completion",
                side_effect=[_context_length_error()] * 10,
            ),
            patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
            pytest.raises(RuntimeError),
        ):
            _generate(client, observation=observation)

        assert clock.sleeps == _DEFAULT_BACKOFF
        assert (stats.retries, stats.wait_s, stats.by_role_model) == (0, 0.0, {})


class TestJitter:
    """The fixed interval ships with symmetric jitter so blocked clients do not
    retry in lockstep. The estimator uses the *mean* interval, which the jitter
    preserves exactly."""

    def test_jitter_is_on_by_default(self) -> None:
        assert RateLimitProbeConfig().jitter_fraction == 0.2

    def test_zero_jitter_is_the_bare_fixed_interval(self) -> None:
        strategy = _build_rate_limit_wait(_probe(retry_interval_s=15.0, jitter_fraction=0.0))
        assert isinstance(strategy, wait_fixed)
        assert strategy.wait_fixed == 15.0

    def test_jittered_waits_stay_inside_the_configured_band(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock
    ) -> None:
        client = _make_client(
            monkeypatch,
            probe=_probe(retry_interval_s=15.0, jitter_fraction=0.2),
            clock=clock,
        )

        with (
            patch(
                "tolokaforge.core.llm.client.completion",
                side_effect=[_rate_limit_error()] * 20 + [_completion_response()],
            ),
            patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
        ):
            _generate(client)

        assert len(clock.sleeps) == 20
        assert all(12.0 <= wait <= 18.0 for wait in clock.sleeps)
        # Lockstep is the thing being prevented; identical waits would mean the
        # jitter never reached the controller.
        assert len(set(clock.sleeps)) > 1

    def test_mean_interval_is_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The extremes of the band, averaged, are exactly the interval — which
        is what keeps the ``1 / retry_interval_s`` poll-rate inversion valid in
        expectation."""
        draws = iter([0.0, 1.0] * 8)
        monkeypatch.setattr(tenacity.wait.random, "random", lambda: next(draws))
        strategy = _build_rate_limit_wait(_probe(retry_interval_s=15.0, jitter_fraction=0.2))
        state = MagicMock()

        waits = [strategy(retry_state=state) for _ in range(16)]

        assert min(waits) == pytest.approx(12.0)
        assert max(waits) == pytest.approx(18.0)
        assert sum(waits) / len(waits) == pytest.approx(15.0)

    def test_jitter_never_schedules_a_non_positive_wait(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``jitter_fraction`` is capped below 1.0, so the low end of the band
        stays positive even at the extreme."""
        monkeypatch.setattr(tenacity.wait.random, "random", lambda: 0.0)
        strategy = _build_rate_limit_wait(_probe(retry_interval_s=15.0, jitter_fraction=0.99))

        assert strategy(retry_state=MagicMock()) == pytest.approx(0.15)

    def test_jitter_does_not_apply_to_non_429_waits(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock
    ) -> None:
        client = _make_client(monkeypatch, probe=_probe(jitter_fraction=0.2), clock=clock)

        with (
            patch("tolokaforge.core.llm.client.completion", side_effect=[_server_error()] * 10),
            patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
            pytest.raises(RuntimeError),
        ):
            _generate(client)

        assert clock.sleeps == _DEFAULT_BACKOFF


class TestProbeCounters:
    def test_absorbed_429s_land_on_the_trial_stats(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock
    ) -> None:
        probe = _probe()
        client = _make_client(monkeypatch, probe=probe, clock=clock)
        stats = RateLimitProbeStats()
        observation = LLMCallObservation(
            events=_NULL_EVENTS,
            trial_id="task_x:0",
            role="agent",
            probe_stats=stats,
        )

        with (
            patch(
                "tolokaforge.core.llm.client.completion",
                side_effect=[_rate_limit_error()] * 4 + [_completion_response()],
            ),
            patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
        ):
            _generate(client, observation=observation)

        assert stats.retries == 4
        assert stats.wait_s == 60.0
        assert stats.first_ts is not None
        assert stats.last_ts is not None
        assert stats.last_ts >= stats.first_ts

    def test_non_429_retries_do_not_touch_the_probe_counters(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock
    ) -> None:
        probe = _probe()
        client = _make_client(monkeypatch, probe=probe, clock=clock)
        stats = RateLimitProbeStats()
        observation = LLMCallObservation(
            events=_NULL_EVENTS,
            trial_id="task_x:0",
            role="agent",
            probe_stats=stats,
        )

        with (
            patch(
                "tolokaforge.core.llm.client.completion",
                side_effect=[_server_error(), _completion_response()],
            ),
            patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
        ):
            _generate(client, observation=observation)

        assert (stats.retries, stats.wait_s, stats.first_ts) == (0, 0.0, None)

    def test_a_default_path_client_never_contributes_to_the_counters(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock
    ) -> None:
        """The recording is gated on *this* client's probe, not only on the
        observation carrying an accumulator, so exponential waits from a
        default-path client can never land in ``rate_limit_wait_s``."""
        client = _make_client(monkeypatch, clock=clock)
        stats = RateLimitProbeStats()
        observation = LLMCallObservation(
            events=_NULL_EVENTS, trial_id="task_x:0", role="agent", probe_stats=stats
        )

        with (
            patch(
                "tolokaforge.core.llm.client.completion",
                side_effect=[_rate_limit_error()] * 10,
            ),
            patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
            pytest.raises(RuntimeError),
        ):
            _generate(client, observation=observation)

        assert clock.sleeps == _DEFAULT_BACKOFF
        assert (stats.retries, stats.wait_s, stats.by_role_model) == (0, 0.0, {})


class TestPerModelAccounting:
    """The agent and the user simulator are different models in a real arena
    config, so their 429s must land in different buckets. Every assertion here
    fails if the counters were keyed by anything coarser than
    ``(role, model)``."""

    _AGENT_MODEL = "deepseek/deepseek-v3.2-exp"
    _USER_MODEL = "anthropic/claude-sonnet-4.6"

    def _drive(
        self,
        monkeypatch: pytest.MonkeyPatch,
        clock: _FakeClock,
        stats: RateLimitProbeStats,
        *,
        role: str,
        model: str,
        rate_limits: int,
    ) -> None:
        client = _make_client(monkeypatch, probe=_probe(), clock=clock, model=model)
        observation = LLMCallObservation(
            events=_NULL_EVENTS,
            trial_id="task_x:0",
            role=role,  # type: ignore[arg-type]  # LLMCallRole literal, given per case
            probe_stats=stats,
        )
        with (
            patch(
                "tolokaforge.core.llm.client.completion",
                side_effect=[_rate_limit_error()] * rate_limits + [_completion_response()],
            ),
            patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
        ):
            _generate(client, observation=observation)

    def test_agent_only_429s_bucket_under_the_agent_model(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock
    ) -> None:
        stats = RateLimitProbeStats()
        self._drive(monkeypatch, clock, stats, role="agent", model=self._AGENT_MODEL, rate_limits=3)

        assert list(stats.by_role_model) == [("agent", f"openrouter/{self._AGENT_MODEL}")]
        bucket = stats.by_role_model[("agent", f"openrouter/{self._AGENT_MODEL}")]
        assert (bucket.retries, bucket.wait_s) == (3, 45.0)
        assert (stats.retries, stats.wait_s) == (3, 45.0)

    def test_simulator_only_429s_bucket_under_the_user_model(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock
    ) -> None:
        stats = RateLimitProbeStats()
        self._drive(monkeypatch, clock, stats, role="user", model=self._USER_MODEL, rate_limits=2)

        assert list(stats.by_role_model) == [("user", f"openrouter/{self._USER_MODEL}")]
        bucket = stats.by_role_model[("user", f"openrouter/{self._USER_MODEL}")]
        assert (bucket.retries, bucket.wait_s) == (2, 30.0)

    def test_mixed_roles_keep_separate_buckets_and_a_correct_total(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock
    ) -> None:
        """One trial, two models, asymmetric 429 counts: the per-model rows are
        3 and 1, and the flat total is their sum. A shared counter would report
        4 against whichever model happened to be recorded."""
        stats = RateLimitProbeStats()
        self._drive(monkeypatch, clock, stats, role="agent", model=self._AGENT_MODEL, rate_limits=3)
        self._drive(monkeypatch, clock, stats, role="user", model=self._USER_MODEL, rate_limits=1)

        agent_key = ("agent", f"openrouter/{self._AGENT_MODEL}")
        user_key = ("user", f"openrouter/{self._USER_MODEL}")
        assert sorted(stats.by_role_model) == sorted([agent_key, user_key])
        assert stats.by_role_model[agent_key].retries == 3
        assert stats.by_role_model[user_key].retries == 1
        assert stats.by_role_model[agent_key].wait_s == 45.0
        assert stats.by_role_model[user_key].wait_s == 15.0
        assert stats.retries == 4
        assert stats.wait_s == 60.0

    def test_same_model_on_two_roles_still_splits_by_role(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock
    ) -> None:
        """A config that points both roles at one model must not merge them —
        the agent is the measured role and the simulator is not."""
        stats = RateLimitProbeStats()
        self._drive(monkeypatch, clock, stats, role="agent", model=self._AGENT_MODEL, rate_limits=2)
        self._drive(monkeypatch, clock, stats, role="user", model=self._AGENT_MODEL, rate_limits=1)

        assert sorted(stats.by_role_model) == [
            ("agent", f"openrouter/{self._AGENT_MODEL}"),
            ("user", f"openrouter/{self._AGENT_MODEL}"),
        ]
        assert stats.by_role_model[("agent", f"openrouter/{self._AGENT_MODEL}")].retries == 2
        assert stats.by_role_model[("user", f"openrouter/{self._AGENT_MODEL}")].retries == 1


class TestNoEnvActivationChannel:
    """There is deliberately no ``TOLOKAFORGE_RATE_LIMIT_PROBE``.

    An env override could not reach the agent client (the orchestrator always
    passes an explicit block, so the var would be dead there) while it *would*
    reach every kwarg-less site — the rubric judge, a ``--fallback-models``
    chain, ``run_trial`` — which are exactly the paths that must never probe,
    and would skip both budget assertions on the way. These cases pin the
    absence.
    """

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE"], ids=lambda v: f"env-{v}")
    def test_a_client_built_without_a_block_never_probes(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv("TOLOKAFORGE_RATE_LIMIT_PROBE", value)
        monkeypatch.setenv("TOLOKAFORGE_RATE_LIMIT_PROBE_INTERVAL_S", "7.5")
        monkeypatch.setenv("TOLOKAFORGE_RATE_LIMIT_PROBE_BUDGET_S", "900")

        assert _make_client(monkeypatch)._rate_limit_probe is None

    def test_a_disabled_block_still_never_probes_with_the_env_var_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TOLOKAFORGE_RATE_LIMIT_PROBE", "1")
        client = _make_client(monkeypatch, probe=RateLimitProbeConfig(enabled=False))
        assert client._rate_limit_probe is None

    def test_a_429_storm_on_an_env_armed_client_still_dies_after_five_attempts(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock
    ) -> None:
        """Behavioural, not shape: with the var set, the controller is still the
        default five-attempt exponential."""
        monkeypatch.setenv("TOLOKAFORGE_RATE_LIMIT_PROBE", "1")
        client = _make_client(monkeypatch, clock=clock)

        with (
            patch(
                "tolokaforge.core.llm.client.completion",
                side_effect=[_rate_limit_error()] * 10,
            ),
            patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
            pytest.raises(RuntimeError),
        ):
            _generate(client)

        assert clock.sleeps == _DEFAULT_BACKOFF

    def test_unset_env_var_leaves_the_mode_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TOLOKAFORGE_RATE_LIMIT_PROBE", raising=False)
        client = _make_client(monkeypatch)
        assert client._rate_limit_probe is None
