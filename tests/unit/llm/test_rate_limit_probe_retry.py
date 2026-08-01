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
  the false-positive surface is pinned here too — including the two cases where
  a 429 *fingerprint* is present but the condition is not transient: the engine's
  own ``AllApiKeysExhaustedError`` (which chains the provider's 429) and a
  deterministic typed non-429 whose message echoes rate-limit prose. Both
  directions are pinned, because missing a real 429 costs the absorption the
  whole mode is built on.
- The counters that reach ``Metrics`` come from real controller runs, not from
  a hand-driven accumulator, and they are keyed by ``(role, model)`` so an
  agent's and a simulator's 429s can never be blended.
- The SUCCESS census — successful calls, their duration and their tokens —
  records from the same runs, under the same two-part gate, into absolute-time
  windows; and records **nothing** with the mode off.
- The passed config block is the mode's only activation channel.

Every case drives the real ``LLMClient.generate`` outer controller with
``tolokaforge.core.llm.client.completion`` patched — no provider is contacted.
Two independent clocks are faked: ``tenacity``'s (a counter advanced only by the
injected ``sleep`` hook, so budget exhaustion is exact) and ``client.py``'s own
``time`` module (:class:`_FakeWallClock`, so the bucket a success lands in and
the ``duration_s`` it contributes are both deterministic). The suite stays
instant.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import openai
import pytest
import tenacity
import tenacity.wait
from litellm.exceptions import RateLimitError
from tenacity import stop_after_attempt, wait_exponential, wait_fixed

