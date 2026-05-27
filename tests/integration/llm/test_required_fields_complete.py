"""Capability test — :attr:`Capability.REQUIRED_FIELDS_COMPLETE`.

Asserts that, when a tool's JSON schema marks N fields as ``required``
and the user turn supplies values for every one, the model emits a
tool call whose arguments include all N fields.

Where :attr:`SIMPLE_TOOL_CALL` only checks that a tool call happens,
this capability checks the **completeness** of the call's arguments.

Test shape — a single tool with five required fields and a user turn
that provides every value in plain text. The model needs to do
nothing more than copy each value into the matching slot. Failures on
this test are uniquely informative: the model has been told the
fields, has been given the values, has the schema in front of it, and
still drops a field.
"""

from __future__ import annotations

import json

import pytest

from tolokaforge.core.models import Message, MessageRole

from ._capability import Capability, ModelCertificate
from .registry import ALL_MODELS

_TOOL_NAME = "create_zendesk_ticket"
_REQUIRED_FIELDS: tuple[str, ...] = (
    "subject",
    "requester_id",
    "organization_id",
    "priority",
    "tags",
)


def _ticket_tool() -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": _TOOL_NAME,
            "description": (
                "Create a Zendesk support ticket. ALL fields below are required; "
                "every field must be populated from the user-provided values."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "string",
                        "description": "Ticket subject line.",
                    },
                    "requester_id": {
                        "type": "string",
                        "description": "ID of the user who submitted the ticket.",
                    },
                    "organization_id": {
                        "type": "string",
                        "description": "ID of the requester's organization.",
                    },
                    "priority": {
                        "type": "string",
                        "description": "One of low / normal / high / urgent.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of free-form tags applied to the ticket.",
                    },
                },
                "required": list(_REQUIRED_FIELDS),
            },
        },
    }


@pytest.mark.parametrize("cert", ALL_MODELS, ids=lambda c: c.model_id)
def test_required_fields_complete(
    cert: ModelCertificate,
    live_client,
    skip_unless_capability_declared,
) -> None:
    """All five required fields must appear in the emitted tool call.

    Assertions:

    1. ``result.tool_calls`` is non-empty.
    2. The selected tool is ``create_zendesk_ticket``.
    3. ``set(args.keys()) ⊇ _REQUIRED_FIELDS`` — every required field
       is present.
    4. Each value matches the literal the user provided (catches the
       partial-fill failure where the model invents a default rather
       than reading the prompt).
    """
    skip_unless_capability_declared(cert, Capability.REQUIRED_FIELDS_COMPLETE)

    client = live_client(cert)
    result = client.generate(
        system=(
            "You are a Zendesk agent. When the user describes a ticket, "
            "create it by calling the create_zendesk_ticket tool. ALL "
            "required schema fields MUST be populated from the values "
            "the user provided — do not omit any field, do not invent "
            "values for unspecified fields."
        ),
        messages=[
            Message(
                role=MessageRole.USER,
                content=(
                    "Please create a Zendesk ticket with the following:\n"
                    '- subject: "WMS access request for new hire"\n'
                    "- requester_id: USR-00000005\n"
                    "- organization_id: ORG-00000004\n"
                    "- priority: normal\n"
                    "- tags: [system_access, on_behalf]\n"
                ),
            )
        ],
        tools=[_ticket_tool()],
        tool_choice="auto",
    )

    assert result.tool_calls, f"{cert.model_id}: no tool call emitted. text={result.text[:120]!r}"
    tc = result.tool_calls[0]

    assert tc.name == _TOOL_NAME, f"{cert.model_id}: wrong tool {tc.name!r}"

    args = tc.arguments
    if isinstance(args, str):
        args = json.loads(args)
    assert isinstance(
        args, dict
    ), f"{cert.model_id}: arguments must be a dict, got {type(args).__name__}: {args!r}"

    missing = sorted(set(_REQUIRED_FIELDS) - set(args.keys()))
    assert not missing, (
        f"{cert.model_id}: emitted tool call is missing required fields {missing}. "
        f"Got args.keys()={sorted(args.keys())!r}. The model dropped fields the "
        "user explicitly provided values for."
    )

    # Sanity: literal-value preservation. Catches the variant where the
    # model emits the field but with a placeholder / inferred value.
    assert args["requester_id"] == "USR-00000005", (
        f"{cert.model_id}: requester_id={args['requester_id']!r} "
        "differs from the user-provided value 'USR-00000005' — the model "
        "filled the slot but did not preserve the literal."
    )
    assert args["organization_id"] == "ORG-00000004", (
        f"{cert.model_id}: organization_id={args['organization_id']!r} "
        "differs from the user-provided 'ORG-00000004'."
    )
