"""Contract: the gateway allow-list matches litellm's OpenAI-envelope providers.

:data:`~tolokaforge.core.llm.proxy.DEFAULT_ROUTED_PROVIDERS` is not a
preference. It is exactly the set of providers whose litellm transport, handed
an ``api_base``, targets ``{api_base}/chat/completions`` — the shape a LiteLLM
proxy (or any gateway presenting the same surface) serves.

That makes the allow-list an assumption about a *third-party library*, which is
the fragile kind: a litellm bump could change a provider's transport and the
gateway would start posting to a route the gateway does not serve, with nothing
in our own code changing. Every trial would fail at the wire with no hint that a
dependency moved. This test pins the assumption so the break is loud and lands
here instead.

If a test here fails after a litellm upgrade, the fix is a deliberate decision,
not a patch: either widen / narrow ``DEFAULT_ROUTED_PROVIDERS`` to match the new
reality, or pin litellm. Do not adjust the expected URL to whatever litellm now
returns without checking what the gateway actually serves.

The ``ProviderConfigManager`` entry point is litellm-internal enough that it may
itself move between versions. That is acceptable and intentional: an ImportError
here is also a signal worth reading.
"""

from __future__ import annotations

import pytest
from litellm.types.utils import LlmProviders
from litellm.utils import ProviderConfigManager

from tolokaforge.core.llm.proxy import DEFAULT_ROUTED_PROVIDERS

pytestmark = pytest.mark.unit

_GATEWAY_BASE = "https://gateway.example.com"

#: A representative model per provider. litellm picks a transport config from
#: the provider, but some configs inspect the model name, so use realistic ones.
_REPRESENTATIVE_MODEL = {
    "openrouter": "anthropic/claude-opus-4.7",
    "openai": "gpt-5.6",
    "anthropic": "claude-opus-4.7",
    "gemini": "gemini-2.5-pro",
}


def _complete_url(provider: str, model: str) -> str:
    config = ProviderConfigManager.get_provider_chat_config(
        model=model, provider=LlmProviders(provider)
    )
    assert config is not None, f"litellm has no chat config for provider {provider!r}"
    return config.get_complete_url(
        api_base=_GATEWAY_BASE,
        api_key="sk-not-a-real-key",
        model=model,
        optional_params={},
        litellm_params={},
    )


@pytest.mark.parametrize("provider", sorted(DEFAULT_ROUTED_PROVIDERS))
def test_routed_providers_target_the_openai_chat_completions_path(provider: str) -> None:
    """Every allow-listed provider must post the OpenAI envelope to the gateway."""
    model = _REPRESENTATIVE_MODEL[provider]
    assert _complete_url(provider, model) == f"{_GATEWAY_BASE}/chat/completions", (
        f"litellm no longer sends {provider!r} to {{api_base}}/chat/completions. "
        f"Routing it through a gateway would target a route the gateway does not "
        f"serve. Re-check DEFAULT_ROUTED_PROVIDERS against the new transport."
    )


def test_every_routed_provider_has_a_representative_model() -> None:
    """Guard against a widened allow-list silently skipping coverage above."""
    missing = sorted(DEFAULT_ROUTED_PROVIDERS - _REPRESENTATIVE_MODEL.keys())
    assert not missing, f"add a representative model for {missing} so it is covered here"


@pytest.mark.parametrize("provider", ["anthropic", "gemini"])
def test_native_protocol_providers_are_not_openai_shaped(provider: str) -> None:
    """Document why the allow-list excludes these, and notice if that changes.

    These providers append their own route (``/v1/messages``,
    ``/models/<m>:generateContent``) downstream of ``get_complete_url``, so they
    never resolve to the OpenAI chat-completions path. A failure here means
    litellm made them OpenAI-shaped and the allow-list *could* be widened.
    """
    assert provider not in DEFAULT_ROUTED_PROVIDERS
    assert _complete_url(provider, _REPRESENTATIVE_MODEL[provider]) != (
        f"{_GATEWAY_BASE}/chat/completions"
    )
