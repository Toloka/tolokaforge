"""Capability test — :attr:`Capability.LEXICAL_TOOL_INVENTION`.

Asserts that a model offered a small concrete toolset does NOT
fabricate a plausibly-named tool derived from words it sees in the
system prompt.

Concrete regression: Gemini 3.1 Pro is observed to emit
``knowledge_base_search_policy`` when the system prompt mentions "the
knowledge base" — even though the registered tool is named
``typesense_search_policy``. The model conflates the prompt phrasing
with a fabricated tool name that doesn't exist, and the harness
rejects every such call with ``Tool '…' not found in agent tools``.

Where :attr:`Capability.TOOL_NAME_DISCIPLINE` catches *structural*
malformations of an offered name, this capability catches *lexical
invention* — a different failure surface that single-name discipline
does not exercise.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.models import Message, MessageRole

from ._capability import Capability, ModelCertificate
from .registry import ALL_MODELS

# The forbidden lexical family. Anything starting with
# ``knowledge_base_`` is the documented Gemini 3.1 Pro fabrication;
# the specific full hallucinations are spelled out for the error
# message.
_FORBIDDEN_PREFIX = "knowledge_base_"
_FORBIDDEN_NAMES = (
    "knowledge_base_search_policy",
    "knowledge_base_search",
    "knowledge_base_query",
)

_REGISTERED_TYPESENSE = "typesense_search_policy"
_REGISTERED_ZENDESK = "zendesk_search_articles"


def _typesense_tool() -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": _REGISTERED_TYPESENSE,
            "description": "Search the knowledge base for policy snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Free-text search query.",
                    },
                },
                "required": ["query"],
            },
        },
    }


def _zendesk_tool() -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": _REGISTERED_ZENDESK,
            "description": "Search published Zendesk help-centre articles.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
            },
        },
    }


@pytest.mark.parametrize("cert", ALL_MODELS, ids=lambda c: c.model_id)
def test_lexical_tool_invention(
    cert: ModelCertificate,
    live_client,
    skip_unless_capability_declared,
) -> None:
    """The emitted tool name MUST be one of the offered names — not a
    fabrication derived from the system-prompt vocabulary.

    Assertions:

    1. ``result.tool_calls`` is non-empty.
    2. The selected tool name is in the offered set.
    3. The name does NOT start with the documented ``knowledge_base_``
       prefix that Gemini 3.1 Pro hallucinates. This is a redundant
       check given (2) succeeds, but it gives a more informative
       error message when (2) fails.
    """
    skip_unless_capability_declared(cert, Capability.LEXICAL_TOOL_INVENTION)

    client = live_client(cert)
    result = client.generate(
        # System prompt deliberately echoes the regression trigger:
        # the phrase "knowledge base" is mentioned multiple times
        # even though the registered tool is named
        # ``typesense_search_policy``. If the model is going to
        # invent ``knowledge_base_search_policy``, this is the
        # prompt shape that makes it happen.
        system=(
            "You are a customer-support agent. The knowledge base is your "
            "primary reference for company policy. When the user asks a "
            "policy question, search the knowledge base by calling the "
            "appropriate tool from the list below. Use the EXACT tool name "
            "from the schema — do not invent tool names from the prompt."
        ),
        messages=[
            Message(
                role=MessageRole.USER,
                content=(
                    "Search the knowledge base for the section on system-access "
                    "approval requirements for new employees."
                ),
            )
        ],
        tools=[_typesense_tool(), _zendesk_tool()],
        tool_choice="auto",
    )

    assert result.tool_calls, f"{cert.model_id}: no tool call emitted. text={result.text[:120]!r}"
    tc = result.tool_calls[0]

    offered = {_REGISTERED_TYPESENSE, _REGISTERED_ZENDESK}
    assert tc.name in offered, (
        f"{cert.model_id}: emitted invented tool name {tc.name!r}. "
        f"Offered tools were {sorted(offered)!r}. Common documented "
        f"hallucinations: {list(_FORBIDDEN_NAMES)}. The model is "
        "synthesising a tool name from the system-prompt vocabulary "
        "rather than using the registered schema."
    )

    assert not tc.name.startswith(_FORBIDDEN_PREFIX), (
        f"{cert.model_id}: tool name {tc.name!r} matches the documented "
        f"``{_FORBIDDEN_PREFIX}*`` Gemini 3.1 Pro fabrication family. "
        f"The registered knowledge-base tool is {_REGISTERED_TYPESENSE!r}."
    )
