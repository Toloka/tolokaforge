"""Contract: an OpenAI reasoning model on the default config sends the same request
directly and through a gateway route.

``openai_gpt5``, ``openai_gpt6`` and ``openai_o_series`` declare
``supports_sampling_params: false``: no OpenRouter endpoint applies a ``temperature``
/ ``top_p`` for these models except gpt-5-image*. On a resolved gateway route the
request goes out through litellm's ``openai`` transport, which refuses
``temperature`` in-process for the gpt-5 and o-series names; directly, the
``openrouter`` transport forwards it. gpt-6 is declared on OpenRouter's support list
alone: litellm's transport accepts ``temperature`` for it on either path. With the
declaration neither path sends a sampling key, so both succeed and agree.

Each case drives the real :meth:`LLMClient.generate` on the default ``ModelConfig``
(``temperature`` left at ``0.0``) against a loopback server. "Direct" points
litellm's OpenRouter transport at it through ``OPENROUTER_API_BASE`` with no gateway
configured; "routed" configures the server as the gateway, whose catalog serves
``openrouter/<name>``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import litellm
import pytest

from tests.utils.recording_gateway import RecordingGateway
from tests.utils.secret_state import secret_manager_installed
from tolokaforge.core.llm import gateway_route
from tolokaforge.core.llm.client import LLMClient
from tolokaforge.core.models import Message, MessageRole, ModelConfig

pytestmark = pytest.mark.canonical

_SAMPLING_KEYS = {"temperature", "top_p", "top_k"}
_OPENROUTER_KEY = "sk-or-loopback"


@pytest.fixture
def installed_fake_secrets(serving_gateway: RecordingGateway) -> Iterator[dict[str, str]]:
    """The gateway configured, with an OpenRouter key for the direct transport."""
    payload = {
        "LLM_PROXY_BASE_URL": serving_gateway.base_url,
        "LLM_PROXY_API_KEY": "sk-loopback-gateway",
        "OPENROUTER_API_KEY": _OPENROUTER_KEY,
    }
    with secret_manager_installed(payload):
        yield payload


def _say_hi(model: ModelConfig) -> LLMClient:
    client = LLMClient(model)
    client._retry_sleep = lambda _s: None
    client.generate(system="Be terse.", messages=[Message(role=MessageRole.USER, content="hi")])
    return client


def _routed(gateway: RecordingGateway, model: ModelConfig) -> dict[str, Any]:
    gateway.catalog = [f"openrouter/{model.name}"]
    client = _say_hi(model)
    assert client._gateway_route is not None
    [request] = gateway.requests
    assert request.model == f"openrouter/{model.name}"
    return request.body


def _direct(
    gateway: RecordingGateway, model: ModelConfig, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    monkeypatch.setenv("OPENROUTER_API_BASE", gateway.base_url)
    gateway_route.clear_catalog_cache()
    with secret_manager_installed({"OPENROUTER_API_KEY": _OPENROUTER_KEY}):
        client = _say_hi(model)
    assert client._gateway_route is None
    [request] = gateway.requests
    assert request.model == model.name
    return request.body


def _both(
    gateway: RecordingGateway, model: ModelConfig, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Any], dict[str, Any]]:
    routed = _routed(gateway, model)
    gateway.requests.clear()
    return routed, _direct(gateway, model, monkeypatch)


def _sampling(body: dict[str, Any]) -> dict[str, Any]:
    return {key: body[key] for key in _SAMPLING_KEYS & body.keys()}


@pytest.mark.parametrize("name", ["openai/gpt-5.2", "openai/gpt-6-astra", "openai/o3"])
def test_a_reasoning_model_sends_no_sampling_key_on_either_path(
    gateway: RecordingGateway, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    routed, direct = _both(gateway, ModelConfig(provider="openrouter", name=name), monkeypatch)

    assert _sampling(routed) == _sampling(direct) == {}


def test_a_model_that_takes_sampling_sends_the_default_on_both_paths(
    gateway: RecordingGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6")

    routed, direct = _both(gateway, model, monkeypatch)

    assert _sampling(routed) == _sampling(direct) == {"temperature": 0.0}


def test_the_escape_hatch_sends_temperature_on_a_provider_openai_route(
    gateway: RecordingGateway,
) -> None:
    """litellm admits ``temperature`` for the bare gpt-5.1+ names its map carries."""
    gateway.catalog = ["openai/gpt-5.2"]
    model = ModelConfig(
        provider="openai",
        name="gpt-5.2",
        temperature=0.7,
        capabilities={"supports_sampling_params": True},
    )

    assert _say_hi(model)._gateway_route is not None

    [request] = gateway.requests
    assert request.model == "gpt-5.2"
    assert _sampling(request.body) == {"temperature": 0.7}


def test_the_escape_hatch_on_a_provider_openrouter_config_breaks_the_gateway_route(
    gateway: RecordingGateway,
) -> None:
    """The ``openai`` transport refuses ``temperature`` for ``openrouter/openai/gpt-5.2``
    before sending anything, and the refusal is not retried."""
    gateway.catalog = ["openrouter/openai/gpt-5.2"]
    model = ModelConfig(
        provider="openrouter",
        name="openai/gpt-5.2",
        capabilities={"supports_sampling_params": True},
    )
    client = LLMClient(model)
    sleeps: list[float] = []
    client._retry_sleep = sleeps.append

    with pytest.raises(RuntimeError) as raised:
        client.generate(messages=[Message(role=MessageRole.USER, content="hi")])

    assert isinstance(raised.value.__cause__, litellm.UnsupportedParamsError)
    assert sleeps == []
    assert gateway.requests == []
