"""Capability test — :attr:`Capability.DISCRIMINATED_UNION_TOOL_CALL`.

Mirror of a production eval failure surface where open-weights
routes (mimo, deepseek-v4, Kimi K2) emitted the discriminated-
union member as a JSON-encoded string instead of a native dict on
``zendesk_create_item`` and similar tools::

    # Buggy wire payload (model side):
    {"table": "tickets", "item": "{\\"subject\\": \\"...\\"}"}
    # What the harness pydantic validator expects:
    {"table": "tickets", "item": {"subject": "..."}}

``JsonCoerceResponse`` recovers the first into the second; this test
asserts the contract end-to-end against the live provider over **two
turns** so a single-turn stringification fluke can't pass while the
real recovery path is broken.

Tool shape mirrors the eval failure: a ``write_item`` function that
accepts ``table: str`` plus ``item`` typed as a discriminated union over
four entity-create variants (Ticket / User / Organization / Comment).
The model must:

1. **Turn 1** — produce a tool call whose ``item`` round-trips to a
   native dict shaped as a ``Ticket`` create payload (``kind: ticket``,
   ``subject``, ``priority``).
2. **Turn 2** — after observing a fake tool result, produce a follow-up
   tool call that round-trips as a ``Comment`` create payload
   (``kind: comment``, ``ticket_id``, ``body``).

Asserting two different variants in sequence catches model adapters
that happen to special-case the first variant but stringify the
others.

**Two union shapes are exercised** because Pydantic emits different
JSON-Schema constructs for each, and providers historically handle
them asymmetrically:

* ``explicit_discriminator`` — ``Annotated[A | B | …, Field(discriminator="kind")]``
  → ``oneOf`` + ``discriminator`` keyword. Pre-2026-05-20 Gemini
  failure surface for the synthetic field-rename bug.
* ``bare_union`` — ``A | B | …`` (no ``Annotated`` / ``Field(discriminator=…)``)
  → ``anyOf`` with inline branches, no ``discriminator`` keyword. The
  shape every production OTS tool uses (``zendesk_create_item``,
  ``d365_api_create_case`` discriminated unions, …). Pre-2026-05-20
  Gemini ``GeminiSchema`` over-flattened this shape into a single
  35-field merged object and lost per-branch field guidance, regressing
  pass rate from 40 % to 0 %.
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Any, Literal

import pytest
from pydantic import BaseModel, Field, TypeAdapter

from tolokaforge.core.models import Message, MessageRole
from tolokaforge.testing.certify import ALL_MODELS, Capability, ModelCertificate


class _TicketCreate(BaseModel):
    kind: Literal["ticket"]
    subject: str = Field(description="Short ticket title.")
    priority: Literal["low", "normal", "high", "urgent"] = Field(
        description="Ticket priority bucket."
    )


class _UserCreate(BaseModel):
    kind: Literal["user"]
    email: str = Field(description="User email.")
    name: str = Field(description="Display name.")


class _OrgCreate(BaseModel):
    kind: Literal["org"]
    name: str = Field(description="Organization display name.")


class _CommentCreate(BaseModel):
    kind: Literal["comment"]
    ticket_id: str = Field(description="Parent ticket ID.")
    body: str = Field(description="Comment body, plain text.")


_ItemDiscriminated = Annotated[
    _TicketCreate | _UserCreate | _OrgCreate | _CommentCreate,
    Field(discriminator="kind", description="Entity to create."),
]


class _WriteItemArgsDiscriminated(BaseModel):
    """Pydantic ``Annotated[..., Field(discriminator='kind')]`` shape —
    emits ``oneOf`` + ``discriminator`` keyword in the JSON schema."""

    table: Literal["tickets", "users", "organizations", "comments"]
    item: _ItemDiscriminated


class _WriteItemArgsBareUnion(BaseModel):
    """Bare ``Union[A, B, …]`` shape — Pydantic emits ``anyOf`` with
    inline branches and no ``discriminator`` keyword.

    Every production OTS tool that takes a union-typed payload uses
    this shape (e.g. ``zendesk_create_item.item: TicketCreate |
    UserCreate | OrganizationCreate | CommentCreate``).
    """

    table: Literal["tickets", "users", "organizations", "comments"]
    item: _TicketCreate | _UserCreate | _OrgCreate | _CommentCreate


def _build_write_item_tool(args_model: type[BaseModel]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "write_item",
            "description": (
                "Create one entity in the support system. Use `kind` to pick "
                "which entity type to create — ticket, user, org, or comment."
            ),
            "parameters": TypeAdapter(args_model).json_schema(),
        },
    }


_WRITE_ITEM_TOOLS: dict[str, dict[str, Any]] = {
    "explicit_discriminator": _build_write_item_tool(_WriteItemArgsDiscriminated),
    "bare_union": _build_write_item_tool(_WriteItemArgsBareUnion),
}


_NOOP_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "log_note",
        "description": "Log a free-form note (distractor — don't call unless asked).",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
}


def _decoded_args(raw: Any) -> dict[str, Any]:
    """Return tool-call arguments as a native dict.

    The harness's ``ResponsePolicy`` runs before this point — if the
    decoded ``arguments`` is still a ``str``, the model emitted a
    stringified blob and the recovery policy did not unstring it. That
    is the regression this test exists to catch, so we fail loudly
    rather than silently ``json.loads`` here.
    """
    if isinstance(raw, str):
        # Recovery didn't fire — surface the wire shape in the failure
        # message so the diagnosis is self-explanatory.
        pytest.fail(
            f"tool-call arguments arrived as a string instead of a dict; "
            f"response policy did not recover the JSON-coerced payload. "
            f"Wire shape: {raw[:300]!r}"
        )
    assert isinstance(
        raw, dict
    ), f"tool-call arguments must parse as a dict, got {type(raw).__name__}: {raw!r}"
    return raw


def _assert_item_dict(args: dict[str, Any], expected_kind: str) -> dict[str, Any]:
    """Cross-check ``args['item']`` is a native dict of the right variant.

    Returns the unwrapped ``item`` dict for variant-specific field checks.
    """
    item = args.get("item")
    if isinstance(item, str):
        pytest.fail(
            f"`item` was emitted as a JSON-encoded string instead of a "
            f"nested dict — exact wire shape of the eval failure. "
            f"Recovery policy did not decode it. String body: {item[:300]!r}"
        )
    assert isinstance(
        item, dict
    ), f"`item` must be a dict (discriminated union member), got {type(item).__name__}: {item!r}"
    assert item.get("kind") == expected_kind, (
        f"discriminator mismatch: expected kind={expected_kind!r}, "
        f"got {item.get('kind')!r}. Full item: {item!r}"
    )
    return item


@pytest.mark.parametrize("union_shape", sorted(_WRITE_ITEM_TOOLS.keys()), ids=lambda s: s)
@pytest.mark.parametrize("cert", ALL_MODELS, ids=lambda c: c.model_id)
def test_discriminated_union_tool_call_two_turns(
    cert: ModelCertificate,
    union_shape: str,
    live_client,
    skip_unless_capability_declared,
) -> None:
    """Two-turn flow exercising two different discriminated-union variants.

    Parametrised over the ``union_shape`` Pydantic emits:

    * ``explicit_discriminator`` — ``oneOf`` + ``discriminator`` keyword
      (``Annotated[A | B, Field(discriminator='kind')]``).
    * ``bare_union`` — ``anyOf`` with inline branches, no
      ``discriminator`` keyword (``A | B``). The production OTS shape.

    Both shapes MUST round-trip on the live provider for the model to
    pass ``DISCRIMINATED_UNION_TOOL_CALL`` — a model that handles only
    one is dishonest about the capability.

    Assertions per turn:
      1. ``tool_calls`` is non-empty and names ``write_item``.
      2. Decoded ``arguments`` is a native dict (not a stringified blob).
      3. ``arguments["item"]`` is a native dict (not a stringified blob).
      4. The dict's ``kind`` discriminator matches the variant the user
         turn asked for (``ticket`` on turn 1, ``comment`` on turn 2).
      5. The variant's signature fields are populated (e.g.
         ``subject`` + ``priority`` for ticket, ``ticket_id`` + ``body``
         for comment).
    """
    skip_unless_capability_declared(cert, Capability.DISCRIMINATED_UNION_TOOL_CALL)
    client = live_client(cert)

    write_item_tool = _WRITE_ITEM_TOOLS[union_shape]

    system = (
        "You are a support-system ops assistant. Prefer calling tools "
        "over prose. When the user asks to create something, you MUST "
        "call `write_item` with the right `table` and an `item` whose "
        "`kind` matches the entity type. Always pass `item` as a nested "
        "object — never as a JSON-encoded string."
    )

    # ----- Turn 1: create a ticket -----
    turn1_request = (
        "Open a high-priority ticket titled 'Cargo doors stuck on Truck-7' for the night shift."
    )
    result1 = client.generate(
        system=system,
        messages=[Message(role=MessageRole.USER, content=turn1_request)],
        tools=[write_item_tool, _NOOP_TOOL],
        tool_choice="auto",
    )
    assert (
        result1.tool_calls
    ), f"{cert.model_id}: turn 1 returned no tool call (text={result1.text[:200]!r})"
    tc1 = result1.tool_calls[0]
    assert (
        tc1.name == "write_item"
    ), f"{cert.model_id}: turn 1 picked wrong tool {tc1.name!r}; expected `write_item`"
    args1 = _decoded_args(tc1.arguments)
    assert args1.get("table") == "tickets", (
        f"{cert.model_id}: turn 1 wrote to wrong table "
        f"{args1.get('table')!r}; expected 'tickets'. Args: {args1!r}"
    )
    ticket = _assert_item_dict(args1, expected_kind="ticket")
    assert ticket.get(
        "subject"
    ), f"{cert.model_id}: turn 1 ticket missing `subject`. Item: {ticket!r}"
    assert ticket.get("priority") in {"high", "urgent"}, (
        f"{cert.model_id}: turn 1 ticket priority {ticket.get('priority')!r} "
        f"isn't high/urgent (the user said 'high-priority'). Item: {ticket!r}"
    )

    # ----- Synthesize a tool result and ask for a follow-up comment -----
    ticket_id = f"TCK-{uuid.uuid4().hex[:8].upper()}"
    tool_result = json.dumps({"ok": True, "id": ticket_id, "table": "tickets"})

    # The exact replay shape varies per provider; we hand the client
    # the same Message sequence that the runner would build.
    assistant_msg_content_for_turn2 = (
        f"Ticket created with id {ticket_id}."  # narrative — the tool reply is canonical.
    )

    # ----- Turn 2: add a comment -----
    turn2_request = (
        f"Now add an internal comment on ticket {ticket_id} that says "
        f"'Maintenance was paged; ETA 20 min.'"
    )
    result2 = client.generate(
        system=system,
        messages=[
            Message(role=MessageRole.USER, content=turn1_request),
            Message(
                role=MessageRole.ASSISTANT,
                content=assistant_msg_content_for_turn2,
            ),
            Message(role=MessageRole.USER, content=turn2_request),
            # Inline the synthesized tool outcome as a final user turn
            # so the model has the ticket id in context without
            # depending on provider-specific tool-message replay
            # semantics — the capability under test is the arguments
            # shape, not multi-turn role plumbing (covered by
            # MULTI_TURN_TOOL_USE).
            Message(
                role=MessageRole.USER,
                content=f"(System: prior tool call returned {tool_result})",
            ),
        ],
        tools=[write_item_tool, _NOOP_TOOL],
        tool_choice="auto",
    )
    assert (
        result2.tool_calls
    ), f"{cert.model_id}: turn 2 returned no tool call (text={result2.text[:200]!r})"
    tc2 = result2.tool_calls[0]
    assert (
        tc2.name == "write_item"
    ), f"{cert.model_id}: turn 2 picked wrong tool {tc2.name!r}; expected `write_item`"
    args2 = _decoded_args(tc2.arguments)
    assert args2.get("table") == "comments", (
        f"{cert.model_id}: turn 2 wrote to wrong table "
        f"{args2.get('table')!r}; expected 'comments'. Args: {args2!r}"
    )
    comment = _assert_item_dict(args2, expected_kind="comment")
    assert comment.get("ticket_id") == ticket_id, (
        f"{cert.model_id}: turn 2 comment.ticket_id "
        f"{comment.get('ticket_id')!r} doesn't match the ticket id "
        f"returned in turn 1 ({ticket_id!r}). Item: {comment!r}"
    )
    assert comment.get("body"), f"{cert.model_id}: turn 2 comment missing `body`. Item: {comment!r}"
