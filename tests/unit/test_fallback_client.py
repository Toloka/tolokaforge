"""Ordered per-trial cursor tests for :class:`FallbackLLMClient`.

Locks the Stage-3 contract in ``tolokaforge/core/llm/fallback_client.py``:

* On any exception raised by the cursor-current client's ``generate`` the
  cursor advances one step and the call retries once against the next
  client.
* Chain-exhausted → the final exception propagates.
* Success on the primary → cursor stays at 0 (no fallback logic invoked).
* Cursor advances at ``generate()`` granularity — a mid-trial swap on
  turn N > 0 continues subsequent turns on the fallback (locked by
  asserting cursor after successive calls on the same instance).
* Each instance owns its own cursor — the orchestrator constructs one
  per trial, so cursors do not leak across trials.
* The wrapper's forwarding ``config`` / ``capabilities`` properties
  return the cursor-current client's values, so
  :class:`ConductorContext` consumers reading them see the model that
  actually served the last successful call.
"""

from __future__ import annotations

from typing import Any

import pytest

from tolokaforge.core.llm.client import LLMApiTimeoutError, LLMClient
from tolokaforge.core.llm.fallback_client import FallbackLLMClient
from tolokaforge.core.models import ModelConfig

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _StubGeneration:
    """Placeholder :class:`GenerationResult` stand-in for tests.

    :meth:`LLMClient.generate` normally returns a full ``GenerationResult``;
    the wrapper never inspects the value, so a tagged sentinel is enough
    to prove which chain link served the call.
    """

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name


def _install_scripted_generate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    outcomes_by_name: dict[str, list[Any]],
) -> dict[str, int]:
    """Patch :meth:`LLMClient.generate` with a per-model scripted sequence.

    ``outcomes_by_name[model_name]`` is a list of per-call outcomes; each
    entry is either a ``BaseException`` instance (raised) or a return
    value (returned). Returns a mutable dict tracking how many times each
    model was called so tests can assert per-model invocation counts.
    """
    counts: dict[str, int] = dict.fromkeys(outcomes_by_name, 0)

    def scripted_generate(self: LLMClient, *args: object, **kwargs: object) -> object:
        name = self.config.name
        idx = counts[name]
        counts[name] += 1
        outcome = outcomes_by_name[name][idx]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(LLMClient, "generate", scripted_generate)
    return counts


def _primary() -> ModelConfig:
    return ModelConfig(provider="openai", name="primary-model")


def _fallback(idx: int) -> ModelConfig:
    return ModelConfig(provider="anthropic", name=f"fallback-{idx}")


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_requires_at_least_one_fallback() -> None:
    with pytest.raises(ValueError, match="requires at least one fallback"):
        FallbackLLMClient(primary=_primary(), fallbacks=[])


def test_cursor_starts_at_zero() -> None:
    wrapper = FallbackLLMClient(primary=_primary(), fallbacks=[_fallback(1)])
    assert wrapper.cursor == 0
    assert wrapper.chain[0].name == "primary-model"


# ---------------------------------------------------------------------------
# Fallback advance-and-retry
# ---------------------------------------------------------------------------


def test_success_on_primary_leaves_cursor_at_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _StubGeneration("primary-model")
    _install_scripted_generate(
        monkeypatch,
        outcomes_by_name={
            "primary-model": [result],
            "fallback-1": [],
        },
    )
    wrapper = FallbackLLMClient(primary=_primary(), fallbacks=[_fallback(1)])

    assert wrapper.generate("sys", []) is result
    assert wrapper.cursor == 0


def test_timeout_on_primary_advances_and_retries_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _StubGeneration("fallback-1")
    counts = _install_scripted_generate(
        monkeypatch,
        outcomes_by_name={
            "primary-model": [LLMApiTimeoutError("primary timed out")],
            "fallback-1": [result],
        },
    )
    wrapper = FallbackLLMClient(primary=_primary(), fallbacks=[_fallback(1)])

    assert wrapper.generate("sys", []) is result
    assert wrapper.cursor == 1
    assert counts["primary-model"] == 1
    assert counts["fallback-1"] == 1


