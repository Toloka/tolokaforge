"""Live integration test for the LLM gateway transport.

Answers one question that unit tests structurally cannot: *does a real gateway
accept what this engine sends it?* Everything in
``tests/unit/llm/test_llm_proxy.py`` stops at the kwargs dict, and the two
failure modes that matter most in production live past that boundary — the
gateway resolving our model name to a route we did not intend, and a gateway
rejecting a request shape litellm produced.

Spend isolation
---------------

This test bills to its **own** credential, ``LLM_PROXY_INT_TEST_API_KEY``, not to
whatever ``LLM_PROXY_API_KEY`` the ambient environment holds. The fixture
installs a SecretManager that overrides that name for the test's duration, so a
CI budget for integration tests stays separate from a deployment's production
gateway budget, and a local ``.env`` cannot accidentally charge the wrong key.

Three calls reach the network (the pinned-upstream check is opt-in), each capped at a few dozen output tokens.

Environment contract
--------------------

``LLM_PROXY_INT_TEST_API_KEY`` (required)
    Gateway credential dedicated to integration testing. Absent → every test
    here skips, which is the state of any checkout without the secret.

``LLM_PROXY_INT_TEST_MODEL`` (required once the key is set)
    Model name **as the gateway routes it**, e.g. a LiteLLM proxy's
    ``openrouter/<vendor>/<model>``. Required rather than defaulted because
    every gateway names its routes differently, and a wrong guess would test
    the gateway's fallback behaviour instead of this transport. Discover the
    available names with ``GET {base_url}/models``.

    Not a credential — in CI this belongs in the workflow's ``env:``, not in a
    secret. Missing it while the key *is* present **fails** rather than skips:
    otherwise a pipeline holding the secret would report green while testing
    nothing.

``LLM_PROXY_INT_TEST_BASE_URL`` (optional)
    Gateway base URL. Falls back to ``LLM_PROXY_BASE_URL``. Must include
    whatever path prefix the gateway serves its OpenAI-compatible route under
    (commonly ``/v1``) — litellm appends ``/chat/completions`` to it.

``LLM_PROXY_INT_TEST_PROVIDER`` (optional, default ``openai``)
    Provider whose litellm transport carries the request. ``openai`` is the
    default because litellm strips exactly one provider prefix, so
    ``provider=openai`` + a gateway route name leaves that name intact on the
    wire. See ``docs/LLM_LAYER.md`` § proxy.

``LLM_PROXY_HEADERS`` / ``LLM_PROXY_REQUEST_ID_HEADER`` (optional)
    Carried over from the ambient environment, so gateways that mandate
    attribution headers are exercised as configured.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tolokaforge.core.llm.client import LLMClient
from tolokaforge.core.llm.proxy import (
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_HEADERS,
    ENV_PREFERRED_ROUTE,
    ENV_PROVIDERS,
    ENV_REQUEST_ID_HEADER,
    ENV_TRUST_WILDCARDS,
)
from tolokaforge.core.llm.reasoning import ReasoningConfig
from tolokaforge.core.models import Message, MessageRole, ModelConfig
from tolokaforge.secrets import DictProvider, SecretManager, get_default
from tolokaforge.secrets import manager as secrets_manager

pytestmark = [pytest.mark.integration, pytest.mark.requires_api]

ENV_TEST_API_KEY = "LLM_PROXY_INT_TEST_API_KEY"
ENV_TEST_MODEL = "LLM_PROXY_INT_TEST_MODEL"
ENV_TEST_BASE_URL = "LLM_PROXY_INT_TEST_BASE_URL"
ENV_TEST_PROVIDER = "LLM_PROXY_INT_TEST_PROVIDER"
# Optional pinned-upstream check (skipped when unset): an OpenRouter-namespace
# slug plus the exact OpenRouter provider name expected to serve it. Needs a
# gateway that forwards openrouter/... routes AND an OPENROUTER_API_KEY for the
# retroactive /generation lookup.
ENV_TEST_PINNED_MODEL = "LLM_PROXY_INT_TEST_PINNED_MODEL"
ENV_TEST_PINNED_PROVIDER = "LLM_PROXY_INT_TEST_PINNED_PROVIDER"

_WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Look up the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name"},
                "unit": {"type": "string", "enum": ["c", "f"]},
            },
            "required": ["city", "unit"],
        },
    },
}


def _secret(name: str) -> str:
    """Read a configuration value through ``SecretManager``.

    Not ``os.environ``: the repo's secrets contract routes credential reads
    through ``SecretManager`` (see ``AGENTS.md``), and it also means a value
    living only in ``.env`` resolves deterministically rather than depending on
    a dependency's import-time ``load_dotenv`` side effect.
    """
    return (get_default().get_secret(name) or "").strip()


@pytest.fixture(scope="module")
def gateway_key() -> str:
    """The dedicated integration-test credential, or skip."""
    api_key = _secret(ENV_TEST_API_KEY)
    if not api_key:
        pytest.skip(f"{ENV_TEST_API_KEY} not set — skipping live gateway test.")
    return api_key


@pytest.fixture(scope="module")
def gateway_client(gateway_key: str) -> Iterator[LLMClient]:
    """An ``LLMClient`` pointed at the gateway, billed to the test credential.

    Skips unless the dedicated key and a gateway route name are both present, so
    a checkout without the CI secret is quiet rather than failing.
    """
    # Past this point the credential exists, which is an explicit signal that
    # this environment intends to run the test. A missing companion is therefore
    # a misconfiguration, not an opt-out, and must fail rather than skip — a
    # pipeline that holds the secret but lacks the route name would otherwise go
    # green while testing nothing.
    model = _secret(ENV_TEST_MODEL)
    if not model:
        pytest.fail(
            f"{ENV_TEST_API_KEY} is set but {ENV_TEST_MODEL} is not. The gateway's own "
            f"route name is required — list candidates with GET {{base_url}}/models. "
            f"Unset {ENV_TEST_API_KEY} to disable this test deliberately."
        )

    base_url = _secret(ENV_TEST_BASE_URL) or _secret(ENV_BASE_URL)
    if not base_url:
        pytest.fail(
            f"{ENV_TEST_API_KEY} is set but neither {ENV_TEST_BASE_URL} nor "
            f"{ENV_BASE_URL} is. Set a gateway base URL, or unset "
            f"{ENV_TEST_API_KEY} to disable this test deliberately."
        )

    provider = _secret(ENV_TEST_PROVIDER) or "openai"

    # Override the gateway credential so this run bills to the test budget even
    # when a production LLM_PROXY_API_KEY is present in .env or the environment.
    secrets: dict[str, str] = {ENV_BASE_URL: base_url, ENV_API_KEY: gateway_key}
    for passthrough in (
        ENV_HEADERS,
        ENV_REQUEST_ID_HEADER,
        ENV_PROVIDERS,
        ENV_PREFERRED_ROUTE,
        ENV_TRUST_WILDCARDS,
    ):
        value = _secret(passthrough)
        if value:
            secrets[passthrough] = value

    original = secrets_manager._default_manager
    secrets_manager._default_manager = SecretManager([DictProvider(secrets)])
    try:
        client = LLMClient(
            ModelConfig(
                provider=provider,
                name=model,
                temperature=0.0,
                # Enough headroom for a tool call from a model that emits
                # preamble or reasoning text first. At 64 a Cohere model
                # truncated before emitting the call, which surfaces as an empty
                # response and reads like a gateway fault rather than a cap.
                max_tokens=256,
                reasoning=ReasoningConfig(mode="off"),
            )
        )
        assert client._proxy is not None, (
            "gateway did not claim this provider; check LLM_PROXY_INT_TEST_PROVIDER "
            "against DEFAULT_ROUTED_PROVIDERS"
        )
        yield client
    finally:
        secrets_manager._default_manager = original


def test_request_is_addressed_to_the_gateway(gateway_client: LLMClient, gateway_key: str) -> None:
    """The transport is applied and billed to the test key. No network spend."""
    kwargs = gateway_client._build_kwargs(
        system="Be terse.",
        messages=[Message(role=MessageRole.USER, content="ping")],
        tools=None,
        tool_choice=None,
        temperature=None,
        seed=None,
        reasoning=None,
        top_p=None,
        max_tokens=None,
    )

    assert kwargs["api_base"] == gateway_client._proxy.base_url
    # Spend isolation: the call carries the test credential, not whatever
    # production gateway key the ambient environment holds.
    assert kwargs["api_key"] == gateway_key

    # The model string must survive intact — re-prefixing it would silently
    # miss the pricing table (see docs/LLM_LAYER.md § proxy).
    expected_model = (
        gateway_client.config.name
        if gateway_client.config.name.startswith(f"{gateway_client.config.provider}/")
        else f"{gateway_client.config.provider}/{gateway_client.config.name}"
    )
    assert kwargs["model"] == expected_model, (
        f"model string was rewritten: {kwargs['model']!r} != {expected_model!r}; "
        f"a re-prefix breaks normalize_model_name and degrades cost_source"
    )

    for header in gateway_client._proxy.headers:
        assert header in kwargs["extra_headers"]
    if gateway_client._proxy.request_id_header:
        assert kwargs["extra_headers"][gateway_client._proxy.request_id_header]


def test_gateway_serves_a_completion(gateway_client: LLMClient) -> None:
    """The gateway accepts what litellm sends and returns usable text."""
    result = gateway_client.generate(
        system="Answer with a single word.",
        messages=[Message(role=MessageRole.USER, content="Say OK")],
    )

    assert result.text.strip(), "gateway returned empty text"
    assert result.usage.prompt_tokens > 0, "no prompt tokens reported"
    assert result.usage.completion_tokens > 0, "no completion tokens reported"

    # cost_source is recorded either way. Which value depends on whether the
    # gateway reports cost in the response; a gateway that does not leaves
    # "unknown" unless a pricing.json entry covers the formatted model string.
    sources = [call.cost_source for call in result.usage.calls]
    assert sources, "no per-call usage recorded"


def test_gateway_serves_a_tool_call(gateway_client: LLMClient) -> None:
    """Tool calling round-trips — the arena's actual requirement of a gateway."""
    result = gateway_client.generate(
        system="Use the provided tool to answer. Fill every required field.",
        messages=[Message(role=MessageRole.USER, content="Weather in Budapest, in celsius?")],
        tools=[_WEATHER_TOOL],
    )

    assert result.tool_calls, f"no tool call; model replied {result.text!r}"
    call = result.tool_calls[0]
    assert call.name == "get_weather", f"unexpected tool {call.name!r}"

    arguments = call.arguments
    assert isinstance(arguments, dict), f"arguments not parsed into a dict: {arguments!r}"
    # Both fields are `required` in the schema, so a gateway that mangles the
    # tool payload shows up as a missing key rather than a silent pass.
    assert "city" in arguments
    assert "unit" in arguments


