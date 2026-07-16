"""LLM-call trio emission from :meth:`LLMClient.generate`.

Locks the observation contract added in #389:

- ``observation=None`` (default) → zero events, output byte-identical to
  the pre-observation path.
- ``observation`` supplied → exactly one ``llm_call_started`` /
  ``llm_call_finished`` pair per outer-retry attempt, matching
  ``(trial_id, role, provider, model, attempt)`` on every pair.
- Failed attempts surface via ``llm_call_finished`` with
  ``error = f"{ExcType}: {msg}"``; success surfaces with ``error is None``.
- ``llm_retry_scheduled`` fires between attempts, from the tenacity
  ``before_sleep`` hook — **before** the sleep, so a display can render
  "next attempt in Xs" while the backoff is still in flight.
- ``attempt`` is 1-indexed and monotonic across the multi-attempt cases.

The tests stub ``client._retry_sleep`` so ``wait_exponential(min=4)``
does not add ~60s of real wall time across a 5-attempt case.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.core.llm.client import LLMClient
from tolokaforge.core.models import Message, MessageRole, ModelConfig
from tolokaforge.core.run_display_events import LLMCallObservation

pytestmark = pytest.mark.unit


class _RecordingEvents:
    """Capture every :class:`RunDisplayEvents` invocation as ``(name, kwargs)``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run_started(self, **kwargs: Any) -> None:
        self.calls.append(("run_started", kwargs))

    def trial_started(self, **kwargs: Any) -> None:
        self.calls.append(("trial_started", kwargs))

    def trial_progress(self, **kwargs: Any) -> None:
        self.calls.append(("trial_progress", kwargs))

    def trial_completed(self, **kwargs: Any) -> None:
        self.calls.append(("trial_completed", kwargs))

    def trial_failed(self, **kwargs: Any) -> None:
        self.calls.append(("trial_failed", kwargs))

    def judgment_scored(self, **kwargs: Any) -> None:
        self.calls.append(("judgment_scored", kwargs))

    def run_finished(self, **kwargs: Any) -> None:
        self.calls.append(("run_finished", kwargs))

    def phase_changed(self, **kwargs: Any) -> None:
        self.calls.append(("phase_changed", kwargs))

    def trial_provisioned(self, **kwargs: Any) -> None:
        self.calls.append(("trial_provisioned", kwargs))

    def llm_call_started(self, **kwargs: Any) -> None:
        self.calls.append(("llm_call_started", kwargs))

    def llm_call_finished(self, **kwargs: Any) -> None:
        self.calls.append(("llm_call_finished", kwargs))

    def llm_retry_scheduled(self, **kwargs: Any) -> None:
        self.calls.append(("llm_retry_scheduled", kwargs))


def _normal_response(content: str = "hello") -> MagicMock:
    """A clean litellm-shape completion — no synthetic-envelope marker."""
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


def _make_client(monkeypatch: pytest.MonkeyPatch) -> LLMClient:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-sk-llm-events")
    client = LLMClient(ModelConfig(provider="openrouter", name="anthropic/claude-3-haiku"))
    # Fast: stub outer-retry backoff so 5-attempt failure runs instantly.
    client._retry_sleep = lambda _s: None
    return client


def _make_observation(events: _RecordingEvents, *, role: str = "agent") -> LLMCallObservation:
    return LLMCallObservation(events=events, trial_id="task_x:0", role=role)  # type: ignore[arg-type]


def _messages() -> list[Message]:
    return [Message(role=MessageRole.USER, content="hi")]


def _filter(recording: _RecordingEvents, name: str) -> list[dict[str, Any]]:
    return [kw for n, kw in recording.calls if n == name]


