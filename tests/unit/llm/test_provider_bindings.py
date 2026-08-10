"""Provider-binding schema and bundled snapshot at
:mod:`tolokaforge.core.llm.providers`.

Locks the shape of every shipped ``providers.yaml`` entry, the unknown-provider
fall-through, and the frozen-model contract. The engine consumers migrate in
later stages of #935 — Stage 1 only lands the data seam.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tolokaforge.core.llm.providers import (
    ProviderBinding,
    SlugRewrite,
    _load_bundled_providers,
    get_provider_binding,
)

pytestmark = pytest.mark.unit


SHIPPED_PROVIDERS = ("nova", "openrouter", "openai", "anthropic", "gemini", "mock")


def test_every_shipped_provider_loads() -> None:
    """The bundled ``providers.yaml`` validates for every shipped entry."""
    bindings = _load_bundled_providers()
    assert set(bindings) == set(SHIPPED_PROVIDERS)
    for name, binding in bindings.items():
        assert isinstance(binding, ProviderBinding), name


def test_nova_binding_matches_current_engine_hardcodes() -> None:
    """Nova's binding reproduces today's three-site engine behaviour verbatim."""
    binding = get_provider_binding("nova")

    assert binding.endpoint == "https://api.nova.amazon.com/v1"
    assert binding.api_base_env == "NOVA_API_BASE"
    assert binding.api_key_env == "NOVA_API_KEY"
    assert binding.api_keys_env is None
    assert binding.unroutable is True
    assert binding.custom_llm_provider == "openai"
    assert binding.format_model_name_bare is True
    assert binding.kwargs_pin_transport is True
    assert binding.slug_rewrite == SlugRewrite(strip_prefix="nova/", ensure_prefix="openai/")


def test_openrouter_binding_captures_rotation_env_vars() -> None:
    """OpenRouter's binding names its key + rotation env vars and custom hint."""
    binding = get_provider_binding("openrouter")

    assert binding.api_key_env == "OPENROUTER_API_KEY"
    assert binding.api_keys_env == "OPENROUTER_API_KEYS"
    assert binding.custom_llm_provider == "openrouter"
    assert binding.unroutable is False
    assert binding.kwargs_pin_transport is False
    assert binding.slug_rewrite is None


def test_mock_binding_is_unroutable_only() -> None:
    """``mock`` declares ``unroutable=True`` with every other field default."""
    binding = get_provider_binding("mock")

    assert binding.unroutable is True
    assert binding.endpoint is None
    assert binding.api_key_env is None
    assert binding.api_keys_env is None
    assert binding.custom_llm_provider is None
    assert binding.rate_limit_patterns == ()
    assert binding.format_model_name_bare is False
    assert binding.kwargs_pin_transport is False
    assert binding.slug_rewrite is None


@pytest.mark.parametrize("provider", ("openai", "anthropic", "gemini"))
def test_plain_providers_are_routable_and_carry_no_transport_overrides(provider: str) -> None:
    """OpenAI / Anthropic / Gemini use litellm defaults — no pinned transport."""
    binding = get_provider_binding(provider)

    assert binding.unroutable is False
    assert binding.endpoint is None
    assert binding.api_base_env is None
    assert binding.api_key_env is None
    assert binding.api_keys_env is None
    assert binding.custom_llm_provider is None
    assert binding.format_model_name_bare is False
    assert binding.kwargs_pin_transport is False
    assert binding.slug_rewrite is None


def test_rate_limit_patterns_are_shared_across_non_mock_providers() -> None:
    """Every non-mock provider ships the same rate-limit pattern list."""
    non_mock = [get_provider_binding(p) for p in SHIPPED_PROVIDERS if p != "mock"]
    reference = non_mock[0].rate_limit_patterns
    assert reference  # non-empty
    for binding in non_mock[1:]:
        assert binding.rate_limit_patterns == reference


def test_unknown_provider_falls_through_to_default() -> None:
    """A provider absent from ``providers.yaml`` resolves to a default binding."""
    default = ProviderBinding()
    assert get_provider_binding("wat") == default
    assert get_provider_binding("") == default


def test_lookup_key_is_first_slash_segment_lowercased() -> None:
    """``openrouter/google`` resolves to the ``openrouter`` entry."""
    compound = get_provider_binding("openrouter/google")
    assert compound == get_provider_binding("openrouter")

    upper = get_provider_binding("Nova")
    assert upper == get_provider_binding("nova")


def test_provider_binding_rejects_unknown_fields() -> None:
    """``extra="forbid"`` traps YAML typos at load time."""
    with pytest.raises(ValidationError):
        ProviderBinding.model_validate({"unknown_field": 1})


def test_provider_binding_is_frozen() -> None:
    """Mutation of a resolved binding raises ``ValidationError``."""
    binding = get_provider_binding("nova")
    with pytest.raises(ValidationError):
        binding.endpoint = "https://elsewhere.example/v1"  # type: ignore[misc]


def test_slug_rewrite_is_frozen() -> None:
    """``SlugRewrite`` mutation raises ``ValidationError``."""
    rewrite = SlugRewrite(strip_prefix="a/", ensure_prefix="b/")
    with pytest.raises(ValidationError):
        rewrite.strip_prefix = "changed/"  # type: ignore[misc]


def test_slug_rewrite_rejects_unknown_fields() -> None:
    """``SlugRewrite`` also enforces ``extra="forbid"``."""
    with pytest.raises(ValidationError):
        SlugRewrite.model_validate({"strip_prefix": "a/", "extra": "nope"})
