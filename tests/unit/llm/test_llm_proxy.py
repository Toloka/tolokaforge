"""Tests for the configurable OpenAI-compatible gateway transport.

Covers the two halves of the feature:

* :mod:`tolokaforge.core.llm.proxy` — resolving and validating the env
  contract.
* :class:`~tolokaforge.core.llm.client.LLMClient` — applying it, on the branch
  where the gateway catalog is unreadable so the model string is left alone. The
  route-resolving branches live in test_gateway_routing_applied.py. What preset
  resolution and pricing key off either way is ``ModelConfig``, never the wire
  name.

It also covers ``ModelConfig.session``, whose header rides the same
``extra_headers`` on every route, gateway or not, and is refused when another
header source sets the same name.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import litellm
import pytest
import tenacity.nap

from tolokaforge.core.llm import client as client_module
from tolokaforge.core.llm.client import LLMClient
from tolokaforge.core.llm.fallback_client import FallbackLLMClient
from tolokaforge.core.llm.providers import get_provider_binding, litellm_model_id
from tolokaforge.core.llm.proxy import (
    ProxyConfig,
    ProxyConfigError,
    resolve_proxy_config,
)
from tolokaforge.core.llm.session_header import (
    SessionHeaderConflictError,
    session_header_conflicts,
)
from tolokaforge.core.models import Message, MessageRole, ModelConfig
from tolokaforge.core.run_display_events import _NULL_EVENTS, LLMCallObservation
from tolokaforge.secrets import DictProvider, SecretManager
from tolokaforge.secrets import manager as secrets_manager

pytestmark = pytest.mark.unit


_UNROUTABLE_PROVIDERS = frozenset(
    name for name in ("mock", "nova") if get_provider_binding(name).unroutable
)


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
            "LLM_PROXY_PREFERRED_ROUTE",
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
        assert frozenset({"mock", "nova"}) == _UNROUTABLE_PROVIDERS
        proxy = ProxyConfig(
            base_url="https://gateway.example.com",
            providers=frozenset(_UNROUTABLE_PROVIDERS),
        )
        for provider in _UNROUTABLE_PROVIDERS:
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


class TestHeaderSecretRefs:
    """The wire-level half of ``${secret:NAME}``; the syntax itself is pinned in
    tests/unit/secrets/test_expand.py."""

    def test_a_reference_is_resolved_into_the_header(self, install_secrets: Any) -> None:
        install_secrets(
            {
                "LLM_PROXY_BASE_URL": "https://gateway.example.com",
                "LLM_PROXY_HEADERS": '{"X-Team-Id": "research", "X-Order-Id": "${secret:ORDER_ID}"}',
                "ORDER_ID": "9000123",
            }
        )
        proxy = resolve_proxy_config()
        assert proxy is not None
        assert proxy.headers == {"X-Team-Id": "research", "X-Order-Id": "9000123"}

    def test_no_reference_reaches_the_wire(self, install_secrets: Any) -> None:
        """A literal ``${secret:...}`` sent as a header value is the failure this
        guards: no gateway errors on it usefully."""
        install_secrets(
            {
                "LLM_PROXY_BASE_URL": "https://gateway.example.com",
                "LLM_PROXY_HEADERS": '{"X-Order-Id": "${secret:ORDER_ID}"}',
                "ORDER_ID": "9000123",
            }
        )
        proxy = resolve_proxy_config()
        assert proxy is not None
        assert "${secret:" not in "".join(proxy.request_headers().values())

    def test_an_unresolved_reference_surfaces_as_a_gateway_config_error(
        self, install_secrets: Any
    ) -> None:
        """Callers catch one exception type for "the gateway is misconfigured", so the
        secrets-layer error is translated rather than leaking through."""
        install_secrets(
            {
                "LLM_PROXY_BASE_URL": "https://gateway.example.com",
                "LLM_PROXY_HEADERS": '{"X-Order-Id": "${secret:ORDER_ID}"}',
            }
        )
        with pytest.raises(ProxyConfigError) as excinfo:
            resolve_proxy_config()
        message = str(excinfo.value)
        assert "X-Order-Id" in message
        assert "ORDER_ID" in message

    def test_a_non_string_value_takes_no_reference(self, install_secrets: Any) -> None:
        """JSON scalars keep their existing stringify path."""
        install_secrets(
            {
                "LLM_PROXY_BASE_URL": "https://gateway.example.com",
                "LLM_PROXY_HEADERS": '{"X-Cost-Center": 42, "X-On": true}',
            }
        )
        proxy = resolve_proxy_config()
        assert proxy is not None
        assert proxy.headers == {"X-Cost-Center": "42", "X-On": "True"}


class TestClientAppliesProxy:
    """``LLMClient`` applying the gateway, with the catalog unreadable.

    The autouse fixture in conftest.py stubs the catalog fetch to ``None``, so these
    pin the degraded branch rather than a general invariant.
    """

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

    def test_an_unreadable_catalog_leaves_the_model_string_alone(self, install_secrets) -> None:
        """Pins the unreadable-catalog branch, not a general invariant.

        A readable catalog naming the model DOES rewrite this to the gateway's route
        name; that path is covered in test_gateway_routing_applied.py. What presets and
        pricing actually key off is ``ModelConfig``, asserted below.
        """
        install_secrets({"LLM_PROXY_BASE_URL": "https://gateway.example.com"})
        config = ModelConfig(provider="openrouter", name="anthropic/claude-opus-4.7")
        kwargs = _build_kwargs(config)
        assert kwargs["model"] == "openrouter/anthropic/claude-opus-4.7"
        assert config.provider == "openrouter"
        assert config.name == "anthropic/claude-opus-4.7"

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


class TestTrustWildcardsFlag:
    def test_default_is_off(self, install_secrets) -> None:
        install_secrets({"LLM_PROXY_BASE_URL": "https://gateway.example.com"})
        assert resolve_proxy_config().trust_namespace_wildcards is False

    @pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes"])
    def test_truthy_values(self, install_secrets, value: str) -> None:
        install_secrets(
            {
                "LLM_PROXY_BASE_URL": "https://gateway.example.com",
                "LLM_PROXY_TRUST_NAMESPACE_WILDCARDS": value,
            }
        )
        assert resolve_proxy_config().trust_namespace_wildcards is True

    def test_garbage_is_refused(self, install_secrets) -> None:
        install_secrets(
            {
                "LLM_PROXY_BASE_URL": "https://gateway.example.com",
                "LLM_PROXY_TRUST_NAMESPACE_WILDCARDS": "openrouter",
            }
        )
        with pytest.raises(ProxyConfigError):
            resolve_proxy_config()

    def test_set_without_a_base_url_is_an_orphan(self, install_secrets) -> None:
        """A companion without the on-switch means a typo, never silent direct."""
        install_secrets({"LLM_PROXY_TRUST_NAMESPACE_WILDCARDS": "true"})
        with pytest.raises(ProxyConfigError):
            resolve_proxy_config()


SESSION_HEADER = "x-session-id"
CANARY = "self-hosted/tolokaforge-canary"
_ROUTING_SECRETS = {
    "OPENAI_API_KEY": "sk-openai",
    "OPENROUTER_API_KEY": "sk-or",
    "ANTHROPIC_API_KEY": "sk-ant",
}
_GATEWAY_ON = {"LLM_PROXY_BASE_URL": "https://gateway.example.com"}


class _RecordingCompletion:
    """Stands in for litellm ``completion``: records each call's kwargs, raises
    a retryable error for the first ``failures`` calls, then answers."""

    def __init__(self, failures: int = 0) -> None:
        self.calls: list[dict[str, Any]] = []
        self._failures = failures

    def __call__(self, **kwargs: Any) -> litellm.ModelResponse:
        self.calls.append(kwargs)
        if len(self.calls) <= self._failures:
            raise RuntimeError("upstream 503")
        return litellm.ModelResponse(
            model=kwargs["model"],
            choices=[
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "ok"},
                }
            ],
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )

    def sent(self, header: str = SESSION_HEADER) -> list[str | None]:
        return [(call.get("extra_headers") or {}).get(header) for call in self.calls]


def _observation(session_id: str | None) -> LLMCallObservation:
    return LLMCallObservation(
        events=_NULL_EVENTS, trial_id="task:0", role="agent", session_id=session_id
    )


def _generate(client: LLMClient, observation: LLMCallObservation | None) -> None:
    client.generate(
        system="s", messages=[Message(role=MessageRole.USER, content="hi")], observation=observation
    )


#: `(provider, name, gateway)`: every provider path that builds ``extra_headers``.
#: ``gateway`` is ``None`` (off), ``"unreadable"`` (catalog fetch fails) or
#: ``"resolved"`` (the catalog serves the model).
SESSION_ROUTES = [
    pytest.param("openai", CANARY, None, id="openai-direct"),
    pytest.param("openrouter", "anthropic/claude-opus-4.7", None, id="openrouter-direct"),
    pytest.param("anthropic", "claude-sonnet-4-6", None, id="plain-provider"),
    pytest.param("openai", CANARY, "unreadable", id="openai-gateway-unreadable"),
    pytest.param(
        "openrouter", "anthropic/claude-opus-4.7", "unreadable", id="openrouter-gateway-unreadable"
    ),
    pytest.param("openai", CANARY, "resolved", id="openai-gateway-route"),
    pytest.param(
        "openrouter", "anthropic/claude-opus-4.7", "resolved", id="openrouter-gateway-route"
    ),
]


class TestSessionHeader:
    """``ModelConfig.session`` puts one conversation value on every request of a call."""

    @pytest.fixture
    def completion(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[_RecordingCompletion]:
        recorder = _RecordingCompletion()
        # client.py imports the name, so only its binding is live.
        monkeypatch.setattr(client_module, "completion", recorder)
        yield recorder

    def _client(
        self,
        install_secrets: Any,
        monkeypatch: pytest.MonkeyPatch,
        provider: str,
        name: str,
        gateway: str | None,
        *,
        session: dict[str, str] | None,
    ) -> LLMClient:
        secrets = dict(_ROUTING_SECRETS)
        if gateway is not None:
            secrets.update(_GATEWAY_ON)
        if gateway == "resolved":
            served = frozenset({litellm_model_id(provider, name)})
            monkeypatch.setattr(client_module, "fetch_gateway_catalog", lambda *_a, **_k: served)
        install_secrets(secrets)
        client = LLMClient(ModelConfig(provider=provider, name=name, session=session))
        assert (client._proxy is not None) == (gateway is not None)
        assert (client._gateway_route is not None) == (gateway == "resolved")
        client._retry_sleep = lambda _s: None
        return client

    @pytest.mark.parametrize("provider, name, gateway", SESSION_ROUTES)
    def test_the_observation_id_reaches_every_route(
        self, install_secrets, monkeypatch, completion, provider, name, gateway
    ) -> None:
        client = self._client(
            install_secrets,
            monkeypatch,
            provider,
            name,
            gateway,
            session={"header": SESSION_HEADER},
        )
        _generate(client, _observation("trace-agent"))
        _generate(client, _observation("trace-agent"))
        assert completion.sent() == ["trace-agent", "trace-agent"]

    @pytest.mark.parametrize("provider, name, gateway", SESSION_ROUTES)
    def test_no_session_block_sends_no_header_even_with_an_id(
        self, install_secrets, monkeypatch, completion, provider, name, gateway
    ) -> None:
        client = self._client(install_secrets, monkeypatch, provider, name, gateway, session=None)
        _generate(client, _observation("trace-agent"))
        assert completion.sent() == [None]
        assert "trace-agent" not in str(completion.calls[0])

    @pytest.mark.parametrize(
        "observation", [None, _observation(None)], ids=["no-observation", "no-session-id"]
    )
    def test_a_call_without_identity_is_its_own_conversation(
        self, install_secrets, monkeypatch, completion, observation
    ) -> None:
        client = self._client(
            install_secrets,
            monkeypatch,
            "openai",
            CANARY,
            "unreadable",
            session={"header": SESSION_HEADER},
        )
        _generate(client, observation)
        _generate(client, observation)
        first, second = completion.sent()
        assert first and second and first != second
        assert str(uuid.UUID(first)) == first

    def test_one_id_across_the_outer_retry_of_a_call_without_identity(
        self, install_secrets, monkeypatch
    ) -> None:
        """The fault is raised where tenacity sees it; a 5xx on the wire would be
        re-sent inside the OpenAI SDK and never reach the outer retry."""
        recorder = _RecordingCompletion(failures=1)
        monkeypatch.setattr(client_module, "completion", recorder)
        client = self._client(
            install_secrets,
            monkeypatch,
            "openai",
            CANARY,
            "unreadable",
            session={"header": SESSION_HEADER},
        )
        sleeps: list[float] = []
        client._retry_sleep = sleeps.append

        _generate(client, None)

        assert len(sleeps) == 1
        first, second = recorder.sent()
        assert first is not None and first == second

    def test_the_session_header_rides_beside_the_gateway_request_id(
        self, install_secrets, monkeypatch, completion
    ) -> None:
        install_secrets(
            {
                **_ROUTING_SECRETS,
                **_GATEWAY_ON,
                "LLM_PROXY_REQUEST_ID_HEADER": "X-Request-Id",
            }
        )
        client = LLMClient(
            ModelConfig(provider="openai", name=CANARY, session={"header": SESSION_HEADER})
        )
        _generate(client, _observation("trace-agent"))
        _generate(client, _observation("trace-agent"))
        assert completion.sent() == ["trace-agent", "trace-agent"]
        first_id, second_id = completion.sent("X-Request-Id")
        assert first_id and second_id and first_id != second_id

    def test_the_session_header_stays_off_litellms_global_headers(
        self, install_secrets, monkeypatch, completion
    ) -> None:
        client = self._client(
            install_secrets,
            monkeypatch,
            "openrouter",
            "anthropic/claude-opus-4.7",
            None,
            session={"header": SESSION_HEADER},
        )
        _generate(client, _observation("trace-agent"))
        assert SESSION_HEADER not in (litellm.openai_headers or {})

    @pytest.mark.parametrize(
        "fallback_session, expected", [({"header": SESSION_HEADER}, "trace-agent"), (None, None)]
    )
    def test_each_link_of_a_fallback_chain_sends_its_own_declaration(
        self, install_secrets, monkeypatch, fallback_session, expected
    ) -> None:
        """One observation across a failover: a fallback that declares ``session``
        sends the conversation's value, one that declares none sends no header."""
        install_secrets(_ROUTING_SECRETS)
        failing = _RecordingCompletion(failures=10**6)
        answering = _RecordingCompletion()

        def by_model(**kwargs: Any) -> litellm.ModelResponse:
            recorder = failing if kwargs["model"] == "openai/primary" else answering
            return recorder(**kwargs)

        monkeypatch.setattr(client_module, "completion", by_model)
        # Both links bind their outer-retry sleep at construction; the fallback's
        # is built inside the chain on failover.
        monkeypatch.setattr(tenacity.nap, "sleep", lambda _s: None)
        chain = FallbackLLMClient(
            primary=ModelConfig(
                provider="openai", name="primary", session={"header": SESSION_HEADER}
            ),
            fallbacks=[ModelConfig(provider="openai", name="hosted", session=fallback_session)],
        )

        _generate(chain, _observation("trace-agent"))

        assert chain.cursor == 1
        assert failing.sent() == ["trace-agent"] * 5
        assert answering.sent() == [expected]


