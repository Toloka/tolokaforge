"""The client half of gateway routing: what actually reaches litellm.

The resolver is pinned in test_gateway_route.py. This pins the wiring, because a
resolver that returns the right answer into a call site that ignores it is a feature
that does not exist. Each test here fails if the payload in ``_build_kwargs`` or the
constructor's three-outcome split is removed.

The catalog fetch is monkeypatched throughout: unit tests do no network I/O, and the
module-level cache is cleared so ordering cannot leak a catalog between tests.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

from tolokaforge.core.llm.client import LLMClient
from tolokaforge.core.models import Message, MessageRole, ModelConfig
from tolokaforge.secrets import DictProvider, SecretManager
from tolokaforge.secrets import manager as secrets_manager

pytestmark = pytest.mark.unit

GATEWAY = "https://gateway.example.com"
MODEL = "anthropic/claude-sonnet-4.6"
FULL = f"openrouter/{MODEL}"


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
    """The directory conftest already clears the catalog cache; this restores the
    secrets singleton and os.environ, which client construction mutates."""
    original_manager = secrets_manager._default_manager
    original_env = dict(os.environ)
    try:
        yield
    finally:
        secrets_manager._default_manager = original_manager
        os.environ.clear()
        os.environ.update(original_env)


def _client(
    catalog: frozenset[str] | None, monkeypatch: pytest.MonkeyPatch, **extra: str
) -> LLMClient:
    secrets_manager._default_manager = SecretManager(
        [
            DictProvider(
                {
                    "OPENROUTER_API_KEY": "sk-or",
                    "LLM_PROXY_BASE_URL": GATEWAY,
                    "LLM_PROXY_API_KEY": "sk-gw",
                    **extra,
                }
            )
        ]
    )
    # client.py imports the name, so only its binding is live.
    monkeypatch.setattr(
        "tolokaforge.core.llm.client.fetch_gateway_catalog", lambda *_a, **_k: catalog
    )
    return LLMClient(ModelConfig(provider="openrouter", name=MODEL))


def _kwargs(client: LLMClient) -> dict[str, Any]:
    return client._build_kwargs(
        system="s",
        messages=[Message(role=MessageRole.USER, content="x")],
        tools=None,
        tool_choice=None,
        temperature=None,
        seed=None,
        reasoning=None,
        top_p=None,
        max_tokens=None,
    )


class TestCatalogNamesTheModel:
    def test_the_gateway_route_name_replaces_the_model_string(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kwargs = _kwargs(_client(frozenset({FULL}), monkeypatch))
        assert kwargs["model"] == FULL
        assert kwargs["api_base"] == GATEWAY

    def test_the_bare_name_is_used_when_that_is_what_the_gateway_serves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kwargs = _kwargs(_client(frozenset({MODEL}), monkeypatch))
        assert kwargs["model"] == MODEL

    def test_the_dialect_becomes_the_gateways_own(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without this litellm adds OpenRouter's ``usage`` extension, which every
        other upstream rejects."""
        assert _kwargs(_client(frozenset({FULL}), monkeypatch))["custom_llm_provider"] == "openai"


class TestCatalogAnswersAndOmits:
    def test_the_call_goes_to_the_provider_directly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A gateway that does not serve the model must not intercept it."""
        client = _client(frozenset({"some/other-model"}), monkeypatch)
        kwargs = _kwargs(client)
        assert client._proxy is None
        assert "api_base" not in kwargs
        assert kwargs["model"] == FULL


class TestCatalogUnreadable:
    @pytest.mark.parametrize("catalog", [None], ids=["unreadable"])
    def test_the_gateway_is_kept_with_the_untranslated_name(
        self, catalog: frozenset[str] | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unreadable is not absence: leaving the gateway on a blip is the
        unattributed-spend outcome this transport exists to prevent."""
        client = _client(catalog, monkeypatch)
        kwargs = _kwargs(client)
        assert client._proxy is not None
        assert kwargs["api_base"] == GATEWAY
        assert kwargs["model"] == FULL
        assert kwargs.get("custom_llm_provider") != "openai"


class TestPreferenceReachesTheClient:
    def test_the_preferred_namespace_picks_the_route(self, monkeypatch: pytest.MonkeyPatch) -> None:
        both = frozenset({FULL, MODEL})
        kwargs = _kwargs(_client(both, monkeypatch, LLM_PROXY_PREFERRED_ROUTE="openrouter/"))
        assert kwargs["model"] == FULL


class TestModelConfigIsUntouched:
    def test_rewriting_the_wire_name_does_not_move_presets_or_pricing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both key off ModelConfig, so the wire rename must not reach them."""
        client = _client(frozenset({MODEL}), monkeypatch)
        assert client.model_name == FULL
        assert client.config.name == MODEL
        assert client.config.provider == "openrouter"


class TestOpenRouterOnlyBodyFieldsAreDropped:
    def test_the_provider_pin_does_not_ride_to_the_gateway(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same class as the usage extension: a non-OpenRouter upstream rejects it."""
        from tolokaforge.core.models import OpenRouterConfig

        secrets_manager._default_manager = SecretManager(
            [
                DictProvider(
                    {
                        "OPENROUTER_API_KEY": "sk-or",
                        "LLM_PROXY_BASE_URL": GATEWAY,
                        "LLM_PROXY_API_KEY": "sk-gw",
                    }
                )
            ]
        )
        monkeypatch.setattr(
            "tolokaforge.core.llm.client.fetch_gateway_catalog", lambda *_a, **_k: frozenset({FULL})
        )
        config = ModelConfig(
            provider="openrouter",
            name=MODEL,
            openrouter=OpenRouterConfig(provider_order=["anthropic"]),
        )
        kwargs = _kwargs(LLMClient(config))
        assert "provider" not in kwargs.get("extra_body", {})
        assert kwargs["custom_llm_provider"] == "openai"

    def test_it_still_rides_when_the_call_is_not_routed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tolokaforge.core.models import OpenRouterConfig

        secrets_manager._default_manager = SecretManager(
            [DictProvider({"OPENROUTER_API_KEY": "sk"})]
        )
        config = ModelConfig(
            provider="openrouter",
            name=MODEL,
            openrouter=OpenRouterConfig(provider_order=["anthropic"]),
        )
        kwargs = _kwargs(LLMClient(config))
        assert kwargs["extra_body"]["provider"]["order"] == ["anthropic"]