class TestSingleAttemptSuccess:
    """Success on attempt 1 fires exactly one started/finished pair."""

    def test_fires_started_then_finished_with_success_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client(monkeypatch)
        events = _RecordingEvents()
        observation = _make_observation(events)

        with patch("tolokaforge.core.llm.client.completion", return_value=_normal_response("ok")):
            with patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0):
                result = client.generate(system="s", messages=_messages(), observation=observation)

        assert result.text == "ok"
        starts = _filter(events, "llm_call_started")
        finishes = _filter(events, "llm_call_finished")
        retries = _filter(events, "llm_retry_scheduled")
        assert len(starts) == 1
        assert len(finishes) == 1
        assert retries == []
        assert starts[0] == {
            "trial_id": "task_x:0",
            "role": "agent",
            "provider": "openrouter",
            "model": client.model_name,
            "attempt": 1,
        }
        finished = finishes[0]
        assert finished["trial_id"] == "task_x:0"
        assert finished["role"] == "agent"
        assert finished["provider"] == "openrouter"
        assert finished["model"] == client.model_name
        assert finished["attempt"] == 1
        assert finished["error"] is None
        assert finished["duration_s"] >= 0.0

    def test_observation_none_fires_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Backward-compat: default call path must be output-identical."""
        client = _make_client(monkeypatch)
        events = _RecordingEvents()
        with patch("tolokaforge.core.llm.client.completion", return_value=_normal_response("ok")):
            with patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0):
                result = client.generate(system="s", messages=_messages())
        assert result.text == "ok"
        assert events.calls == []


class TestRetryFires:
    """Failed attempts fire finished(error=...) then retry_scheduled then next started."""

    def test_two_failures_then_success_fires_three_pairs_two_retries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client(monkeypatch)
        events = _RecordingEvents()
        observation = _make_observation(events)

        boom = ValueError("kaboom")
        good = _normal_response("recovered")
        with patch(
            "tolokaforge.core.llm.client.completion",
            side_effect=[boom, boom, good],
        ):
            with patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0):
                result = client.generate(system="s", messages=_messages(), observation=observation)

        assert result.text == "recovered"
        starts = _filter(events, "llm_call_started")
        finishes = _filter(events, "llm_call_finished")
        retries = _filter(events, "llm_retry_scheduled")

        # Three attempts → three started/finished pairs, two retries between them.
        assert [s["attempt"] for s in starts] == [1, 2, 3]
        assert [f["attempt"] for f in finishes] == [1, 2, 3]
        assert [r["attempt"] for r in retries] == [1, 2]

        # First two attempts carry error strings; final is None.
        # `_call_with_key_rotation` wraps generic upstream exceptions in
        # RuntimeError ("LLM API call failed: ..."), so the surface class
        # on `error` and `reason` is the wrapped RuntimeError, not the raw
        # ValueError raised by the litellm-completion stub.
        assert finishes[0]["error"] == "RuntimeError: LLM API call failed: kaboom"
        assert finishes[1]["error"] == "RuntimeError: LLM API call failed: kaboom"
        assert finishes[2]["error"] is None

        # retry_scheduled carries the exception class name as reason and a positive backoff.
        for retry in retries:
            assert retry["reason"] == "RuntimeError"
            assert retry["next_attempt_in_s"] > 0.0
            assert retry["trial_id"] == "task_x:0"
            assert retry["role"] == "agent"

    def test_five_failures_reraise_exhausted_fires_five_pairs_four_retries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Locks ``stop_after_attempt(5)`` + ``reraise=True`` under the new controller."""
        client = _make_client(monkeypatch)
        events = _RecordingEvents()
        observation = _make_observation(events)

        with patch("tolokaforge.core.llm.client.completion", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                client.generate(system="s", messages=_messages(), observation=observation)

        starts = _filter(events, "llm_call_started")
        finishes = _filter(events, "llm_call_finished")
        retries = _filter(events, "llm_retry_scheduled")

        # 5 attempts, 4 sleeps between them (no retry fires after the last attempt).
        assert [s["attempt"] for s in starts] == [1, 2, 3, 4, 5]
        assert [f["attempt"] for f in finishes] == [1, 2, 3, 4, 5]
        assert [r["attempt"] for r in retries] == [1, 2, 3, 4]

        # `_call_with_key_rotation` wraps the raw exception in RuntimeError
        # ("LLM API call failed: ..."), so surface class is RuntimeError.
        for finished in finishes:
            assert finished["error"] is not None
            assert finished["error"].startswith("RuntimeError:")
        for retry in retries:
            assert retry["reason"] == "RuntimeError"


class TestRetryScheduledOrdering:
    """`llm_retry_scheduled` must fire BEFORE the sleep hook — the panel needs
    the ETA before the pause, not after it."""

    def test_retry_scheduled_precedes_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _make_client(monkeypatch)
        events = _RecordingEvents()
        observation = _make_observation(events)

        # Recording sleep hook: append a marker "SLEEP" into events.calls when
        # the controller calls sleep(), so the ordering is directly assertable.
        def _recording_sleep(seconds: float) -> None:
            events.calls.append(("SLEEP", {"seconds": seconds}))

        client._retry_sleep = _recording_sleep

        good = _normal_response("done")
        with patch(
            "tolokaforge.core.llm.client.completion",
            side_effect=[ValueError("x"), good],
        ):
            with patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0):
                client.generate(system="s", messages=_messages(), observation=observation)

        # Extract the ordered kinds.
        kinds = [name for name, _ in events.calls]
        # Contract: `llm_retry_scheduled` fires from `before_sleep`, immediately
        # before the controller invokes `sleep(...)`. So the SLEEP marker must
        # follow the corresponding llm_retry_scheduled.
        assert "llm_retry_scheduled" in kinds
        assert "SLEEP" in kinds
        assert kinds.index("llm_retry_scheduled") < kinds.index("SLEEP")

        # And the retry event must sit between the two started events for
        # attempts 1 and 2 — never after the final attempt.
        started_indices = [i for i, k in enumerate(kinds) if k == "llm_call_started"]
        retry_index = kinds.index("llm_retry_scheduled")
        assert started_indices[0] < retry_index < started_indices[1]


class TestUserRole:
    """The observation carries an ``LLMCallRole`` — role="user" must round-trip."""

    def test_role_user_propagates_to_started_and_finished(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client(monkeypatch)
        events = _RecordingEvents()
        observation = _make_observation(events, role="user")

        with patch("tolokaforge.core.llm.client.completion", return_value=_normal_response("ok")):
            with patch("tolokaforge.core.llm.client.estimate_cost", return_value=0.0):
                client.generate(system="s", messages=_messages(), observation=observation)

        starts = _filter(events, "llm_call_started")
        finishes = _filter(events, "llm_call_finished")
        assert len(starts) == 1
        assert len(finishes) == 1
        assert starts[0]["role"] == "user"
        assert finishes[0]["role"] == "user"