def test_pinned_provider_reaches_the_upstream(gateway_key: str) -> None:
    """A provider-pinned call through the gateway is served by THE pinned upstream.

    This is the check the request shape cannot give: litellm's proxy may or may
    not forward ``extra_body.provider`` to OpenRouter depending on its version,
    and an upstream silently ignoring the field is invisible in the response
    body. OpenRouter's ``/generation`` endpoint reports which provider actually
    served the call, keyed by the generation id the engine already extracts
    (``usage.openrouter_generation_id``).
    """
    import json
    import urllib.request

    pinned_model = _secret(ENV_TEST_PINNED_MODEL)
    pinned_provider = _secret(ENV_TEST_PINNED_PROVIDER)
    if not pinned_model or not pinned_provider:
        pytest.skip(
            f"{ENV_TEST_PINNED_MODEL} / {ENV_TEST_PINNED_PROVIDER} not set - "
            "skipping the pinned-upstream check."
        )
    openrouter_key = _secret("OPENROUTER_API_KEY")
    if not openrouter_key:
        pytest.fail(
            f"{ENV_TEST_PINNED_MODEL} is set but OPENROUTER_API_KEY is not - the "
            "/generation lookup needs it. Unset the pinned-model envs to disable "
            "this test deliberately."
        )

    base_url = _secret(ENV_TEST_BASE_URL) or _secret(ENV_BASE_URL)
    if not base_url:
        pytest.fail(
            f"{ENV_TEST_PINNED_MODEL} is set but no gateway base URL is; set "
            f"{ENV_TEST_BASE_URL} or {ENV_BASE_URL}."
        )

    secrets: dict[str, str] = {ENV_BASE_URL: base_url, ENV_API_KEY: gateway_key}
    for passthrough in (
        ENV_HEADERS,
        ENV_REQUEST_ID_HEADER,
        ENV_PROVIDERS,
        ENV_PREFERRED_ROUTE,
        ENV_TRUST_WILDCARDS,
    ):
        value = _secret(passthrough)
        if value:
            secrets[passthrough] = value

    original = secrets_manager._default_manager
    secrets_manager._default_manager = SecretManager([DictProvider(secrets)])
    try:
        client = LLMClient(
            ModelConfig(
                provider="openrouter",
                name=pinned_model,
                temperature=0.0,
                max_tokens=64,
                reasoning=ReasoningConfig(mode="off"),
                openrouter={"provider_order": [pinned_provider], "allow_fallbacks": False},
            )
        )
        assert client._proxy is not None, "gateway did not claim the openrouter provider"
        result = client.generate(
            system="Answer with a single word.",
            messages=[Message(role=MessageRole.USER, content="Say OK")],
        )
    finally:
        secrets_manager._default_manager = original

    generation_ids = [
        call.openrouter_generation_id
        for call in result.usage.calls
        if call.openrouter_generation_id
    ]
    assert generation_ids, (
        "no OpenRouter generation id on the response: either the gateway does not "
        "forward upstream response headers (attribution is then impossible on this "
        "route) or the call never reached OpenRouter"
    )

    request = urllib.request.Request(
        f"https://openrouter.ai/api/v1/generation?id={generation_ids[-1]}",
        headers={"Authorization": f"Bearer {openrouter_key}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    served_by = (payload.get("data") or {}).get("provider_name")
    assert served_by == pinned_provider, (
        f"the pin did not hold through the gateway: pinned {pinned_provider!r}, "
        f"served by {served_by!r} - the proxy dropped extra_body.provider"
    )
