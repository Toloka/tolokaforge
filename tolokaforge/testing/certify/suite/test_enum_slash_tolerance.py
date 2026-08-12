"""Capability test — :attr:`Capability.ENUM_SLASH_TOLERANCE`.

Probes a provider quirk: xAI's grok-4.3 endpoint rejects tool schemas
whose enum values contain
``/`` (forward slash) with the opaque error
``OpenrouterException - Invalid arguments passed to the model``.

Repro bisected locally to a single enum value
(``"income/salary verification letter"``) on a single tool
(``hr_service_api_hr_service_api_create_document_generation_request``).
Replacing the ``/`` with any non-slash separator makes the same payload
accepted; submitting *only* that one ``/``-containing enum value
(nothing else) makes it reject. Other providers — OpenAI GPT-5.x,
Anthropic Opus 4.6/4.7, DeepSeek V4, Qwen 3.6, Kimi K2.6, MiMo V2.5,
Gemini Flash + 3.1 Pro, and crucially grok-4 (the older sibling on the
same xAI provider) — all accept the slashed enum.

The contract this test gates is the minimum every well-behaved
provider should support: arbitrary printable-ASCII content inside enum
string values, including ``/``. A real-world OTS schema relied on this
working (the `document_name` enum is the canonical example), and a
provider that can't handle it is unusable on those task surfaces.

Failure mode at integration-test time is the opaque API error, not a
test assertion — the harness's :class:`~tolokaforge.core.llm.client.LLMClient`
``@retry`` loop burns 5 attempts and then surfaces the exception, which
this test lets bubble. A passing run is "no exception during
``client.generate``"; we additionally assert the model produced
*something* (text or tool call), so a provider that silently swallows
the schema and returns nothing also fails.
"""

from __future__ import annotations

from typing import Any

import pytest

from tolokaforge.core.models import Message, MessageRole
from tolokaforge.testing.certify import ALL_MODELS, Capability, ModelCertificate

# Minimal tool whose `enum` shape reproduces the eval failure. Single
# property, single enum value containing `/` — anything beyond this is
# decorative.
_TOOL_WITH_SLASH_ENUM: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "create_document",
        "description": (
            "Create a document of the specified type. Pick one of the "
            "supported document categories from the `doc_type` enum."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "doc_type": {
                    "type": "string",
                    "enum": [
                        # The trigger value. Mirrors
                        # bank/HR evaluation domain /
                        # ``hr_service_api_hr_service_api_create_document_generation_request``
                        # / ``document_name`` enum from the OTS eval.
                        "income/salary verification letter",
                        "employment verification letter",
                        "pay stubs",
                    ],
                    "description": "Document type to create.",
                },
            },
            "required": ["doc_type"],
        },
    },
}


_SYSTEM = (
    "You are a document-generation assistant. When asked to create a "
    "document, use the `create_document` tool with the matching "
    "`doc_type` value from the enum."
)


@pytest.mark.parametrize("cert", ALL_MODELS, ids=lambda c: c.model_id)
def test_enum_slash_tolerance(
    cert: ModelCertificate,
    live_client,
    skip_unless_capability_declared,
) -> None:
    """Provider accepts a tool schema whose enum values contain ``/``.

    Assertions:

    1. ``client.generate`` returns without raising — the upstream
       provider didn't reject the schema. The retry-and-surface logic
       in :class:`~tolokaforge.core.llm.client.LLMClient` is what we
       rely on; if all 5 attempts fail with
       ``Invalid arguments passed to the model``, the exception
       propagates and this test fails.
    2. The model produced either a tool call or non-empty text — a
       provider that silently no-ops on the schema (some sanitiser
       paths can do this) also fails the capability.
    """
    skip_unless_capability_declared(cert, Capability.ENUM_SLASH_TOLERANCE)
    client = live_client(cert)

    result = client.generate(
        system=_SYSTEM,
        messages=[
            Message(
                role=MessageRole.USER,
                content="Please create an income/salary verification letter for me.",
            )
        ],
        tools=[_TOOL_WITH_SLASH_ENUM],
        tool_choice="auto",
        # 256 not 64: providers that emit reasoning tokens (Kimi K2.6,
        # Gemini 3.1 Pro) can chew through a small cap on internal
        # reasoning before producing any visible content, even with
        # ``ReasoningConfig(mode="off")``. The capability we gate is
        # schema acceptance, not token efficiency.
        max_tokens=256,
    )

    has_response = bool(result.tool_calls) or bool((result.text or "").strip())
    assert has_response, (
        f"{cert.model_id}: provider accepted the slashed-enum schema but "
        f"returned an empty response (no tool call, no text). Some "
        f"sanitiser paths silently strip the offending enum and emit "
        f"nothing — that's not the contract we want either."
    )
