"""Tests for the configurable OpenAI-compatible gateway transport.

Covers the two halves of the feature:

* :mod:`tolokaforge.core.llm.proxy` — resolving and validating the env
  contract.
* :class:`~tolokaforge.core.llm.client.LLMClient` — applying it as a *transport
  swap only*, which is the load-bearing invariant: the litellm model string
  must keep its ``<provider>/<name>`` shape so preset resolution and pricing
  normalisation stay untouched.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

from tolokaforge.core.llm import client as client_module
from tolokaforge.core.llm.client import LLMClient
from tolokaforge.core.llm.proxy import (
    UNROUTABLE_PROVIDERS,
    ProxyConfig,
    ProxyConfigError,
    resolve_proxy_config,
)
from tolokaforge.core.models import Message, MessageRole, ModelConfig
from tolokaforge.secrets import DictProvider, SecretManager
from tolokaforge.secrets import manager as secrets_manager

pytestmark = pytest.mark.unit


@pytest.fixture
def install_secrets() -> Iterator[Any]:
    """Install a dict-backed SecretManager singleton and isolate ``os.environ``.

    The env snapshot is not incidental. ``LLMClient`` construction reaches
    ``os.environ.setdefault`` for provider base URLs, and ``_rotate_key``
    republishes a provider key, so without a restore these tests would leak
    ``OPENROUTER_API_BASE`` / ``NOVA_API_BASE`` / ``OPENROUTER_API_KEY`` into
    the rest of the session. CI runs the whole suite in one interpreter, and a
    stale ``OPENROUTER_API_BASE`` silently redirects every later litellm
    openrouter call — an order-dependent failure that looks like a network
    problem.
    """
    original_manager = secrets_manager._default_manager
    original_env = dict(os.environ)

    def _install(secrets: dict[str, str]) -> None:
        secrets_manager._default_manager = SecretManager([DictProvider(secrets)])

    try:
        yield _install
    finally:
        secrets_manager._default_manager = original_manager
        os.environ.clear()
        os.environ.update(original_env)


def _clear_env(name: str) -> None:
    """Drop ``name`` so a test starts from a known-absent state.

    Safe because the ``install_secrets`` fixture restores ``os.environ``.
    """
    os.environ.pop(name, None)


def _build_kwargs(config: ModelConfig) -> dict[str, Any]:
    """Return the kwargs ``LLMClient`` would hand to litellm for one call."""
    client = LLMClient(config)
    return client._build_kwargs(
        system="You are a test.",
        messages=[Message(role=MessageRole.USER, content="hi")],
        tools=None,
        tool_choice=None,
        temperature=None,
        seed=None,
        reasoning=None,
        top_p=None,
        max_tokens=None,
    )


class TestResolveProxyConfig:
    """The env contract: presence of the base URL is the on-switch."""

    def test_disabled_when_base_url_absent(self, install_secrets) -> None:
        install_secrets({"OPENROUTER_API_KEY": "sk-or-test"})
        assert resolve_proxy_config() is None

    def test_blank_base_url_is_treated_as_disabled(self, install_secrets) -> None:
        install_secrets({"LLM_PROXY_BASE_URL": "   "})
        assert resolve_proxy_config() is None

    def test_base_url_only_is_enough(self, install_secrets) -> None:
        install_secrets({"LLM_PROXY_BASE_URL": "https://gateway.example.com"})
        proxy = resolve_proxy_config()
        assert proxy is not None
        assert proxy.base_url == "https://gateway.example.com"
        assert proxy.api_key is None
        assert proxy.headers == {}
        assert proxy.request_id_header is None
        assert proxy.providers is None

    def test_trailing_slash_is_normalised(self, install_secrets) -> None:
        install_secrets({"LLM_PROXY_BASE_URL": "https://gateway.example.com/"})
        proxy = resolve_proxy_config()
        assert proxy is not None
        assert proxy.base_url == "https://gateway.example.com"

    def test_full_contract_resolves(self, install_secrets) -> None:
        install_secrets(
            {
                "LLM_PROXY_BASE_URL": "https://gateway.example.com",
                "LLM_PROXY_API_KEY": "sk-gateway",
                "LLM_PROXY_HEADERS": '{"X-Team-Id": "research", "X-Cost-Center": 42}',
                "LLM_PROXY_REQUEST_ID_HEADER": "X-Request-Id",
                "LLM_PROXY_PROVIDERS": "openrouter, anthropic",
            }
        )
        proxy = resolve_proxy_config()
        assert proxy is not None
        assert proxy.api_key == "sk-gateway"
        # Scalar JSON values are coerced to strings — headers are wire strings.
        assert proxy.headers == {"X-Team-Id": "research", "X-Cost-Center": "42"}
        assert proxy.request_id_header == "X-Request-Id"
        assert proxy.providers == frozenset({"openrouter", "anthropic"})


class TestProxyConfigValidation:
    """Malformed configuration fails loudly rather than running unattributed."""

    def test_headers_not_json_raises(self, install_secrets) -> None:
        install_secrets(
            {
                "LLM_PROXY_BASE_URL": "https://gateway.example.com",
                "LLM_PROXY_HEADERS": "X-Team-Id=research",
            }
        )
        with pytest.raises(ProxyConfigError, match="LLM_PROXY_HEADERS"):
            resolve_proxy_config()

    def test_headers_json_array_raises(self, install_secrets) -> None:
        install_secrets(
            {
                "LLM_PROXY_BASE_URL": "https://gateway.example.com",
                "LLM_PROXY_HEADERS": '["X-Team-Id"]',
            }
        )
        with pytest.raises(ProxyConfigError, match="must be a JSON object"):
            resolve_proxy_config()

    def test_header_object_value_raises(self, install_secrets) -> None:
        install_secrets(
            {
                "LLM_PROXY_BASE_URL": "https://gateway.example.com",
                "LLM_PROXY_HEADERS": '{"X-Team-Id": {"nested": true}}',
            }
        )
        with pytest.raises(ProxyConfigError, match="must be a scalar"):
            resolve_proxy_config()

    def test_providers_set_but_empty_raises(self, install_secrets) -> None:
        install_secrets(
            {
                "LLM_PROXY_BASE_URL": "https://gateway.example.com",
                "LLM_PROXY_PROVIDERS": " , ,",
            }
        )
        with pytest.raises(ProxyConfigError, match="contains no provider names"):
            resolve_proxy_config()

    def test_unroutable_provider_in_allow_list_raises(self, install_secrets) -> None:
        install_secrets(
            {
                "LLM_PROXY_BASE_URL": "https://gateway.example.com",
                "LLM_PROXY_PROVIDERS": "openrouter,nova",
            }
        )
        with pytest.raises(ProxyConfigError, match="cannot be routed"):
            resolve_proxy_config()

    @pytest.mark.parametrize(
        "orphan",
        [
            "LLM_PROXY_API_KEY",
            "LLM_PROXY_HEADERS",
            "LLM_PROXY_REQUEST_ID_HEADER",
            "LLM_PROXY_PROVIDERS",
        ],
    )
    def test_companion_without_base_url_raises(self, install_secrets, orphan: str) -> None:
        """A typo in the base-URL name must not silently bypass the gateway."""
        install_secrets({orphan: "openrouter" if orphan.endswith("PROVIDERS") else "value"})
        with pytest.raises(ProxyConfigError, match="LLM_PROXY_BASE_URL"):
            resolve_proxy_config()

    def test_no_gateway_vars_at_all_is_silent(self, install_secrets) -> None:
        """The default path stays quiet — this is not a required feature."""
        install_secrets({"OPENROUTER_API_KEY": "sk-or-test"})
        assert resolve_proxy_config() is None


class TestProviderScoping:
    """Which providers the gateway claims."""

    def test_default_scope_is_the_openai_envelope_providers(self) -> None:
        """Only providers whose litellm transport POSTs /chat/completions."""
        proxy = ProxyConfig(base_url="https://gateway.example.com")
        assert proxy.applies_to("openrouter")
        assert proxy.applies_to("openai")

    def test_default_scope_excludes_native_protocol_providers(self) -> None:
        """anthropic/gemini would get their native route appended, not OpenAI's."""
        proxy = ProxyConfig(base_url="https://gateway.example.com")
        assert not proxy.applies_to("anthropic")
        assert not proxy.applies_to("gemini")
        assert not proxy.applies_to("vertex_ai")

    def test_compound_provider_matches_first_segment(self) -> None:
        proxy = ProxyConfig(base_url="https://gateway.example.com")
        assert proxy.applies_to("openrouter/google")

    def test_explicit_allow_list_replaces_the_default(self) -> None:
        proxy = ProxyConfig(
            base_url="https://gateway.example.com",
            providers=frozenset({"anthropic"}),
        )
        assert proxy.applies_to("anthropic")
        assert not proxy.applies_to("openrouter")

    def test_unroutable_providers_never_match(self) -> None:
        """Even an explicit allow-list cannot route these."""
        proxy = ProxyConfig(
            base_url="https://gateway.example.com",
            providers=frozenset(UNROUTABLE_PROVIDERS),
        )
        for provider in UNROUTABLE_PROVIDERS:
            assert not proxy.applies_to(provider), provider

    def test_empty_provider_string_never_matches(self) -> None:
        proxy = ProxyConfig(base_url="https://gateway.example.com")
        assert not proxy.applies_to("")


