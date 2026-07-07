"""Structural VARIANTS of the discriminated-union capability probe (observe stage).

``test_discriminated_union_tool_call_two_turns`` is a LEGACY test we do
not own; it already parametrises the explicit-vs-bare union SHAPE over two
turns. Rather than edit it, this parallel variant suite stresses the SAME
``DISCRIMINATED_UNION_TOOL_CALL`` capability at different structural
PLACEMENTS - an array of union members, a union nested inside an object,
and a union as a dict-map value - because a model that special-cases a
single top-level union member may still stringify or flatten it once it is
repeated in an array or buried a level deeper.

Self-contained (its own schema + prompt + structural check per variant),
living in the canonical package with a ``test_variant_`` filename prefix.
Gated by the cert AND ``TF_RUN_VARIANTS`` so a normal CI run skips it; the
auto-integration observe workflow runs it (sets the flag + injects the
all-required candidate cert) into a separate junit dir. The ``ShapeVariant``
helper is kept local here; if a second variant file needs it, lift it into a
shared module then, not before.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import pytest
from pydantic import BaseModel, Field, TypeAdapter

from tolokaforge.core.models import Message, MessageRole

from ._capability import Capability, ModelCertificate
from .registry import ALL_MODELS


class _TicketCreate(BaseModel):
    kind: Literal["ticket"]
    subject: str = Field(description="Short ticket title.")
    priority: Literal["low", "normal", "high", "urgent"] = Field(description="Priority bucket.")


class _UserCreate(BaseModel):
    kind: Literal["user"]
    email: str = Field(description="User email.")
    name: str = Field(description="Display name.")


class _CommentCreate(BaseModel):
    kind: Literal["comment"]
    ticket_id: str = Field(description="Parent ticket ID.")
    body: str = Field(description="Comment body, plain text.")


# Bare union (production OTS shape: anyOf with inline branches, no discriminator keyword).
_Item = _TicketCreate | _UserCreate | _CommentCreate


class _SubmitBatch(BaseModel):
    """An array of discriminated-union members."""

    items: list[_Item] = Field(min_length=1, description="Entities to create, each a union member.")


class _Envelope(BaseModel):
    table: Literal["tickets", "users", "comments"]
    item: _Item = Field(description="A single union member nested in the envelope.")


class _SubmitEnvelope(BaseModel):
    envelope: _Envelope = Field(description="An envelope whose `item` is a discriminated union.")


class _SubmitItemMap(BaseModel):
    """A dict-map whose values are discriminated-union members."""

    items: dict[str, _Item] = Field(description="Map of id -> union member to create.")


def _tool(name: str, description: str, model: type[BaseModel]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": TypeAdapter(model).json_schema(),
        },
    }


_BATCH_TOOL = _tool(
    "submit_batch",
    "Create several entities at once; each item picks its type via `kind`.",
    _SubmitBatch,
)
_ENVELOPE_TOOL = _tool(
    "submit_envelope",
    "Create one entity wrapped in an envelope; `item.kind` picks the type.",
    _SubmitEnvelope,
)
_ITEM_MAP_TOOL = _tool(
    "submit_item_map",
    "Create entities from a map; each value picks its type via `kind`.",
    _SubmitItemMap,
)

_SYS_BUILDER = (
    "You are a support-system ops assistant. Prefer calling tools over prose. When "
    "asked to create entities, call the tool and pass each entity as a nested object "
    "whose `kind` matches its type - never as a JSON-encoded string."
)


def _assert_union_dict(item: Any, expected_kind: str) -> None:
    assert isinstance(
        item, dict
    ), f"union member must be a native dict (not a stringified blob), got {type(item).__name__}: {item!r}"
    assert item.get("kind") == expected_kind, (
        f"discriminator mismatch: expected kind={expected_kind!r}, got {item.get('kind')!r}. "
        f"Item: {item!r}"
    )


def _check_array_of_unions(args: dict[str, Any]) -> None:
    items = args.get("items")
    assert isinstance(items, list) and items, f"`items` must be a non-empty list, got {items!r}"
    assert all(isinstance(it, dict) for it in items), f"every item must be a native dict: {items!r}"
    kinds = {it.get("kind") for it in items}
    assert {
        "ticket",
        "user",
    } <= kinds, (
        f"expected both a ticket and a user member; array flattened to {kinds}. Items: {items!r}"
    )


def _check_nested_union(args: dict[str, Any]) -> None:
    envelope = args.get("envelope")
    assert isinstance(
        envelope, dict
    ), f"`envelope` must be a native dict, got {type(envelope).__name__}: {envelope!r}"
    _assert_union_dict(envelope.get("item"), expected_kind="ticket")


def _check_union_in_dict_map(args: dict[str, Any]) -> None:
    items = args.get("items")
    assert isinstance(items, dict) and items, f"`items` must be a non-empty dict-map, got {items!r}"
    kinds = {v.get("kind") for v in items.values() if isinstance(v, dict)}
    assert {
        "ticket",
        "comment",
    } <= kinds, f"expected a ticket and a comment value; map flattened to {kinds}. Items: {items!r}"


@dataclass(frozen=True)
class ShapeVariant:
    """One structural placement of a discriminated-union capability probe."""

    variant_id: str
    tool: dict[str, Any]
    user: str
    check: Callable[[dict[str, Any]], None]


_VARIANTS: list[ShapeVariant] = [
    ShapeVariant(
        variant_id="array_of_unions",
        tool=_BATCH_TOOL,
        user=(
            "Create two entities in one call: a high-priority ticket titled "
            "'Cargo doors stuck', and a user named Dana Lee with email dana@example.com. "
            "Put both in the `items` array."
        ),
        check=_check_array_of_unions,
    ),
    ShapeVariant(
        variant_id="nested_union",
        tool=_ENVELOPE_TOOL,
        user=(
            "Open a high-priority ticket titled 'Night shift handover' on the tickets "
            "table. Put the ticket in the envelope's `item` field."
        ),
        check=_check_nested_union,
    ),
    ShapeVariant(
        variant_id="union_in_dict_map",
        tool=_ITEM_MAP_TOOL,
        user=(
            "Create two entities keyed by id: key 't1' is a normal-priority ticket "
            "titled 'Restock', and key 'c1' is a comment on ticket TCK-1 with body "
            "'Ordered more'. Put both in the `items` map."
        ),
        check=_check_union_in_dict_map,
    ),
]


@pytest.mark.parametrize("cert", ALL_MODELS, ids=lambda c: c.model_id)
@pytest.mark.parametrize("variant", _VARIANTS, ids=lambda v: f"discriminated_union__{v.variant_id}")
def test_variant_discriminated_union(
    cert: ModelCertificate,
    variant: ShapeVariant,
    live_client,
    skip_unless_capability_declared,
) -> None:
    """Stress a discriminated union at one structural placement.

    Same contract as the canonical probe: a non-empty tool call whose arguments are
    a native dict, then a variant-specific check that each union member round-tripped
    as a native dict with the right ``kind`` discriminator rather than a stringified blob.
    """
    if not os.getenv("TF_RUN_VARIANTS"):
        pytest.skip("shape-variant suite runs only in the observe stage (set TF_RUN_VARIANTS)")
    skip_unless_capability_declared(cert, Capability.DISCRIMINATED_UNION_TOOL_CALL)

    client = live_client(cert)
    result = client.generate(
        system=_SYS_BUILDER,
        messages=[Message(role=MessageRole.USER, content=variant.user)],
        tools=[variant.tool],
        tool_choice="auto",
    )

    assert (
        result.tool_calls
    ), f"{cert.model_id}/{variant.variant_id}: expected at least one tool call ({result!r})"
    args = result.tool_calls[0].arguments
    if isinstance(args, str):
        args = json.loads(args)
    assert isinstance(args, dict), (
        f"{cert.model_id}/{variant.variant_id}: arguments must parse as dict, "
        f"got {type(args).__name__}: {args!r}"
    )
    variant.check(args)
