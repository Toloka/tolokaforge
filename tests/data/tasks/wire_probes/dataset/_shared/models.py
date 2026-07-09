"""Models for the wire-probe merged domain (complex multi-step shapes).

This shared domain hosts the higher-complexity, multi-step derived cases:
- a tau2-bench-derived telecom account-resolution flow (nested subscriber,
  discriminated payment-method union, Decimal amounts);
- an API-Bank-derived tool-registry meta-flow (a device dict-map);
- hand-authored neutral ``records`` shapes (typed dict-map with a key-packing
  bait, discriminated union, deep nesting, recursion, heterogeneous array).

All shapes are derived/rewritten from public benchmarks (tau2-bench, API-Bank)
or hand-authored; entities are renamed and no policy is carried. The shapes
drive the codec behaviour; no policy or answer key is present. Non-scoring.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

# --- telecom (tau2-bench-derived) shapes ------------------------------------


class Address(BaseModel):
    """Nested address on a subscriber profile (deep_nesting)."""

    street: Annotated[str, Field(description="Street line.", examples=["12 Maple Ave"])]
    city: Annotated[str, Field(description="City.", examples=["Riverton"])]
    postal_code: Annotated[str, Field(description="Postal code.", examples=["90210"])]
    country: Annotated[str, Field(description="ISO country code.", examples=["US"])]


class CardMethod(BaseModel):
    """``card`` branch of the PaymentMethod discriminated union."""

    kind: Literal["card"]
    last_four: Annotated[str, Field(description="Last four digits of the card.", examples=["4242"])]
    expiry: Annotated[str, Field(description="Expiry as MM/YY.", examples=["08/27"])]


class VoucherMethod(BaseModel):
    """``voucher`` branch: carries a Decimal balance (decimal_anyOf)."""

    kind: Literal["voucher"]
    code: Annotated[str, Field(description="Voucher code.", examples=["SAVE10"])]
    balance: Annotated[Decimal, Field(description="Remaining voucher balance.", examples=["25.00"])]


class BankMethod(BaseModel):
    """``bank`` branch of the PaymentMethod discriminated union."""

    kind: Literal["bank"]
    iban: Annotated[str, Field(description="IBAN.", examples=["DE89370400440532013000"])]


# Discriminated union on ``kind`` -> oneOf + discriminator + $defs/$ref.
PaymentMethod = Annotated[Union[CardMethod, VoucherMethod, BankMethod], Field(discriminator="kind")]


class LineItem(BaseModel):
    """Invoice line item: nested object + Decimal amount."""

    description: Annotated[str, Field(description="What the charge is for.")]
    amount: Annotated[Decimal, Field(description="Charge amount.", examples=["19.99"])]


# --- registry (API-Bank-derived) shapes -------------------------------------


class DeviceSpec(BaseModel):
    """Value half of the ``devices`` dict-map."""

    device_type: Annotated[Literal["phone", "laptop", "tablet"], Field(description="Device type.")]
    label: Annotated[
        str, Field(description="Human-readable device label.", examples=["work-phone"])
    ]


# --- records sandbox shapes (merged; Address->OrderAddress, LineItem->RecordLine) ---


class EntryValue(BaseModel):
    """Value half of a typed dict-map ``key -> {amount}``."""

    amount: Annotated[int, Field(ge=0, description="Amount for this entry.", examples=[5])]


class RecordLine(BaseModel):
    """Multi-field dict-map value (qty + Decimal price).

    A model that mishandles ``Dict[str, T]`` with a multi-field value may pack
    the fields INTO the key string (e.g. ``"SKU-A|qty=10|price=9.99"``) instead
    of a native ``{"SKU-A": {"qty": 10, "unit_price": "9.99"}}`` map.
    """

    qty: Annotated[int, Field(ge=0, description="Quantity.", examples=[10])]
    unit_price: Annotated[Decimal, Field(ge=0, description="Unit price.", examples=["9.99"])]


class NoteItem(BaseModel):
    """``note`` branch of the discriminated union."""

    kind: Literal["note"]
    title: Annotated[str, Field(description="Note title.")]
    body: Annotated[str, Field(description="Note body.")]


class TaskItem(BaseModel):
    """``task`` branch of the discriminated union."""

    kind: Literal["task"]
    title: Annotated[str, Field(description="Task title.")]
    due: Annotated[str, Field(description="Due date, YYYY-MM-DD.")]


# Discriminated union on ``kind`` -> emits oneOf + discriminator + $defs/$ref,
# i.e. the gemini-dialect / discriminated-union / nested-$ref surface at once.
Item = Annotated[Union[NoteItem, TaskItem], Field(discriminator="kind")]


# --- Deeply-nested object (multi-level $ref + array-of-objects) for the
# nesting-depth serialization-stress probe. ---


class OrderAddress(BaseModel):
    street: Annotated[str, Field(description="Street.")]
    city: Annotated[str, Field(description="City.")]
    country: Annotated[str, Field(description="ISO country code.", examples=["HU"])]


class Customer(BaseModel):
    name: Annotated[str, Field(description="Customer name.")]
    address: Annotated[OrderAddress, Field(description="Postal address (nested).")]


class OrderItem(BaseModel):
    sku: Annotated[str, Field(description="SKU.", examples=["SKU-1"])]
    qty: Annotated[int, Field(ge=1, description="Quantity.")]


class DeepOrder(BaseModel):
    """4-level nested object: DeepOrder -> Customer -> OrderAddress, plus an
    array-of-objects (items) and a free-text field."""

    order_id: Annotated[str, Field(description="Order id.", examples=["ORD-9001"])]
    customer: Annotated[Customer, Field(description="Customer (nested object).")]
    items: Annotated[
        list[OrderItem], Field(min_length=1, description="Line items (array of objects).")
    ]
    note: Annotated[str, Field(description="Free-text order note.")]


# --- Recursive / self-referential schema (emits a recursive $ref) ---


class TreeNode(BaseModel):
    """A node whose children are TreeNodes -> a recursive $ref cycle."""

    label: Annotated[str, Field(description="Node label.")]
    children: Annotated[
        list[TreeNode], Field(default_factory=list, description="Child nodes (recursive).")
    ]


TreeNode.model_rebuild()


# --- Heterogeneous / polymorphic array element (list of differing item types) ---


class TextBlock(BaseModel):
    kind: Literal["text"]
    text: Annotated[str, Field(description="Text content.")]


class ImageBlock(BaseModel):
    kind: Literal["image"]
    url: Annotated[str, Field(description="Image URL.")]


# A list[Block] is a polymorphic array: each element is text OR image.
Block = Annotated[Union[TextBlock, ImageBlock], Field(discriminator="kind")]
