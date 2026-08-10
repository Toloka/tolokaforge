"""Capability test — :attr:`Capability.MULTI_TURN_ERROR_RECOVERY`.

Mirror of a production eval failure surface where the model emitted a tool call missing a
runtime-required field, the tool returned an **explicit** error message
naming the missing field, and the model re-emitted the identical broken
call 5+ times rather than incorporating the feedback.

Design — fully synthesised history for determinism. We hand-craft:

1. A user request containing the value the model will need
   (``maria.delgado@example.com``).
2. A fake assistant turn with a ``create_support_case`` tool call that
   omits both ``contact_id`` and ``contact_email`` — replicating the
   exact broken shape from eval.
3. A fake tool response carrying the runtime validation error.

We then ask the live model to continue from that history. A passing
model produces a corrected tool call with ``contact_email`` (or
``contact_id``) populated. A failing model either reproduces the broken
call verbatim, gives up with text, or invokes a different tool.

Why a synthesised turn-1 rather than a real two-pass loop: a real
loop's turn-1 might *correctly* populate the contact field, which would
make the test vacuous. Synthesising forces the exact failure scenario.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tolokaforge.core.models import Message, MessageRole, ToolCall
from tolokaforge.testing.certify import ALL_MODELS, Capability, ModelCertificate

_USER_EMAIL = "maria.delgado@example.com"

_USER_REQUEST = (
    f"Please open a high-priority support case for our customer "
    f"{_USER_EMAIL}. They reported a sink leak in the kitchen of "
    f"store 47 that needs maintenance dispatched today. Use the "
    f"create_support_case tool."
)

# Tool schema mirrors the QSR / OTS bug shape: contact_id and
# contact_email are BOTH optional in the JSON Schema (``required: [subject]``
# only), but the tool's runtime contract is "at least one of contact_id /
# contact_email must be non-null". That contract lives in the description
# text — a passing model has to read it.
_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "create_support_case",
        "description": (
            "Create a support case in the help desk. "
            "**Either contact_id or contact_email is required** to "
            "identify the requester. The subject is a short title. "
            "Returns the case ID on success."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "description": "Short title of the case.",
                },
                "contact_id": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                    "description": (
                        "Internal contact identifier. Either contact_id "
                        "or contact_email must be provided."
                    ),
                },
                "contact_email": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                    "description": (
                        "Contact email for lookup. Either contact_id or "
                        "contact_email must be provided."
                    ),
                },
                "priority": {
                    "anyOf": [
                        {"enum": ["low", "normal", "high", "urgent"], "type": "string"},
                        {"type": "null"},
                    ],
                    "default": None,
                    "description": "Ticket priority bucket.",
                },
            },
            "required": ["subject"],
        },
    },
}


_SYSTEM = (
    "You are a support-desk assistant. Use the create_support_case "
    "tool to open tickets when the user asks. If a prior tool call "
    "returned an error, read the error carefully and correct the next "
    "call accordingly — never re-issue an identical broken call."
)


# The synthesised turn-1 tool call. Missing BOTH contact_id and
# contact_email — the exact wire shape grok-4.3 emitted on eval.
_BROKEN_TOOL_CALL_ID = "call_recovery_probe_01"
_BROKEN_TOOL_ARGS = {
    "subject": "Sink leak in kitchen at store 47 — high priority",
    "priority": "high",
}

_TOOL_ERROR_CONTENT = (
    "Error: missing_required_field: either contact_id or contact_email must be provided"
)


def _decoded_args(raw: Any) -> dict[str, Any]:
    """Return tool-call arguments as a dict, decoding JSON-strings.

    The harness's response-policy layer should have already unstringed
    container args by this point; we fall back to ``json.loads`` to
    keep the test focused on the ERROR-RECOVERY contract rather than
    re-asserting :attr:`Capability.DICT_MAP_TOOL_CALL`.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            pytest.fail(f"tool-call arguments arrived as a non-JSON string: {raw[:300]!r}")
    assert isinstance(
        raw, dict
    ), f"tool-call arguments must be a dict, got {type(raw).__name__}: {raw!r}"
    return raw


@pytest.mark.parametrize("cert", ALL_MODELS, ids=lambda c: c.model_id)
def test_multi_turn_error_recovery(
    cert: ModelCertificate,
    live_client,
    skip_unless_capability_declared,
) -> None:
    """Model receives an explicit tool error and corrects the next call.

    Assertions:

    1. The model emits a tool call on the recovery turn (didn't give up
       with prose).
    2. The tool name is ``create_support_case`` (it picked the right
       retry target).
    3. At least one of ``contact_id`` / ``contact_email`` is now a
       non-empty string — the field the error pointed at is populated.
    4. ``subject`` is still present (the model didn't regress on
       previously-correct fields).
    5. When ``contact_email`` is populated, its value contains the
       email from the original user message — proving the model
       sourced the value from context rather than fabricating one.
    """
    skip_unless_capability_declared(cert, Capability.MULTI_TURN_ERROR_RECOVERY)
    client = live_client(cert)

    synthesised_assistant = Message(
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=[
            ToolCall(
                id=_BROKEN_TOOL_CALL_ID,
                name="create_support_case",
                arguments=dict(_BROKEN_TOOL_ARGS),
            )
        ],
    )
    tool_error = Message(
        role=MessageRole.TOOL,
        content=_TOOL_ERROR_CONTENT,
        tool_call_id=_BROKEN_TOOL_CALL_ID,
    )

    result = client.generate(
        system=_SYSTEM,
        messages=[
            Message(role=MessageRole.USER, content=_USER_REQUEST),
            synthesised_assistant,
            tool_error,
        ],
        tools=[_TOOL],
        tool_choice="auto",
    )

    assert result.tool_calls, (
        f"{cert.model_id}: model gave up after tool error rather than "
        f"retrying with the missing field. text={result.text[:300]!r}. "
        f"This is the eval failure surface where grok-4.3 ignored "
        f"explicit error feedback and re-emitted the identical broken call."
    )

    tc = result.tool_calls[0]
    assert tc.name == "create_support_case", (
        f"{cert.model_id}: model retried with wrong tool {tc.name!r}; "
        f"expected create_support_case retry. The only tool we offered was "
        f"create_support_case, so any other name is a hallucination."
    )

    args = _decoded_args(tc.arguments)

    contact_id = args.get("contact_id")
    contact_email = args.get("contact_email")
    has_contact = (isinstance(contact_id, str) and contact_id.strip()) or (
        isinstance(contact_email, str) and contact_email.strip()
    )
    assert has_contact, (
        f"{cert.model_id}: retried call still has both contact_id and "
        f"contact_email empty — error feedback was ignored. The original "
        f"user message contained the email {_USER_EMAIL!r} but the model "
        f"failed to surface it into the corrected call. Args: {args!r}"
    )

    assert args.get("subject"), (
        f"{cert.model_id}: model dropped the previously-correct "
        f"`subject` field while fixing contact. Args: {args!r}"
    )

    if isinstance(contact_email, str) and contact_email.strip():
        assert _USER_EMAIL.lower() in contact_email.lower(), (
            f"{cert.model_id}: contact_email={contact_email!r} doesn't "
            f"contain the email from the user message ({_USER_EMAIL!r}). "
            f"The model invented a value instead of reading the prompt."
        )
