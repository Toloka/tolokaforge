"""Rate-limit probe mode: the retry controller and the default-path lock.

Locks the behaviour the mode exists for:

- **Regression lock** — with the mode off, ``_build_retrying`` returns
  ``stop_after_attempt(5)`` + ``wait_exponential(multiplier=2, min=4, max=60)``
  and ``_build_probe_retrying`` is never reached.
- 429s retry at exactly ``retry_interval_s``, no exponential growth.
- 429s stop once ``per_call_budget_s`` of wall time is spent, reraising the
  last 429 rather than a ``RetryError``.
- A non-429 error keeps today's five-attempt exponential bound even after a
  long 429 stretch, so one dead upstream cannot inherit the multi-hour budget.
- The counters that reach ``Metrics`` come from real controller runs, not from
  a hand-driven accumulator.

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
from litellm.exceptions import RateLimitError
from tenacity import stop_after_attempt, wait_exponential

from tolokaforge.core.llm.client import LLMClient
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


def _rate_limit_error(message: str = "Rate limit exceeded") -> RateLimitError:
    return RateLimitError(message=message, llm_provider="openrouter", model="anthropic/claude")


def _server_error() -> Exception:
    """A 5xx with no 429 fingerprint in its type, status, or text."""
    return ValueError("upstream returned 503 service unavailable")


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
) -> LLMClient:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-sk-rate-limit-probe")
    client = LLMClient(
        ModelConfig(provider="openrouter", name="anthropic/claude-3-haiku"),
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
        probe = RateLimitProbeConfig(enabled=True, retry_interval_s=15.0, per_call_budget_s=3600.0)
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
        probe = RateLimitProbeConfig(enabled=True, retry_interval_s=5.0, per_call_budget_s=3600.0)
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
        probe = RateLimitProbeConfig(enabled=True, retry_interval_s=10.0, per_call_budget_s=45.0)
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
        probe = RateLimitProbeConfig(enabled=True, retry_interval_s=15.0, per_call_budget_s=3600.0)
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
        probe = RateLimitProbeConfig(enabled=True, retry_interval_s=15.0, per_call_budget_s=3600.0)
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


class TestProbeCounters:
    def test_absorbed_429s_land_on_the_trial_stats(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock
    ) -> None:
        probe = RateLimitProbeConfig(enabled=True, retry_interval_s=15.0, per_call_budget_s=3600.0)
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
        probe = RateLimitProbeConfig(enabled=True, retry_interval_s=15.0, per_call_budget_s=3600.0)
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


class TestEnvOverride:
    def test_env_var_enables_the_mode_when_no_config_block_was_passed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TOLOKAFORGE_RATE_LIMIT_PROBE", "1")
        monkeypatch.setenv("TOLOKAFORGE_RATE_LIMIT_PROBE_INTERVAL_S", "7.5")
        monkeypatch.setenv("TOLOKAFORGE_RATE_LIMIT_PROBE_BUDGET_S", "900")

        client = _make_client(monkeypatch)

        assert client._rate_limit_probe is not None
        assert client._rate_limit_probe.retry_interval_s == 7.5
        assert client._rate_limit_probe.per_call_budget_s == 900.0

    def test_env_var_is_ignored_when_a_config_block_disabled_the_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The run config wins so the run artifacts can never disagree with
        the controller that actually ran."""
        monkeypatch.setenv("TOLOKAFORGE_RATE_LIMIT_PROBE", "1")
        client = _make_client(monkeypatch, probe=RateLimitProbeConfig(enabled=False))
        assert client._rate_limit_probe is None

    def test_unset_env_var_leaves_the_mode_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TOLOKAFORGE_RATE_LIMIT_PROBE", raising=False)
        client = _make_client(monkeypatch)
        assert client._rate_limit_probe is None
