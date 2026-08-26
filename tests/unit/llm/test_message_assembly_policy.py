"""Message-assembly slot — empty-assistant-content filler behaviour.

Locks the two contracts that come with the narrow ``message_assembly_policy``
slot:

1. The three policy classes route the filler correctly through
   ``LLMClient._convert_messages``:
   - ``NullMessageAssembly`` (default) leaves empty assistant content empty.
   - ``NovaMessageAssembly()`` (bare-name overlay shape) substitutes the
     default filler ``"I'll help you with that."``.
   - ``NovaMessageAssembly(empty_assistant_filler="…")`` (``{name, params}``
     overlay shape) substitutes the caller-supplied filler — i.e. the string
     is data on the policy instance, not an engine constant.

2. Non-empty content is untouched regardless of policy — the filler gate only
   fires on empty / whitespace-only content on ``ASSISTANT`` turns that carry
   ``tool_calls``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.core.llm import LLMClient
from tolokaforge.core.llm.message_assembly_policy import (
    NovaMessageAssembly,
    NullMessageAssembly,
)
from tolokaforge.core.llm.presets import (
    build_capabilities,
    resolve_policy_names,
    set_overlay_path,
)
from tolokaforge.core.models import Message, MessageRole, ModelConfig, ToolCall

pytestmark = pytest.mark.unit


def _assistant_content(converted: list[dict]) -> str:
    return next(m for m in converted if m["role"] == "assistant")["content"]


def _empty_assistant_with_tool_call() -> list[Message]:
    return [
        Message(role=MessageRole.USER, content="hi"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[ToolCall(id="t", name="tool", arguments={})],
        ),
    ]


# ---------------------------------------------------------------------------
# Filler-injection routing on the wire
# ---------------------------------------------------------------------------


def test_null_policy_leaves_empty_assistant_content_empty() -> None:
    client = LLMClient(ModelConfig(provider="openrouter", name="openai/gpt-4o"))
    assert isinstance(client.capabilities.message_assembly_policy, NullMessageAssembly)

    converted = client._convert_messages(system=None, messages=_empty_assistant_with_tool_call())
    assert _assistant_content(converted) == ""


def test_nova_policy_default_filler_matches_shipped_string() -> None:
    client = LLMClient(ModelConfig(provider="nova", name="nova-pro"))
    policy = client.capabilities.message_assembly_policy
    assert isinstance(policy, NovaMessageAssembly)
    assert policy.inject_empty_assistant_filler is True
    assert policy.empty_assistant_filler == "I'll help you with that."

    converted = client._convert_messages(system=None, messages=_empty_assistant_with_tool_call())
    assert _assistant_content(converted) == "I'll help you with that."


def test_moonshot_kimi_k3_preset_substitutes_space_filler_on_wire() -> None:
    """The ``moonshot_kimi_k3`` preset overlays the default filler with a
    single space. This locks the wire behaviour end-to-end for the
    Moonshot-direct HTTP 400 fix in issue #1284: on an empty
    assistant-with-tool_calls turn, ``_convert_messages`` must emit
    ``content == " "`` rather than ``""``.
    """
    client = LLMClient(ModelConfig(provider="openrouter", name="moonshotai/kimi-k3"))
    policy = client.capabilities.message_assembly_policy
    assert isinstance(policy, NovaMessageAssembly)
    assert policy.inject_empty_assistant_filler is True
    assert policy.empty_assistant_filler == " "

    converted = client._convert_messages(system=None, messages=_empty_assistant_with_tool_call())
    assert _assistant_content(converted) == " "


def test_nova_policy_overlay_params_propagate_through_wire(
    tmp_path: Path,
) -> None:
    """The ``{name, params}`` slot shape lets a preset overlay override the
    filler string. The overridden string reaches ``_convert_messages`` — the
    filler is policy *data*, not an engine constant.
    """
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        "presets:\n"
        "  custom_nova:\n"
        "    match: ['custom-nova/*']\n"
        "    content_policy: nova\n"
        "    message_assembly_policy:\n"
        "      name: nova\n"
        "      params:\n"
        "        empty_assistant_filler: 'Certainly.'\n"
    )
    set_overlay_path(str(overlay))
    try:
        caps = build_capabilities("custom-nova/model", "custom")
        assert isinstance(caps.message_assembly_policy, NovaMessageAssembly)
        assert caps.message_assembly_policy.empty_assistant_filler == "Certainly."
        assert resolve_policy_names(caps)["message_assembly_policy"] == "nova"

        client = LLMClient(ModelConfig(provider="custom", name="custom-nova/model"))
        converted = client._convert_messages(
            system=None, messages=_empty_assistant_with_tool_call()
        )
        assert _assistant_content(converted) == "Certainly."
    finally:
        set_overlay_path(None)


def test_non_empty_content_survives_regardless_of_policy() -> None:
    """The filler substitution only fires on empty / whitespace-only content."""
    real_content = "Working on it."
    messages = [
        Message(role=MessageRole.USER, content="hi"),
        Message(
            role=MessageRole.ASSISTANT,
            content=real_content,
            tool_calls=[ToolCall(id="t", name="tool", arguments={})],
        ),
    ]
    for provider, name in [
        ("openrouter", "openai/gpt-4o"),
        ("openrouter", "google/gemini-3.1-pro-preview"),
        ("nova", "nova-pro"),
    ]:
        client = LLMClient(ModelConfig(provider=provider, name=name))
        converted = client._convert_messages(system=None, messages=messages)
        got = _assistant_content(converted)
        msg = f"{provider}/{name} corrupted non-empty assistant content: got {got!r}"
        assert got == real_content, msg


# ---------------------------------------------------------------------------
# Fingerprint + slot registry
# ---------------------------------------------------------------------------


def test_resolve_policy_names_includes_message_assembly_slot() -> None:
    """The wire fingerprint carries the new slot for every preset."""
    nova_caps = build_capabilities("nova-pro", "nova")
    default_caps = build_capabilities("openai/gpt-4o", "openrouter")

    assert resolve_policy_names(nova_caps)["message_assembly_policy"] == "nova"
    assert resolve_policy_names(default_caps)["message_assembly_policy"] == "null"