class TestRequestHeaders:
    """Static headers plus an optional per-request correlation id."""

    def test_static_headers_are_returned(self) -> None:
        proxy = ProxyConfig(
            base_url="https://gateway.example.com",
            headers={"X-Team-Id": "research"},
        )
        assert proxy.request_headers() == {"X-Team-Id": "research"}

    def test_request_id_is_fresh_per_call(self) -> None:
        proxy = ProxyConfig(
            base_url="https://gateway.example.com",
            request_id_header="X-Request-Id",
        )
        first = proxy.request_headers()["X-Request-Id"]
        second = proxy.request_headers()["X-Request-Id"]
        assert first != second

    def test_request_headers_does_not_mutate_static_headers(self) -> None:
        proxy = ProxyConfig(
            base_url="https://gateway.example.com",
            headers={"X-Team-Id": "research"},
            request_id_header="X-Request-Id",
        )
        proxy.request_headers()
        assert proxy.headers == {"X-Team-Id": "research"}


class TestClientAppliesProxy:
    """``LLMClient`` treats the gateway as a transport swap and nothing more."""

    def test_kwargs_carry_base_url_and_key(self, install_secrets) -> None:
        install_secrets(
            {
                "LLM_PROXY_BASE_URL": "https://gateway.example.com",
                "LLM_PROXY_API_KEY": "sk-gateway",
            }
        )
        kwargs = _build_kwargs(ModelConfig(provider="openrouter", name="anthropic/claude-opus-4.7"))
        assert kwargs["api_base"] == "https://gateway.example.com"
        assert kwargs["api_key"] == "sk-gateway"

    def test_model_string_is_unchanged(self, install_secrets) -> None:
        """The invariant that keeps presets and pricing working."""
        install_secrets({"LLM_PROXY_BASE_URL": "https://gateway.example.com"})
        config = ModelConfig(provider="openrouter", name="anthropic/claude-opus-4.7")
        kwargs = _build_kwargs(config)
        assert kwargs["model"] == "openrouter/anthropic/claude-opus-4.7"

    def test_configured_headers_reach_the_request(self, install_secrets) -> None:
        install_secrets(
            {
                "LLM_PROXY_BASE_URL": "https://gateway.example.com",
                "LLM_PROXY_HEADERS": '{"X-Team-Id": "research"}',
                "LLM_PROXY_REQUEST_ID_HEADER": "X-Request-Id",
            }
        )
        kwargs = _build_kwargs(ModelConfig(provider="openrouter", name="anthropic/claude-opus-4.7"))
        headers = kwargs["extra_headers"]
        assert headers["X-Team-Id"] == "research"
        assert headers["X-Request-Id"]

    def test_openrouter_headers_survive_alongside_gateway_headers(self, install_secrets) -> None:
        """The gateway must not drop OpenRouter's own attribution headers."""
        install_secrets(
            {
                "LLM_PROXY_BASE_URL": "https://gateway.example.com",
                "LLM_PROXY_HEADERS": '{"X-Team-Id": "research"}',
            }
        )
        kwargs = _build_kwargs(ModelConfig(provider="openrouter", name="anthropic/claude-opus-4.7"))
        headers = kwargs["extra_headers"]
        assert headers["X-Team-Id"] == "research"
        assert "HTTP-Referer" in headers
        assert "X-Title" in headers

    def test_provider_order_extra_body_is_preserved(self, install_secrets) -> None:
        """Gateway routing must not silently drop upstream provider pinning."""
        install_secrets({"LLM_PROXY_BASE_URL": "https://gateway.example.com"})
        config = ModelConfig(
            provider="openrouter",
            name="anthropic/claude-opus-4.7",
            openrouter={"provider_order": ["Together"], "allow_fallbacks": False},
        )
        kwargs = _build_kwargs(config)
        assert kwargs["extra_body"]["provider"] == {
            "order": ["Together"],
            "allow_fallbacks": False,
        }

    def test_no_proxy_kwargs_when_disabled(self, install_secrets) -> None:
        install_secrets({"OPENROUTER_API_KEY": "sk-or-test"})
        kwargs = _build_kwargs(ModelConfig(provider="openrouter", name="anthropic/claude-opus-4.7"))
        assert "api_base" not in kwargs
        assert "api_key" not in kwargs

    def test_out_of_scope_provider_is_not_routed(self, install_secrets) -> None:
        install_secrets(
            {
                "LLM_PROXY_BASE_URL": "https://gateway.example.com",
                "LLM_PROXY_PROVIDERS": "anthropic",
            }
        )
        kwargs = _build_kwargs(ModelConfig(provider="openrouter", name="anthropic/claude-opus-4.7"))
        assert "api_base" not in kwargs

    def test_native_provider_is_not_routed_by_default(self, install_secrets) -> None:
        """A native-protocol provider is not routed by the default scope."""
        install_secrets({"LLM_PROXY_BASE_URL": "https://gateway.example.com"})
        kwargs = _build_kwargs(ModelConfig(provider="anthropic", name="claude-opus-4.7"))
        assert "api_base" not in kwargs

    def test_no_api_key_kwarg_when_gateway_key_unset(self, install_secrets) -> None:
        """Without a gateway key, litellm keeps its own key resolution."""
        install_secrets({"LLM_PROXY_BASE_URL": "https://gateway.example.com"})
        kwargs = _build_kwargs(ModelConfig(provider="openrouter", name="anthropic/claude-opus-4.7"))
        assert kwargs["api_base"] == "https://gateway.example.com"
        assert "api_key" not in kwargs

    def test_gateway_header_wins_over_engine_default(self, install_secrets) -> None:
        """Explicit operator config beats the engine's own OpenRouter defaults."""
        install_secrets(
            {
                "LLM_PROXY_BASE_URL": "https://gateway.example.com",
                "LLM_PROXY_HEADERS": '{"X-Title": "gateway-owned"}',
            }
        )
        kwargs = _build_kwargs(ModelConfig(provider="openrouter", name="anthropic/claude-opus-4.7"))
        assert kwargs["extra_headers"]["X-Title"] == "gateway-owned"
        # The non-colliding OpenRouter defaults still ride along.
        assert "HTTP-Referer" in kwargs["extra_headers"]

    def test_malformed_config_raises_from_client_construction(self, install_secrets) -> None:
        """The fail-fast claim, asserted where operators actually hit it."""
        install_secrets(
            {
                "LLM_PROXY_BASE_URL": "https://gateway.example.com",
                "LLM_PROXY_HEADERS": "not-json",
            }
        )
        with pytest.raises(ProxyConfigError):
            LLMClient(ModelConfig(provider="openrouter", name="anthropic/claude-opus-4.7"))


