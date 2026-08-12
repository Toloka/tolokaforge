"""Rate-limit text patterns come off the provider binding, not a module const.

Every shipped provider carries the same anchored-shapes list in its
``providers.yaml`` entry, so ``matches_rate_limit_text`` on a live provider
binding still fires on today's canonical HTTP-429 shape. A synthetic binding
with a bespoke pattern list matches on its own prose and rejects the default
shapes — the whole point of the per-provider seam.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm.client import LLMClient, matches_rate_limit_text
from tolokaforge.core.llm.providers import (
    ProviderBinding,
    compile_rate_limit_patterns,
    get_provider_binding,
)
from tolokaforge.core.models import ModelConfig

pytestmark = pytest.mark.unit


# Every provider entry in the bundled providers.yaml. Deliberately explicit —
# a new entry needs to add itself here so the guarantee is opt-in per provider.
_SHIPPED_PROVIDERS_WITH_PATTERNS = ("nova", "openrouter", "openai", "anthropic", "gemini")


@pytest.mark.parametrize("provider", _SHIPPED_PROVIDERS_WITH_PATTERNS)
def test_every_shipped_provider_matches_the_canonical_429_shape(provider: str) -> None:
    binding = get_provider_binding(provider)
    patterns = compile_rate_limit_patterns(binding.rate_limit_patterns)

    assert matches_rate_limit_text("Error code: 429 - slow down", patterns) is True


def test_mock_provider_ships_no_patterns_so_matches_nothing() -> None:
    """``mock`` returns without ever calling a real transport (see
    ``LLMClient._mock_generate``), so it deliberately ships an empty pattern
    list — a rate-limit-shaped prose can never arrive on that path."""
    binding = get_provider_binding("mock")
    patterns = compile_rate_limit_patterns(binding.rate_limit_patterns)

    assert binding.rate_limit_patterns == ()
    assert matches_rate_limit_text("Error code: 429", patterns) is False


def test_synthetic_provider_binding_uses_its_own_patterns_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider whose binding overrides ``rate_limit_patterns`` matches on
    its own prose and NOT on the default HTTP-429 shape — the whole point of
    the per-provider mechanism."""
    binding = ProviderBinding(rate_limit_patterns=(r"\bacme_quota_exceeded\b",))
    patterns = compile_rate_limit_patterns(binding.rate_limit_patterns)

    assert matches_rate_limit_text("acme_quota_exceeded reason=throttle", patterns) is True
    assert matches_rate_limit_text("Error code: 429", patterns) is False


def test_llm_client_compiles_its_bindings_patterns_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client resolves its provider binding once at ``__init__`` and
    compiles the patterns into an immutable tuple that
    :meth:`LLMClient._is_rate_limit_exception` closes over. Swapping the
    binding at run time never changes what the compiled cache holds — that
    invariant is what makes the seam safe for tenacity's hot path."""
    binding = ProviderBinding(rate_limit_patterns=(r"\bacme_quota_exceeded\b",))

    import tolokaforge.core.llm.client as client_module

    original = client_module.get_provider_binding

    def _lookup(provider: str) -> ProviderBinding:
        if provider == "acme":
            return binding
        return original(provider)

    monkeypatch.setattr(client_module, "get_provider_binding", _lookup)

    client = LLMClient(ModelConfig(provider="acme", name="acme-lite"))

    assert client._is_rate_limit_exception(RuntimeError("acme_quota_exceeded reason=throttle"))
    # Default HTTP-429 shape is NOT in this binding's pattern list, so an
    # untyped 429-prose exception is not classified — the synthetic provider
    # actually differentiates.
    assert not client._is_rate_limit_exception(RuntimeError("Error code: 429"))