class TestSessionHeaderConflicts:
    """A session header another header source also sets is refused at construction."""

    @pytest.mark.parametrize(
        "provider, session_header, gateway_env, source",
        [
            pytest.param(
                "openai",
                "X-Team-Id",
                {**_GATEWAY_ON, "LLM_PROXY_HEADERS": '{"x-team-id": "research"}'},
                "LLM_PROXY_HEADERS",
                id="static-gateway-header",
            ),
            pytest.param(
                "openrouter",
                "x-request-id",
                {**_GATEWAY_ON, "LLM_PROXY_REQUEST_ID_HEADER": "X-Request-Id"},
                "LLM_PROXY_REQUEST_ID_HEADER",
                id="gateway-request-id",
            ),
            pytest.param(
                "openrouter",
                "x-title",
                {},
                "the engine's OpenRouter default headers",
                id="openrouter-default",
            ),
        ],
    )
    def test_each_source_is_refused_case_insensitively(
        self, install_secrets, provider, session_header, gateway_env, source
    ) -> None:
        install_secrets({**_ROUTING_SECRETS, **gateway_env})
        with pytest.raises(SessionHeaderConflictError) as refused:
            LLMClient(
                ModelConfig(provider=provider, name=CANARY, session={"header": session_header})
            )
        assert (refused.value.path, refused.value.header, refused.value.source) == (
            "session.header",
            session_header,
            source,
        )
        assert source in str(refused.value)

    def test_gateway_headers_do_not_bind_a_provider_the_gateway_skips(
        self, install_secrets
    ) -> None:
        install_secrets(
            {
                **_ROUTING_SECRETS,
                **_GATEWAY_ON,
                "LLM_PROXY_HEADERS": '{"x-team-id": "research"}',
                "LLM_PROXY_REQUEST_ID_HEADER": SESSION_HEADER,
            }
        )
        client = LLMClient(
            ModelConfig(
                provider="anthropic", name="claude-sonnet-4-6", session={"header": SESSION_HEADER}
            )
        )
        assert client._proxy is None

    def test_openrouter_defaults_do_not_bind_another_provider(self, install_secrets) -> None:
        install_secrets(_ROUTING_SECRETS)
        LLMClient(ModelConfig(provider="openai", name=CANARY, session={"header": "X-Title"}))

    def test_the_sweep_names_a_conflict_that_sits_only_on_a_fallback(self, install_secrets) -> None:
        install_secrets(
            {
                **_ROUTING_SECRETS,
                **_GATEWAY_ON,
                "LLM_PROXY_REQUEST_ID_HEADER": SESSION_HEADER,
            }
        )
        models = {
            "agent": ModelConfig(
                provider="anthropic",
                name="claude-sonnet-4-6",
                session={"header": SESSION_HEADER},
                fallbacks=[
                    ModelConfig(provider="openai", name=CANARY, session={"header": SESSION_HEADER})
                ],
            ),
            "user": ModelConfig(provider="openai", name=CANARY),
        }
        conflicts = session_header_conflicts(models, resolve_proxy_config())
        assert [(path, err.path, err.source) for path, err in conflicts] == [
            (
                "models.agent.fallbacks[0]",
                "models.agent.fallbacks[0].session.header",
                "LLM_PROXY_REQUEST_ID_HEADER",
            )
        ]
