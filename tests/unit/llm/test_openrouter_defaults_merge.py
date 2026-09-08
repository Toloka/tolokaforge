"""Unit — field-by-field merge of user + preset OpenRouter routing.

Locks the shape of the merge :meth:`LLMClient._build_kwargs` performs
when assembling ``extra_body.provider``. Each field is resolved
independently over user config and preset default:

* ``provider_order`` — user's list wins when non-empty; the preset default
  fills the gap; both unset → no pin lands.
* ``allow_fallbacks`` — user's value wins when the ``openrouter:`` block
  is present at all (a bool has no ``None`` sentinel); the preset default
  fills the gap.

The field-by-field merge lock lives in
``test_partial_user_openrouter_config_keeps_preset_provider_pin``: a
plain ``or`` short-circuit (``user_or or preset_or``) would let a partial
user config (``allow_fallbacks: false`` only) shadow the preset's pin,
which is exactly the fan-out failure the preset default was written to
prevent.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

from tolokaforge.core.llm.capabilities import ModelCapabilities
from tolokaforge.core.llm.client import LLMClient
from tolokaforge.core.models import Message, MessageRole, ModelConfig, OpenRouterConfig
from tolokaforge.secrets import DictProvider, SecretManager
from tolokaforge.secrets import manager as secrets_manager

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
    original_manager = secrets_manager._default_manager
    original_env = dict(os.environ)
    try:
        yield
    finally:
        secrets_manager._default_manager = original_manager
        os.environ.clear()
        os.environ.update(original_env)


def _make_client(
    *,
    openrouter: OpenRouterConfig | None,
    preset_defaults: OpenRouterConfig | None,
) -> LLMClient:
    secrets_manager._default_manager = SecretManager(
        [DictProvider({"OPENROUTER_API_KEY": "sk-or"})]
    )
    client = LLMClient(
        ModelConfig(
            provider="openrouter/moonshotai",
            name="moonshotai/kimi-k3",
            openrouter=openrouter,
        )
    )
    if preset_defaults is not None or client.capabilities.openrouter_defaults is not None:
        client.capabilities = ModelCapabilities(
            schema_sanitizer=client.capabilities.schema_sanitizer,
            prompt_policy=client.capabilities.prompt_policy,
            content_policy=client.capabilities.content_policy,
            params_policy=client.capabilities.params_policy,
            response_policy=client.capabilities.response_policy,
            reasoning_codec=client.capabilities.reasoning_codec,
            cache_policy=client.capabilities.cache_policy,
            message_assembly_policy=client.capabilities.message_assembly_policy,
            assistant_text_policy=client.capabilities.assistant_text_policy,
            api_call_timeout_s=client.capabilities.api_call_timeout_s,
            api_call_wall_timeout_s=client.capabilities.api_call_wall_timeout_s,
            api_call_retries=client.capabilities.api_call_retries,
            empty_retry_count=client.capabilities.empty_retry_count,
            openrouter_defaults=preset_defaults,
        )
    return client


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


_MOONSHOT_PIN = OpenRouterConfig(provider_order=["moonshotai"], allow_fallbacks=False)


def test_openrouter_defaults_drives_provider_pin() -> None:
    """No user config, preset default set → preset default lands on the wire."""
    client = _make_client(openrouter=None, preset_defaults=_MOONSHOT_PIN)
    kwargs = _kwargs(client)
    assert kwargs["extra_body"]["provider"] == {
        "order": ["moonshotai"],
        "allow_fallbacks": False,
    }


def test_user_openrouter_config_wins_over_capabilities_default() -> None:
    """User set BOTH fields → both user values land, preset shadowed field-wise."""
    client = _make_client(
        openrouter=OpenRouterConfig(provider_order=["deepinfra"], allow_fallbacks=True),
        preset_defaults=_MOONSHOT_PIN,
    )
    kwargs = _kwargs(client)
    assert kwargs["extra_body"]["provider"] == {
        "order": ["deepinfra"],
        "allow_fallbacks": True,
    }


def test_partial_user_openrouter_config_keeps_preset_provider_pin() -> None:
    """User set ONLY ``allow_fallbacks`` → preset's
    ``provider_order`` still lands on the wire, with the user's
    ``allow_fallbacks``. A regression to ``user or preset`` short-circuits
    to the empty-``provider_order`` user config and drops the pin entirely,
    which is exactly the fan-out failure the preset default prevents."""
    client = _make_client(
        openrouter=OpenRouterConfig(allow_fallbacks=False),
        preset_defaults=_MOONSHOT_PIN,
    )
    kwargs = _kwargs(client)
    assert kwargs["extra_body"]["provider"] == {
        "order": ["moonshotai"],
        "allow_fallbacks": False,
    }


def test_no_user_config_uses_full_preset_openrouter_defaults() -> None:
    """Baseline for the fix: user's ``openrouter`` block is None; the preset
    default's ``provider_order`` and ``allow_fallbacks`` both land on the
    wire. This is the path the shipped ``moonshot_kimi_k3`` preset takes
    on every run where the operator does not override it."""
    client = _make_client(openrouter=None, preset_defaults=_MOONSHOT_PIN)
    kwargs = _kwargs(client)
    provider = kwargs["extra_body"]["provider"]
    assert provider["order"] == ["moonshotai"]
    assert provider["allow_fallbacks"] is False


def test_no_user_config_no_preset_default_leaves_pin_off_the_wire() -> None:
    """Neither side pinned → ``extra_body.provider`` never lands. Pins the
    pre-opt-in baseline: the new capability slot is purely additive."""
    client = _make_client(openrouter=None, preset_defaults=None)
    kwargs = _kwargs(client)
    assert "provider" not in kwargs.get("extra_body", {})
