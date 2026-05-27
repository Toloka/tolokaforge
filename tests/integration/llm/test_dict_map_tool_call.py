"""Stage 8 capability test — :attr:`Capability.DICT_MAP_TOOL_CALL`.

Guards Stage 2 (P2): models declaring ``DICT_MAP_TOOL_CALL`` in
``required`` MUST emit typed ``Dict[str, T]`` tool-call arguments as a
native dict, not a stringified JSON blob or array. Pre-Stage-2 Qwen
fell through to the ``default`` preset and stringified ``lines``; now
the Qwen / Grok / GPT-5 presets all route through the same
:class:`~tolokaforge.core.llm.schema_sanitizer.StrictSchema` +
:class:`~tolokaforge.core.llm.response_policy.ArrayDictMapResponse` +
:class:`~tolokaforge.core.llm.prompt_policy.DictMapHints` trio (see
[`AGENTS.md`](../../../AGENTS.md) gotchas #11 + #13).

The tool list co-exercises Stage 1's Decimal collapse (``price`` field)
and Stage 2's dict-map array rewrite — the real tau_manufacturing
failure surface rolled into one round trip.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel, Field, TypeAdapter

from tolokaforge.core.models import Message, MessageRole

from ._capability import Capability, ModelCertificate
from .registry import ALL_MODELS


class _LineItem(BaseModel):
    sku: str = Field(description="Stock keeping unit.")
    qty: int = Field(description="Quantity ordered.", ge=0)
    price: Decimal = Field(description="Unit price in USD.")


class _AddOrderLines(BaseModel):
    order_id: str = Field(description="Order identifier.")
    lines: dict[str, _LineItem] = Field(description="Map of line-id -> line.")


class _Ping(BaseModel):
    message: str = Field(description="Free-form ping message.")


def _tool(name: str, description: str, model: type[BaseModel]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": TypeAdapter(model).json_schema(),
        },
    }


@pytest.mark.parametrize("cert", ALL_MODELS, ids=lambda c: c.model_id)
def test_dict_map_tool_call(
    cert: ModelCertificate,
    live_client,
    skip_unless_capability_declared,
) -> None:
    """``lines`` must round-trip as a native dict with structured values.

    Assertions:

    1. ``result.tool_calls`` is non-empty.
    2. ``arguments["lines"]`` is a ``dict``.
    3. At least one entry is a nested dict with ``qty`` and ``price``
       fields — proves the dict-map carries structured values and
       wasn't stringified to a JSON blob.
    """
    skip_unless_capability_declared(cert, Capability.DICT_MAP_TOOL_CALL)

    tools = [
        _tool("add_order_lines", "Add line items to an order.", _AddOrderLines),
        _tool("ping", "Log a free-form ping.", _Ping),
    ]
    client = live_client(cert)
    result = client.generate(
        system=(
            "You are an order-management assistant. Prefer calling tools over "
            "replying in prose. When the user asks to add multiple lines, you "
            "MUST issue a single `add_order_lines` tool call with every line "
            "included in the `lines` parameter."
        ),
        messages=[
            Message(
                role=MessageRole.USER,
                content=(
                    "Order PO-42: add two order lines: SKU-A at qty 10 price "
                    "$9.99, and SKU-B at qty 5 price $19.50."
                ),
            )
        ],
        tools=tools,
        tool_choice="auto",
    )

    assert result.tool_calls, f"{cert.model_id}: expected at least one tool call ({result!r})"

    first_args = result.tool_calls[0].arguments
    if isinstance(first_args, str):
        first_args = json.loads(first_args)
    assert isinstance(first_args, dict), (
        f"{cert.model_id}: tool-call arguments must parse as dict, got "
        f"{type(first_args).__name__}: {first_args!r}"
    )

    lines = first_args.get("lines")
    assert isinstance(lines, dict), (
        f"{cert.model_id}: after Stage 2 the response policy must convert "
        f"dict-map arguments back to a dict, got {type(lines).__name__}: {lines!r}"
    )
    assert lines, f"{cert.model_id}: 'lines' dict must be non-empty"

    structured = [v for v in lines.values() if isinstance(v, dict) and "qty" in v and "price" in v]
    assert structured, (
        f"{cert.model_id}: no dict-shaped entries with qty/price — model "
        f"likely stringified the dict-map. Full arguments: {first_args!r}"
    )
