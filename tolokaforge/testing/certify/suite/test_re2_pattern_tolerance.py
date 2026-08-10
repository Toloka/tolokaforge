"""Capability test — :attr:`Capability.RE2_PATTERN_TOLERANCE`.

Probes whether the provider accepts a tool-schema ``pattern`` field
carrying a RE2-incompatible regex (lookarounds / backreferences). The
canonical example is Pydantic's ``Decimal``-string idiom pattern
``"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$"`` embedded inside an
``Optional[str]`` branch — the exact shape that triggers xAI grok-4.3's
opaque ``Invalid arguments passed to the model`` rejection on
production tools (eg ``d365_api_create_case.custom_refund_amount`` in
a travel marketplace evaluation domain).

Companion ratchet test:
:mod:`test_re2_pattern_tolerance_unsupported_ratchet` exercises the
``known_unsupported`` branch — when xAI relaxes the validator, the
ratchet trips, grok-4.3 flips to ``required``, and we can drop the
``StrictSchema.strip_re2_incompatible_patterns`` strip.

**Sanitiser bypass.** The harness's
:class:`~tolokaforge.core.llm.schema_sanitizer.StrictSchema` ordinarily
strips the lookaround pattern before the schema reaches grok-4.3. To
probe the upstream validator's actual behaviour, the test overrides
``client.capabilities.schema_sanitizer`` with
:class:`~tolokaforge.core.llm.schema_sanitizer.PassthroughSchema` for
the duration of one call — the cert's normal preset is still resolved
(so the model's other policies stay correct) but the schema reaches
the wire unfiltered.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from tolokaforge.core.llm.schema_sanitizer import PassthroughSchema
from tolokaforge.core.models import Message, MessageRole
from tolokaforge.testing.certify import ALL_MODELS, Capability, ModelCertificate

# Pydantic-Decimal-string idiom pattern. Lookahead-bearing → RE2-
# incompatible. Embedded in an ``Optional[str]`` (anyOf string + null)
# so it skips the Decimal-anyOf collapse path and lands on the raw
# pattern-strip rule when StrictSchema is the active sanitiser.
_RE2_INCOMPAT_PATTERN = r"^(?!^[-+.]*$)[+-]?0*\d*\.?\d*$"

_TOOL_WITH_RE2_INCOMPAT_PATTERN: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "create_case",
        "description": (
            "Open a case for an incident. Use the optional "
            "`custom_refund_amount` field if the request involves a refund."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short case title.",
                },
                "custom_refund_amount": {
                    "anyOf": [
                        {"type": "string", "pattern": _RE2_INCOMPAT_PATTERN},
                        {"type": "null"},
                    ],
                    "default": None,
                    "description": "Refund amount in dollars (decimal string).",
                    "examples": ["350.00"],
                    "title": "Custom Refund Amount",
                },
            },
            "required": ["title"],
        },
    },
}

_SYSTEM = (
    "You are an incident-response assistant. When the user reports an "
    "incident, call the `create_case` tool with a `title` and, when the "
    "user mentions a refund, populate `custom_refund_amount`."
)

_USER_TURN = (
    "Open a case titled 'Damaged TV in room 207' for a guest who is requesting a refund of $350.00."
)


def _with_passthrough_sanitiser(client):
    """Replace ``client.capabilities.schema_sanitizer`` with
    ``PassthroughSchema`` so the RE2-incompatible ``pattern`` reaches
    the provider unfiltered. The cert's other policies (content,
    response, reasoning codec, cache, params) stay intact — only the
    schema route changes.
    """
    client.capabilities = replace(client.capabilities, schema_sanitizer=PassthroughSchema())


@pytest.mark.parametrize("cert", ALL_MODELS, ids=lambda c: c.model_id)
def test_re2_pattern_tolerance(
    cert: ModelCertificate,
    live_client,
    skip_unless_capability_declared,
) -> None:
    """Provider accepts a tool schema carrying a RE2-incompatible ``pattern``.

    Assertions:

    1. ``client.generate`` returns without raising — the provider
       didn't reject the schema. ``LLMClient``'s ``@retry`` loop will
       exhaust attempts and surface the exception if the validator
       refuses every try; we let that propagate.
    2. The model produced either a tool call or non-empty text — a
       provider that silently no-ops on the schema also fails the
       capability.
    """
    skip_unless_capability_declared(cert, Capability.RE2_PATTERN_TOLERANCE)
    client = live_client(cert)
    _with_passthrough_sanitiser(client)

    result = client.generate(
        system=_SYSTEM,
        messages=[Message(role=MessageRole.USER, content=_USER_TURN)],
        tools=[_TOOL_WITH_RE2_INCOMPAT_PATTERN],
        tool_choice="auto",
        # 1024 budget: Gemini 3.1 Pro chews through small caps on
        # internal reasoning before emitting any visible content, even
        # with ``ReasoningConfig(mode="off")``. The capability we gate
        # is schema acceptance, not token efficiency — keep the cap
        # generous so an empty response unambiguously means the schema
        # was no-op'd by the provider, not that we starved the budget.
        max_tokens=1024,
    )

    has_response = bool(result.tool_calls) or bool((result.text or "").strip())
    assert has_response, (
        f"{cert.model_id}: provider accepted the RE2-incompatible-pattern "
        f"schema but returned an empty response (no tool call, no text). "
        f"A provider that silently no-ops on the schema is not the "
        f"contract we want either."
    )