from tolokaforge.core.llm.client import (
    DEFAULT_API_CALL_TIMEOUT_S,
    DEFAULT_API_TIMEOUT_RETRIES,
    AllApiKeysExhaustedError,
    LLMClient,
    _build_rate_limit_wait,
    _is_rate_limit_exception,
    _should_retry_exception,
    matches_rate_limit_text,
)
from tolokaforge.core.models import (
    RATE_LIMIT_PROBE_ATTEMPT_CEILING_S,
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


def _bad_request_error(message: str) -> openai.BadRequestError:
    """A typed, deterministic provider 400 — an authoritative non-429 status."""
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    return openai.BadRequestError(message, response=httpx.Response(400, request=request), body=None)


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


def _completion_response(
    content: str = "ok",
    *,
    prompt_tokens: int = 1,
    completion_tokens: int = 1,
) -> MagicMock:
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
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        prompt_tokens_details=None,
        completion_tokens_details=None,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    return response


class _FakeWallClock:
    """Stands in for the ``time`` module inside ``client.py``.

    Two independent needs, both deterministic:

    * ``time()`` is the epoch second the probe buckets a success into, so
      driving it drives the absolute-time window a call lands in.
    * ``monotonic()`` advances a fixed step per read, so the per-attempt
      ``duration_s`` the success recorder sums is exact rather than a real
      sub-millisecond measurement.

    Independent of the ``_FakeClock`` swapped into ``tenacity`` — that one fakes
    the retry budget, this one fakes what ``client.py`` itself reads.
    """

    def __init__(self, *, epoch: float, monotonic_step: float = 0.5) -> None:
        self.epoch = epoch
        self._monotonic = 0.0
        self._step = monotonic_step

    def time(self) -> float:
        return self.epoch

    def monotonic(self) -> float:
        now = self._monotonic
        self._monotonic += self._step
        return now


@pytest.fixture
def wall(monkeypatch: pytest.MonkeyPatch) -> _FakeWallClock:
    fake = _FakeWallClock(epoch=_PROBE_EPOCH)
    monkeypatch.setattr("tolokaforge.core.llm.client.time", fake)
    return fake


_PROBE_EPOCH = 1_700_000_000.0
"""Epoch second the goodput cases start at.

Deliberately NOT a multiple of the 30 s bucket width — ``1_700_000_000 // 30 *
30 == 1_699_999_980`` — so a bucket start anchored on the first call instead of
on the epoch grid is visible as a wrong window key.
"""

_PROBE_EPOCH_BUCKET = 1_699_999_980
"""The window ``_PROBE_EPOCH`` falls in on the 30 s epoch grid."""


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


class TestTerminalKeyExhaustionIsNotARateLimit:
    """``AllApiKeysExhaustedError`` chains the provider's own 429 but is terminal.

    ``_call_with_key_rotation`` enters its rotation branch on OpenRouter's 429
    ("Key limit exceeded") and, after the last key, re-raises ``from`` that typed
    429. A ``__cause__`` walk therefore used to classify a **terminal**
    credential-exhaustion state as a transient 429 and hand it the multi-hour
    fixed-interval budget — for the rest of the run, because ``_rotate_key``
    only ever advances its index. This is the one defect that fires on the
    documented default config.

    Both directions are pinned: the terminal wrapper must NOT be a rate limit,
    and a genuinely wrapped 429 must still be one.
    """

    def _exhausted(self) -> AllApiKeysExhaustedError:
        """The exact shape ``_call_with_key_rotation`` raises."""
        try:
            raise _rate_limit_error("Key limit exceeded")
        except RateLimitError as inner:
            exhausted = AllApiKeysExhaustedError("All API keys exhausted")
            exhausted.__cause__ = inner
        return exhausted

    def test_the_terminal_wrapper_is_not_a_rate_limit(self) -> None:
        assert _is_rate_limit_exception(self._exhausted()) is False

    def test_a_real_wrapped_429_is_still_a_rate_limit(self) -> None:
        """The negative case above must not cost the absorption the mode exists
        for: the same wrap shape with a non-terminal outer type still classifies.
        """
        try:
            raise _rate_limit_error("Key limit exceeded")
        except RateLimitError as inner:
            wrapped = RuntimeError(f"LLM API call failed: {inner}")
            wrapped.__cause__ = inner
        assert _is_rate_limit_exception(wrapped) is True

    def test_the_default_retry_predicate_is_unchanged(self) -> None:
        """The fix lives in the 429 classifier only. ``_should_retry_exception``
        still returns True, so a probe-off run retries key exhaustion exactly as
        it always did — five attempts of exponential backoff."""
        assert _should_retry_exception(self._exhausted()) is True

    def test_it_is_a_runtime_error_carrying_the_unchanged_message(self) -> None:
        """The engine's text-matching classifiers (``core/runner.py``,
        ``core/resume.py``) and every ``except RuntimeError`` keep matching, so
        the type cannot change an existing run."""
        exhausted = self._exhausted()
        assert isinstance(exhausted, RuntimeError)
        assert str(exhausted) == "All API keys exhausted"

    def test_key_exhaustion_raises_the_terminal_type_from_the_typed_429(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The raise site, not a hand-built exception: one key, a provider 429
        whose text triggers rotation, nothing left to rotate to."""
        client = _make_client(monkeypatch)
        client._api_keys = ["only-key"]
        client._current_key_index = 0

        with (
            patch(
                "tolokaforge.core.llm.client.completion",
                side_effect=_rate_limit_error("Key limit exceeded"),
            ),
            pytest.raises(AllApiKeysExhaustedError) as excinfo,
        ):
            client._call_with_key_rotation({"model": "m", "messages": []})

        assert str(excinfo.value) == "All API keys exhausted"
        assert isinstance(excinfo.value.__cause__, RateLimitError)

    def test_exhausted_keys_under_probe_mode_take_the_bounded_default_path(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock
    ) -> None:
        """The failure scenario, end to end. A dead credential set must die on
        the five-attempt exponential — byte-for-byte the probe-off behaviour —
        instead of polling for ``per_call_budget_s``, and must contribute
        nothing to the 429 census the mode exists to produce."""
        client = _make_client(monkeypatch, probe=_probe(), clock=clock)
        client._api_keys = ["only-key"]
        client._current_key_index = 0
        stats = RateLimitProbeStats()
        observation = LLMCallObservation(
            events=_NULL_EVENTS, trial_id="task_x:0", role="agent", probe_stats=stats
        )

        with (
            patch(
                "tolokaforge.core.llm.client.completion",
                side_effect=[_rate_limit_error("Key limit exceeded")] * 10,
            ),
            patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
            pytest.raises(AllApiKeysExhaustedError),
        ):
            _generate(client, observation=observation)

        assert clock.sleeps == _DEFAULT_BACKOFF
        assert (stats.retries, stats.wait_s, stats.by_role_model) == (0, 0.0, {})

    def test_a_rotatable_429_still_rotates_before_giving_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rotation itself is untouched: with two keys the second is tried, and
        only the exhausted state raises the terminal type."""
        client = _make_client(monkeypatch)
        client._api_keys = ["first-key", "second-key"]
        client._current_key_index = 0

        with (
            patch(
                "tolokaforge.core.llm.client.completion",
                side_effect=[
                    _rate_limit_error("Key limit exceeded"),
                    _completion_response(),
                ],
            ),
        ):
            client._call_with_key_rotation({"model": "m", "messages": []})

        assert client._current_key_index == 1


class TestTheAttemptCeilingMatchesTheClient:
    """``RATE_LIMIT_PROBE_ATTEMPT_CEILING_S`` is restated in ``core/models.py``.

    It cannot be imported from here — ``core/llm/client.py`` imports
    ``core/models.py``, so the dependency only runs one way. The budget invariant
    reads it, so a drift between the two would silently loosen the lease bound.
    This case is the lock.
    """

    def test_the_ceiling_equals_the_clients_own_timeout_budget(self) -> None:
        attempts = DEFAULT_API_TIMEOUT_RETRIES + 1
        # ``wait_exponential(multiplier=1, min=1, max=5)`` over the five sleeps
        # between those six attempts: 1 + 2 + 4 + 5 + 5.
        inner_backoff_s = sum(min(max(2 ** (attempt - 1), 1), 5) for attempt in range(1, attempts))
        assert inner_backoff_s == 17
        assert (
            attempts * DEFAULT_API_CALL_TIMEOUT_S + inner_backoff_s
            == RATE_LIMIT_PROBE_ATTEMPT_CEILING_S
        )


class TestAnAuthoritativeStatusBeatsProse:
    """The anchored text tier is a fallback for a *stringified* provider error.

    The outermost production message is
    ``RuntimeError(f"LLM API call failed: {e}")`` and ``e``'s message can embed
    the provider's response body, which some providers use to echo request
    content. A task conversation about rate limiting could then put
    ``rate limit exceeded`` inside a deterministic 400 — and the text tier used
    to run on every wrapped non-429, so the 400 inherited the multi-hour budget.
    An HTTP status anywhere in the chain now settles it.

    The tier is NOT narrowed for untyped chains: under-matching a real 429 is
    the more expensive direction, because the whole feature is the absorption.
    """

    def test_a_typed_400_echoing_rate_limit_prose_is_not_a_rate_limit(self) -> None:
        inner = _bad_request_error(
            "Error code: 400 - {'error': {'message': 'Invalid tool call', 'metadata': "
            "{'raw': 'user: our gateway logged rate limit exceeded on the vendor "
            "API, please open a ticket'}}}"
        )
        wrapped = RuntimeError(f"LLM API call failed: {inner}")
        wrapped.__cause__ = inner

        assert matches_rate_limit_text(str(wrapped)) is True
        assert _is_rate_limit_exception(wrapped) is False

    def test_an_untyped_chain_still_text_matches(self) -> None:
        """The shape the tier exists for. No link carries a status, so prose is
        the only evidence available and it is still honoured."""
        inner = ValueError("Error code: 429 - {'error': 'slow down'}")
        wrapped = RuntimeError(f"LLM API call failed: {inner}")
        wrapped.__cause__ = inner

        assert _is_rate_limit_exception(wrapped) is True

    def test_a_bare_stringified_429_with_no_cause_still_text_matches(self) -> None:
        assert (
            _is_rate_limit_exception(
                RuntimeError("LLM API call failed: HTTP/1.1 429 Too Many Requests")
            )
            is True
        )

    def test_a_429_deeper_in_the_chain_beats_a_non_429_status_above_it(self) -> None:
        """The status tier is evaluated per link, so a genuine 429 under a
        wrapper that happens to carry its own status is still absorbed."""
        deepest = _rate_limit_error("slow down")
        middle = _bad_request_error("Error code: 400 - upstream relay")
        middle.__cause__ = deepest
        outer = RuntimeError(f"LLM API call failed: {middle}")
        outer.__cause__ = middle

        assert _is_rate_limit_exception(outer) is True


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


class TestTheConfigBlockIsTheOnlyActivationChannel:
    """The mode arms from the constructor argument and from nothing else.

    That is what keeps the paths which must never probe off it — the rubric
    judge, a ``--fallback-models`` chain, a bare ``run_trial`` — because they all
    build their clients without the argument, and it is what makes both budget
    assertions unskippable.
    """

    def test_a_client_built_without_a_block_never_probes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert _make_client(monkeypatch)._rate_limit_probe is None

    def test_a_disabled_block_is_the_same_as_no_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client(monkeypatch, probe=RateLimitProbeConfig(enabled=False))
        assert client._rate_limit_probe is None

    def test_a_429_storm_on_a_blockless_client_dies_after_five_attempts(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock
    ) -> None:
        """Behavioural, not shape: without a block the controller is still the
        default five-attempt exponential even under a persistent 429."""
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


class TestGoodputCounters:
    """The SUCCESS side, recorded from real controller runs.

    ``usage.calls`` cannot answer these questions: it holds agent calls only and
    carries no role field, so per-model goodput and latency are not computable
    from it. In a real measurement that gap forced counting litellm log lines by
    hand, which conflated the agent model with the user-simulator model and
    inflated the number.
    """

    def _observation(
        self, stats: RateLimitProbeStats | None, *, role: str = "agent"
    ) -> LLMCallObservation:
        return LLMCallObservation(
            events=_NULL_EVENTS,
            trial_id="task_x:0",
            role=role,  # type: ignore[arg-type]  # LLMCallRole literal, given per case
            probe_stats=stats,
        )

    def test_a_successful_call_records_calls_duration_and_tokens(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock, wall: _FakeWallClock
    ) -> None:
        client = _make_client(monkeypatch, probe=_probe(), clock=clock)
        stats = RateLimitProbeStats()

        with (
            patch(
                "tolokaforge.core.llm.client.completion",
                return_value=_completion_response(prompt_tokens=1234, completion_tokens=56),
            ),
            patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
        ):
            _generate(client, observation=self._observation(stats))

        assert stats.successes == 1
        assert stats.success_duration_s == 0.5
        assert (stats.prompt_tokens, stats.completion_tokens) == (1234, 56)
        row = stats.by_role_model[("agent", "openrouter/anthropic/claude-3-haiku")]
        assert (row.successes, row.prompt_tokens, row.completion_tokens) == (1, 1234, 56)
        # No 429 happened, so the failure census stays untouched — the two are
        # independent, which is the point of recording both.
        assert (stats.retries, stats.wait_s, stats.first_ts) == (0, 0.0, None)

    def test_tokens_are_counted_once_and_agree_with_usage(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock, wall: _FakeWallClock
    ) -> None:
        """The counters reuse the ``Usage`` ``_assemble_result`` already built for
        the call, so three calls give 3x the tokens — not 6x from a second
        extraction — and the trial's ``usage.calls`` list is unaffected."""
        client = _make_client(monkeypatch, probe=_probe(), clock=clock)
        stats = RateLimitProbeStats()
        observation = self._observation(stats)
        results = []

        with (
            patch(
                "tolokaforge.core.llm.client.completion",
                return_value=_completion_response(prompt_tokens=100, completion_tokens=10),
            ),
            patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
        ):
            for _ in range(3):
                results.append(_generate(client, observation=observation))

        assert stats.successes == 3
        assert (stats.prompt_tokens, stats.completion_tokens) == (300, 30)
        # Same numbers the per-call usage rows carry, counted once each.
        assert stats.prompt_tokens == sum(
            call.prompt_tokens for result in results for call in result.usage.calls
        )
        assert stats.successes == sum(len(result.usage.calls) for result in results)

    def test_a_429_then_a_success_records_both_censuses_for_one_model(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock, wall: _FakeWallClock
    ) -> None:
        client = _make_client(monkeypatch, probe=_probe(), clock=clock)
        stats = RateLimitProbeStats()

        with (
            patch(
                "tolokaforge.core.llm.client.completion",
                side_effect=[_rate_limit_error()] * 2
                + [_completion_response(prompt_tokens=90, completion_tokens=9)],
            ),
            patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
        ):
            _generate(client, observation=self._observation(stats))

        row = stats.by_role_model[("agent", "openrouter/anthropic/claude-3-haiku")]
        assert (row.retries, row.wait_s) == (2, 30.0)
        assert (row.successes, row.prompt_tokens) == (1, 90)
        # One retried call is one success, not three: only the returning attempt
        # counts, so goodput is completions and not attempts.
        assert stats.successes == 1

    def test_a_failed_call_records_no_success(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock, wall: _FakeWallClock
    ) -> None:
        """Goodput must count only what the provider actually served."""
        client = _make_client(monkeypatch, probe=_probe(), clock=clock)
        stats = RateLimitProbeStats()

        with (
            patch(
                "tolokaforge.core.llm.client.completion",
                side_effect=[_server_error()] * 6,
            ),
            patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
            pytest.raises(RuntimeError),
        ):
            _generate(client, observation=self._observation(stats))

        assert (stats.successes, stats.success_duration_s) == (0, 0.0)
        assert (stats.prompt_tokens, stats.completion_tokens) == (0, 0)
        assert stats.by_bucket == {}

    def test_a_default_path_client_records_no_goodput(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock, wall: _FakeWallClock
    ) -> None:
        """Probe OFF must record nothing, even when the observation carries an
        accumulator. The gate is the same two-part one the 429 side uses, so a
        rubric judge or a fallback-chain member cannot contribute to a
        measurement it is not part of."""
        client = _make_client(monkeypatch, clock=clock)
        assert client._rate_limit_probe is None
        stats = RateLimitProbeStats()

        with (
            patch(
                "tolokaforge.core.llm.client.completion",
                return_value=_completion_response(prompt_tokens=999, completion_tokens=99),
            ),
            patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
        ):
            result = _generate(client, observation=self._observation(stats))

        # The call really did succeed and really did report tokens...
        assert result.usage.prompt_tokens == 999
        # ...and none of it was recorded.
        assert (stats.successes, stats.prompt_tokens, stats.completion_tokens) == (0, 0, 0)
        assert stats.success_duration_s == 0.0
        assert stats.by_role_model == {}
        assert stats.by_bucket == {}

    def test_no_observation_records_nothing_and_still_returns(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock, wall: _FakeWallClock
    ) -> None:
        client = _make_client(monkeypatch, probe=_probe(), clock=clock)

        with (
            patch(
                "tolokaforge.core.llm.client.completion",
                return_value=_completion_response(),
            ),
            patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
        ):
            assert _generate(client, observation=None).usage.prompt_tokens == 1

    def test_an_observation_without_stats_records_nothing(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock, wall: _FakeWallClock
    ) -> None:
        client = _make_client(monkeypatch, probe=_probe(), clock=clock)

        with (
            patch(
                "tolokaforge.core.llm.client.completion",
                return_value=_completion_response(),
            ),
            patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
        ):
            _generate(client, observation=self._observation(None))


class TestGoodputPerModelAttribution:
    """Per-``(role, model)`` separation of the SUCCESS side, driven end to end.

    In the measured live case every absorbed 429 belonged to the agent and none
    to the simulator (``docs/OUTPUT_FORMAT.md`` § Field observations). The same
    attribution has to hold for goodput, or a leg's agent tokens/s is inflated by
    its simulator's traffic.
    """

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
        calls: int,
        prompt_tokens: int,
        completion_tokens: int = 10,
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
                return_value=_completion_response(
                    prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
                ),
            ),
            patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
        ):
            for _ in range(calls):
                _generate(client, observation=observation)

    def test_mixed_roles_keep_separate_goodput_rows(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock, wall: _FakeWallClock
    ) -> None:
        """One trial, two models, asymmetric in BOTH call count and token
        profile — the token values are the measured ~4x cross-domain spread
        (``docs/OUTPUT_FORMAT.md`` § Field observations). A shared bucket reports
        4 calls and 1,199,555 tokens against whichever model was recorded, so
        every assertion here fails if the buckets were merged."""
        stats = RateLimitProbeStats()
        self._drive(
            monkeypatch,
            clock,
            stats,
            role="agent",
            model=self._AGENT_MODEL,
            calls=3,
            prompt_tokens=369_857,
        )
        self._drive(
            monkeypatch,
            clock,
            stats,
            role="user",
            model=self._USER_MODEL,
            calls=1,
            prompt_tokens=89_984,
        )

        agent_key = ("agent", f"openrouter/{self._AGENT_MODEL}")
        user_key = ("user", f"openrouter/{self._USER_MODEL}")
        assert sorted(stats.by_role_model) == sorted([agent_key, user_key])
        agent, user = stats.by_role_model[agent_key], stats.by_role_model[user_key]
        assert (agent.successes, agent.prompt_tokens) == (3, 1_109_571)
        assert (user.successes, user.prompt_tokens) == (1, 89_984)
        assert stats.successes == 4
        assert stats.prompt_tokens == agent.prompt_tokens + user.prompt_tokens == 1_199_555
        # Little's law is per model too: summed duration over wall time.
        assert agent.success_duration_s == 1.5
        assert user.success_duration_s == 0.5

    def test_same_model_on_two_roles_still_splits_goodput(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock, wall: _FakeWallClock
    ) -> None:
        """A config pointing both roles at one model must not merge them: the
        agent is the measured role and the simulator is not."""
        stats = RateLimitProbeStats()
        self._drive(
            monkeypatch,
            clock,
            stats,
            role="agent",
            model=self._AGENT_MODEL,
            calls=2,
            prompt_tokens=100,
        )
        self._drive(
            monkeypatch,
            clock,
            stats,
            role="user",
            model=self._AGENT_MODEL,
            calls=1,
            prompt_tokens=7,
        )

        slug = f"openrouter/{self._AGENT_MODEL}"
        assert sorted(stats.by_role_model) == [("agent", slug), ("user", slug)]
        assert stats.by_role_model[("agent", slug)].prompt_tokens == 200
        assert stats.by_role_model[("user", slug)].prompt_tokens == 7

    def test_goodput_and_429s_of_two_models_stay_independent(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock, wall: _FakeWallClock
    ) -> None:
        """The live shape: every 429 on the agent, none on the simulator, while
        both serve successful calls."""
        stats = RateLimitProbeStats()
        agent = _make_client(monkeypatch, probe=_probe(), clock=clock, model=self._AGENT_MODEL)
        agent_obs = LLMCallObservation(
            events=_NULL_EVENTS, trial_id="t:0", role="agent", probe_stats=stats
        )
        with (
            patch(
                "tolokaforge.core.llm.client.completion",
                side_effect=[_rate_limit_error()] * 3 + [_completion_response(prompt_tokens=50)],
            ),
            patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
        ):
            _generate(agent, observation=agent_obs)
        self._drive(
            monkeypatch,
            clock,
            stats,
            role="user",
            model=self._USER_MODEL,
            calls=2,
            prompt_tokens=5,
        )

        agent_row = stats.by_role_model[("agent", f"openrouter/{self._AGENT_MODEL}")]
        user_row = stats.by_role_model[("user", f"openrouter/{self._USER_MODEL}")]
        assert (agent_row.retries, agent_row.successes) == (3, 1)
        assert (user_row.retries, user_row.successes) == (0, 2)