class TestNonProxyPathUnchanged:
    """The constructor refactor must not disturb the direct-provider path.

    Nothing in the suite covered ``_configure_openrouter_base_url`` or
    ``_configure_nova_base_url`` before, so a regression here would have been
    invisible.
    """

    def test_openrouter_base_url_override_still_applies(self, install_secrets) -> None:
        _clear_env("OPENROUTER_API_BASE")
        install_secrets({"OPENROUTER_BASE_URL": "https://or-mirror.example.com"})
        LLMClient(ModelConfig(provider="openrouter", name="anthropic/claude-opus-4.7"))
        assert os.environ.get("OPENROUTER_API_BASE") == "https://or-mirror.example.com"

    def test_gateway_suppresses_the_openrouter_override(self, install_secrets) -> None:
        """With the gateway on, the explicit api_base kwarg is the only base URL."""
        _clear_env("OPENROUTER_API_BASE")
        install_secrets(
            {
                "LLM_PROXY_BASE_URL": "https://gateway.example.com",
                "OPENROUTER_BASE_URL": "https://or-mirror.example.com",
            }
        )
        LLMClient(ModelConfig(provider="openrouter", name="anthropic/claude-opus-4.7"))
        assert os.environ.get("OPENROUTER_API_BASE") is None

    def test_nova_base_url_still_applies(self, install_secrets) -> None:
        _clear_env("NOVA_API_BASE")
        install_secrets({"NOVA_API_KEY": "nova-test"})
        LLMClient(ModelConfig(provider="nova", name="busan-v1"))
        assert os.environ.get("NOVA_API_BASE") == "https://api.nova.amazon.com/v1"

    def test_nova_keeps_its_own_transport_even_with_gateway_configured(
        self, install_secrets
    ) -> None:
        """``nova`` is unroutable, so the gateway must not claim it."""
        install_secrets(
            {
                "LLM_PROXY_BASE_URL": "https://gateway.example.com",
                "NOVA_API_KEY": "nova-test",
            }
        )
        client = LLMClient(ModelConfig(provider="nova", name="busan-v1"))
        assert client._proxy is None

        captured: dict[str, Any] = {}

        def _fake_completion(**kwargs: Any) -> str:
            captured.update(kwargs)
            return "ok"

        original = client_module.completion
        client_module.completion = _fake_completion  # type: ignore[assignment]
        try:
            client._call_with_key_rotation({"model": "nova/busan-v1", "messages": []})
        finally:
            client_module.completion = original  # type: ignore[assignment]

        assert captured["api_base"] == "https://api.nova.amazon.com/v1"
        # The rewrite that makes the bare Nova name routable is still in place.
        assert captured["model"] == "openai/busan-v1"
        assert captured["custom_llm_provider"] == "openai"