def test_non_timeout_runtime_error_also_advances_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Locks D6 — the fallback fires on ANY exception, not just timeouts."""
    result = _StubGeneration("fallback-1")
    _install_scripted_generate(
        monkeypatch,
        outcomes_by_name={
            "primary-model": [RuntimeError("provider 5xx")],
            "fallback-1": [result],
        },
    )
    wrapper = FallbackLLMClient(primary=_primary(), fallbacks=[_fallback(1)])

    assert wrapper.generate("sys", []) is result
    assert wrapper.cursor == 1


def test_chain_exhaustion_reraises_last_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    final_error = RuntimeError("final fallback dead")
    _install_scripted_generate(
        monkeypatch,
        outcomes_by_name={
            "primary-model": [LLMApiTimeoutError("primary dead")],
            "fallback-1": [RuntimeError("intermediate dead")],
            "fallback-2": [final_error],
        },
    )
    wrapper = FallbackLLMClient(primary=_primary(), fallbacks=[_fallback(1), _fallback(2)])

    with pytest.raises(RuntimeError, match="final fallback dead"):
        wrapper.generate("sys", [])
    # Cursor advanced to the last link before the raise; no further advance.
    assert wrapper.cursor == 2


def test_second_call_on_same_instance_stays_on_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Locks D4 — cursor advances at generate() granularity, not per-trial.

    First call fails on primary and succeeds on fallback-1; second call
    on the same instance issues directly against fallback-1 (cursor
    stayed advanced), without re-attempting the primary.
    """
    result_1 = _StubGeneration("fallback-1")
    result_2 = _StubGeneration("fallback-1")
    counts = _install_scripted_generate(
        monkeypatch,
        outcomes_by_name={
            "primary-model": [LLMApiTimeoutError("primary dead")],
            "fallback-1": [result_1, result_2],
        },
    )
    wrapper = FallbackLLMClient(primary=_primary(), fallbacks=[_fallback(1)])

    assert wrapper.generate("sys", []) is result_1
    assert wrapper.cursor == 1
    assert wrapper.generate("sys", []) is result_2
    assert wrapper.cursor == 1
    # Primary was hit exactly once (on the first call, before the swap).
    assert counts["primary-model"] == 1
    assert counts["fallback-1"] == 2


def test_separate_instances_have_independent_cursors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The orchestrator builds one :class:`FallbackLLMClient` per trial;
    per-trial cursors must not leak across instances."""
    _install_scripted_generate(
        monkeypatch,
        outcomes_by_name={
            "primary-model": [
                LLMApiTimeoutError("dead-1"),
                _StubGeneration("primary-model"),
            ],
            "fallback-1": [_StubGeneration("fallback-1")],
        },
    )
    wrapper_a = FallbackLLMClient(primary=_primary(), fallbacks=[_fallback(1)])
    wrapper_b = FallbackLLMClient(primary=_primary(), fallbacks=[_fallback(1)])

    wrapper_a.generate("sys", [])  # swaps A to fallback-1
    assert wrapper_a.cursor == 1
    # A fresh instance re-enters at the primary.
    assert wrapper_b.cursor == 0


# ---------------------------------------------------------------------------
# Forwarded LLMClient surface (config, capabilities)
# ---------------------------------------------------------------------------


def test_config_property_reflects_current_link_after_advance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _StubGeneration("fallback-1")
    _install_scripted_generate(
        monkeypatch,
        outcomes_by_name={
            "primary-model": [LLMApiTimeoutError("primary dead")],
            "fallback-1": [result],
        },
    )
    wrapper = FallbackLLMClient(primary=_primary(), fallbacks=[_fallback(1)])
    assert wrapper.config.name == "primary-model"

    wrapper.generate("sys", [])
    assert wrapper.config.name == "fallback-1"
    assert wrapper.config.provider == "anthropic"


def test_capabilities_property_reflects_current_link_after_advance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_scripted_generate(
        monkeypatch,
        outcomes_by_name={
            "primary-model": [LLMApiTimeoutError("primary dead")],
            "fallback-1": [_StubGeneration("fallback-1")],
        },
    )
    wrapper = FallbackLLMClient(primary=_primary(), fallbacks=[_fallback(1)])
    primary_caps_id = id(wrapper.capabilities)

    wrapper.generate("sys", [])
    # After the swap, the forwarded capabilities is the fallback client's
    # (a distinct object; ``LLMClient`` builds fresh capabilities per init).
    assert id(wrapper.capabilities) != primary_caps_id