class TestGoodputBucketsFromRealCalls:
    """Absolute-time windows, filled by the real controller.

    A cumulative counter hides non-stationarity: measured goodput decays at a
    CONSTANT offered concurrency while rejections climb (``docs/OUTPUT_FORMAT.md``
    § Field observations). The windows are how that curve survives into the
    artifacts.
    """

    def _observation(self, stats: RateLimitProbeStats) -> LLMCallObservation:
        return LLMCallObservation(
            events=_NULL_EVENTS, trial_id="t:0", role="agent", probe_stats=stats
        )

    def test_calls_land_in_epoch_aligned_windows(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock, wall: _FakeWallClock
    ) -> None:
        client = _make_client(monkeypatch, probe=_probe(), clock=clock)
        stats = RateLimitProbeStats(bucket_width_s=30)
        observation = self._observation(stats)

        with (
            patch(
                "tolokaforge.core.llm.client.completion",
                return_value=_completion_response(prompt_tokens=10, completion_tokens=1),
            ),
            patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
        ):
            for offset in (0.0, 5.0, 35.0):
                wall.epoch = _PROBE_EPOCH + offset
                _generate(client, observation=observation)

        slug = "openrouter/anthropic/claude-3-haiku"
        # First two calls (epoch +0, +5) share the window _PROBE_EPOCH falls in;
        # the third (+35) is one window later. Both starts are multiples of 30
        # from the Unix epoch, not from the first call.
        assert sorted(stats.by_bucket) == [
            ("agent", slug, _PROBE_EPOCH_BUCKET),
            ("agent", slug, _PROBE_EPOCH_BUCKET + 30),
        ]
        assert stats.by_bucket[("agent", slug, _PROBE_EPOCH_BUCKET)].successes == 2
        assert stats.by_bucket[("agent", slug, _PROBE_EPOCH_BUCKET + 30)].successes == 1
        assert all(start % 30 == 0 for (_r, _m, start) in stats.by_bucket)
        assert stats.successes == 3

    def test_the_window_records_the_served_and_rejected_side_together(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock, wall: _FakeWallClock
    ) -> None:
        client = _make_client(monkeypatch, probe=_probe(), clock=clock)
        stats = RateLimitProbeStats(bucket_width_s=30)

        with (
            patch(
                "tolokaforge.core.llm.client.completion",
                side_effect=[_rate_limit_error(), _completion_response(prompt_tokens=10)],
            ),
            patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
        ):
            _generate(client, observation=self._observation(stats))

        (window,) = stats.by_bucket.values()
        assert (window.successes, window.retries) == (1, 1)
        assert window.prompt_tokens == 10

    def test_the_bucket_cap_drops_visibly_and_keeps_the_totals(
        self, monkeypatch: pytest.MonkeyPatch, clock: _FakeClock, wall: _FakeWallClock
    ) -> None:
        client = _make_client(monkeypatch, probe=_probe(), clock=clock)
        stats = RateLimitProbeStats(bucket_width_s=30, max_buckets=1)
        observation = self._observation(stats)

        with (
            patch(
                "tolokaforge.core.llm.client.completion",
                return_value=_completion_response(prompt_tokens=10, completion_tokens=1),
            ),
            patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0),
        ):
            for offset in (0.0, 30.0, 60.0):
                wall.epoch = _PROBE_EPOCH + offset
                _generate(client, observation=observation)

        assert len(stats.by_bucket) == 1
        assert stats.dropped_buckets == 2
        # The series truncated; the cumulative record did not.
        assert stats.successes == 3
        assert stats.prompt_tokens == 30
        assert stats.by_role_model[("agent", "openrouter/anthropic/claude-3-haiku")].successes == 3
