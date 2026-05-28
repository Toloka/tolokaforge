"""Unit tests for per-call accounting on :class:`Usage`.

Pins the structural contract that PR #106 is meant to add:

* ``ProviderRawCall`` is a frozen dataclass with the per-call fields used
  by trial-level cost / latency analytics.
* ``Usage.calls`` is a tuple of ``ProviderRawCall``; ``Usage.__add__``
  concatenates it (no latest-wins).
* ``Metrics.usage`` round-trips ``calls`` through Pydantic JSON
  serialisation, including the ``cost_source`` literal.
"""

from __future__ import annotations

import dataclasses

import pytest
from pydantic import ValidationError

from tolokaforge.core.llm.usage import ProviderRawCall, Usage
from tolokaforge.core.models import Metrics

pytestmark = pytest.mark.unit


def _call(**overrides: object) -> ProviderRawCall:
    """Build a fully-populated call record so field renames break tests loudly."""
    base: dict[str, object] = {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "cached_tokens": 10,
        "reasoning_tokens": 5,
        "cache_creation_input_tokens": 20,
        "cache_read_input_tokens": 15,
        "cost_usd": 0.0123,
        "cost_source": "litellm",
        "latency_s": 0.42,
    }
    base.update(overrides)
    return ProviderRawCall(**base)  # type: ignore[arg-type]


class TestUsageCallsConcatenate:
    def test_add_concatenates_call_history(self) -> None:
        c1 = _call(prompt_tokens=100, cost_usd=0.01)
        c2 = _call(prompt_tokens=200, cost_usd=0.02)
        combined = Usage(calls=(c1,)) + Usage(calls=(c2,))

        assert combined.calls == (c1, c2)
        assert len(combined.calls) == 2

    def test_add_empty_calls_preserves_other_side(self) -> None:
        c1 = _call()
        combined = Usage() + Usage(calls=(c1,))
        assert combined.calls == (c1,)

    def test_calls_default_is_immutable_empty_tuple(self) -> None:
        # Frozen dataclass + tuple default → no shared mutable state.
        u1 = Usage()
        u2 = Usage()
        assert u1.calls == () == u2.calls
        assert isinstance(u1.calls, tuple)


class TestMetricsRoundTrip:
    def test_round_trip_preserves_calls_and_cost_source(self) -> None:
        c1 = _call(cost_source="litellm", cost_usd=0.05)
        c2 = _call(cost_source="local", cost_usd=0.03)
        c3 = _call(cost_source="unknown", cost_usd=None)
        metrics = Metrics(usage=Usage(calls=(c1, c2, c3)))

        dumped = metrics.model_dump(mode="json")
        restored = Metrics.model_validate(dumped)

        assert len(restored.usage.calls) == 3
        for original, recovered in zip((c1, c2, c3), restored.usage.calls):
            assert recovered == original
            assert dataclasses.is_dataclass(recovered)

    def test_serializer_emits_dataclass_field_set(self) -> None:
        # The serializer must use dataclasses.asdict so adding a field to
        # ProviderRawCall doesn't silently drop it from YAML.
        metrics = Metrics(usage=Usage(calls=(_call(),)))
        dumped = metrics.model_dump(mode="json")

        assert "calls" in dumped["usage"]
        emitted = dumped["usage"]["calls"][0]
        expected_keys = {f.name for f in dataclasses.fields(ProviderRawCall)}
        assert set(emitted.keys()) == expected_keys


class TestCostSourceLiteral:
    @pytest.mark.parametrize("source", ["litellm", "local", "unknown"])
    def test_accepts_valid_sources(self, source: str) -> None:
        # Validation happens at the Metrics layer (pydantic), so we drive
        # cost_source through a round-trip to exercise the literal type.
        c = _call(cost_source=source)
        m = Metrics(usage=Usage(calls=(c,)))
        restored = Metrics.model_validate(m.model_dump(mode="json"))
        assert restored.usage.calls[0].cost_source == source

    def test_rejects_invalid_source_on_validation(self) -> None:
        # Construct directly (bypass dataclass type-checking, which Python
        # doesn't enforce at runtime) and route through Pydantic validation.
        bad_dump = {
            "usage": {
                "calls": [
                    {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "cached_tokens": 0,
                        "reasoning_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "cost_usd": None,
                        "cost_source": "bogus",
                        "latency_s": 0.0,
                    }
                ]
            }
        }
        with pytest.raises(ValidationError):
            Metrics.model_validate(bad_dump)