class TestGatewayQuotaRejection:
    """A gateway rejection must not be reported as a provider key-chain problem."""

    def _raise_quota(self, client: LLMClient) -> BaseException:
        def _fake_completion(**_: Any) -> str:
            raise RuntimeError('litellm.AuthenticationError {"code":403} budget exceeded')

        original = client_module.completion
        client_module.completion = _fake_completion  # type: ignore[assignment]
        try:
            with pytest.raises(RuntimeError) as excinfo:
                client._call_with_key_rotation({"model": "x", "messages": []})
        finally:
            client_module.completion = original  # type: ignore[assignment]
        return excinfo.value

    def test_gateway_rejection_names_the_gateway(self, install_secrets) -> None:
        install_secrets(
            {
                "LLM_PROXY_BASE_URL": "https://gateway.example.com",
                "LLM_PROXY_API_KEY": "sk-gateway",
                # A provider chain exists; rotating it would be useless here
                # because the gateway key is pinned as an explicit kwarg.
                "OPENROUTER_API_KEYS": "k1,k2,k3",
            }
        )
        client = LLMClient(ModelConfig(provider="openrouter", name="anthropic/claude-opus-4.7"))
        message = str(self._raise_quota(client))
        assert message.startswith("LLM gateway at https://gateway.example.com rejected the request")
        assert "Provider key rotation does not apply" in message
        assert "exhausted" not in message.lower()

    def test_direct_path_still_reports_key_exhaustion(self, install_secrets) -> None:
        install_secrets({"OPENROUTER_API_KEY": "sk-or-only"})
        client = LLMClient(ModelConfig(provider="openrouter", name="anthropic/claude-opus-4.7"))
        assert "All API keys exhausted" in str(self._raise_quota(client))

    def test_rotation_survives_a_gateway_without_its_own_key(self, install_secrets) -> None:
        """A gateway authenticating by network position leaves rotation working.

        Without ``LLM_PROXY_API_KEY`` nothing pins ``api_key``, so litellm reads
        the provider env var that ``_rotate_key`` republishes. Suppressing
        rotation here would abort a trial with unused keys still in the chain.
        """
        _clear_env("OPENROUTER_API_KEY")
        install_secrets(
            {
                "LLM_PROXY_BASE_URL": "https://gateway.example.com",
                "OPENROUTER_API_KEYS": "k1,k2,k3",
            }
        )
        client = LLMClient(ModelConfig(provider="openrouter", name="anthropic/claude-opus-4.7"))
        assert client._proxy is not None and client._proxy.api_key is None

        message = str(self._raise_quota(client))
        # Rotation ran through the whole chain instead of blaming the gateway.
        assert client._current_key_index == 2
        assert os.environ.get("OPENROUTER_API_KEY") == "k3"
        assert "All API keys exhausted" in message
