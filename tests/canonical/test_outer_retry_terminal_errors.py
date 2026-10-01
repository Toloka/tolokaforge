"""Contract: the outer retry does not re-attempt a refusal that cannot change on retry.

``_call_with_key_rotation`` re-raises every provider error as
``RuntimeError(...) from e``, so the outer controller sees the provider's
exception one link down the cause chain. A parameter set litellm refuses before
sending anything, and a 401, fail after one attempt; a 5xx keeps the five-attempt
budget.

Each case is a real :class:`LLMClient` on a resolved route of a loopback gateway.
Attempts are counted through the stubbed ``_retry_sleep`` (attempts = sleeps + 1),
never through recorded requests: under the ``openai`` transport the OpenAI SDK
re-sends a 5xx itself, so the request count is attempts times SDK sends.
"""

from __future__ import annotations

from collections.abc import Iterator

import litellm
import pytest

from tests.utils.recording_gateway import (
    RecordingGateway,
    auth_error_reply,
    server_error_reply,
    serving_recording_gateway,
)
from tests.utils.secret_state import secret_manager_installed
from tolokaforge.core.llm import gateway_route
from tolokaforge.core.llm.client import LLMClient
from tolokaforge.core.models import Message, MessageRole, ModelConfig

pytestmark = pytest.mark.canonical

MODEL = "self-hosted/retry-canary"


@pytest.fixture(scope="module")
def _serving_gateway() -> Iterator[RecordingGateway]:
    with serving_recording_gateway() as server:
        yield server


@pytest.fixture
def installed_fake_secrets(_serving_gateway: RecordingGateway) -> Iterator[dict[str, str]]:
    """Point the process SecretManager at the loopback gateway."""
    payload = {
        "LLM_PROXY_BASE_URL": _serving_gateway.base_url,
        "LLM_PROXY_API_KEY": "sk-loopback-gateway",
    }
    with secret_manager_installed(payload):
        yield payload


@pytest.fixture
def gateway(_serving_gateway: RecordingGateway) -> Iterator[RecordingGateway]:
    _serving_gateway.reset()
    _serving_gateway.catalog = [MODEL]
    gateway_route.clear_catalog_cache()
    yield _serving_gateway
    gateway_route.clear_catalog_cache()


def _failed_call(config: ModelConfig) -> tuple[BaseException, list[float]]:
    """Run one ``generate()`` that must fail; return its error and the backoff sleeps."""
    client = LLMClient(config)
    assert client._gateway_route is not None
    sleeps: list[float] = []
    client._retry_sleep = sleeps.append
    with pytest.raises(Exception) as raised:
        client.generate(system="Be terse.", messages=[Message(role=MessageRole.USER, content="hi")])
    return raised.value, sleeps


def _chain_types(exc: BaseException) -> list[type[BaseException]]:
    chain: list[type[BaseException]] = []
    link: BaseException | None = exc
    while link is not None and len(chain) < 8:
        chain.append(type(link))
        link = link.__cause__
    return chain


def test_a_parameter_set_litellm_refuses_is_attempted_once(gateway: RecordingGateway) -> None:
    """``reasoning_effort`` on a name litellm's map does not carry is refused in-process."""
    error, sleeps = _failed_call(
        ModelConfig(
            provider="openai", name=MODEL, reasoning={"mode": "adaptive", "effort_hint": "low"}
        )
    )

    assert litellm.UnsupportedParamsError in _chain_types(error), _chain_types(error)
    assert sleeps == []
    assert gateway.requests == []


def test_a_gateway_401_is_attempted_once(gateway: RecordingGateway) -> None:
    """The OpenAI SDK does not re-send a 401, so one attempt is one request."""
    gateway.scripts[MODEL] = [auth_error_reply()] * 5

    error, sleeps = _failed_call(ModelConfig(provider="openai", name=MODEL))

    assert litellm.AuthenticationError in _chain_types(error), _chain_types(error)
    assert sleeps == []
    assert len(gateway.requests) == 1


def test_a_gateway_500_keeps_the_five_attempt_budget(gateway: RecordingGateway) -> None:
    gateway.scripts[MODEL] = [server_error_reply()] * 50

    _, sleeps = _failed_call(ModelConfig(provider="openai", name=MODEL))

    assert len(sleeps) == 4
    assert gateway.requests, "the 500 never reached the client"
