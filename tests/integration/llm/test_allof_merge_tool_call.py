"""Capability test - :attr:`Capability.ALLOF_MERGE_TOOL_CALL`.

A parameter typed as an ``allOf`` composition must satisfy BOTH merged
subschemas at once. Models (and some schema sanitizers) historically
honour only one side of the merge, dropping fields contributed by the
other branch. This guards that a model declaring
``ALLOF_MERGE_TOOL_CALL`` populates fields from every subschema.

The ``allOf`` schema is injected directly (Pydantic does not emit a
bare ``allOf`` from a simple model); synthetic, no private content.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel, Field, TypeAdapter

from tolokaforge.core.models import Message, MessageRole

from ._capability import Capability, ModelCertificate
from .registry import ALL_MODELS


# A payload that must satisfy BOTH halves: {a: string} AND {b: integer}.
_ALLOF_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_payload",
        "description": (
            "Submit a payload. `payload` must satisfy BOTH parts of an allOf "
            "schema: it carries a string field `a` AND an integer field `b`."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "payload": {
                    "type": "object",
                    "allOf": [
                        {"properties": {"a": {"type": "string"}}, "required": ["a"]},
                        {"properties": {"b": {"type": "integer"}}, "required": ["b"]},
                    ],
                }
            },
            "required": ["payload"],
        },
    },
}


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
def test_allof_merge_tool_call(
    cert: ModelCertificate,
    live_client,
    skip_unless_capability_declared,
) -> None:
    """``payload`` must carry BOTH merged fields (``a`` str + ``b`` int).

    Assertions:

    1. ``result.tool_calls`` is non-empty.
    2. ``arguments["payload"]`` is a dict.
    3. It has a string ``a`` AND an integer ``b`` - proves both allOf
       branches were honoured, not just one.
    """
    skip_unless_capability_declared(cert, Capability.ALLOF_MERGE_TOOL_CALL)

    tools = [_ALLOF_TOOL, _tool("ping", "Log a free-form ping.", _Ping)]
    client = live_client(cert)
    result = client.generate(
        system=(
            "You are a payload-builder. Prefer calling tools over replying in "
            "prose. Issue a single `submit_payload` call with a complete payload."
        ),
        messages=[
            Message(
                role=MessageRole.USER,
                content=(
                    "Submit a payload where a is hello and b is 42. Then you're "
                    "done."
                ),
            )
        ],
        tools=tools,
        tool_choice="auto",
    )

    assert result.tool_calls, f"{cert.model_id}: expected at least one tool call ({result!r})"

    args = result.tool_calls[0].arguments
    if isinstance(args, str):
        args = json.loads(args)
    assert isinstance(args, dict), (
        f"{cert.model_id}: arguments must parse as dict, got {type(args).__name__}: {args!r}"
    )

    payload = args.get("payload")
    assert isinstance(payload, dict), (
        f"{cert.model_id}: `payload` must be a native dict, got {type(payload).__name__}: {payload!r}"
    )
    assert isinstance(payload.get("a"), str), (
        f"{cert.model_id}: allOf field `a` (string) missing/wrong - only one merge "
        f"branch honoured. Payload: {payload!r}"
    )
    assert isinstance(payload.get("b"), int) and not isinstance(payload.get("b"), bool), (
        f"{cert.model_id}: allOf field `b` (integer) missing/wrong - only one merge "
        f"branch honoured. Payload: {payload!r}"
    )
