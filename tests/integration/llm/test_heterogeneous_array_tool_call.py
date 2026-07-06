"""Capability test - :attr:`Capability.HETEROGENEOUS_ARRAY_TOOL_CALL`.

A list whose elements are a discriminated union (text block | image
block) compiles to an array of ``oneOf``/``anyOf`` branches. Models
historically flatten such arrays to a single variant, drop the
discriminator, or stringify the elements. This guards that a model
declaring ``HETEROGENEOUS_ARRAY_TOOL_CALL`` emits the array with the
correct per-element branch.

Synthetic; derived from the JSON Schema Test Suite polymorphic-array
surface, no private content.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, Union

import pytest
from pydantic import BaseModel, Field, TypeAdapter

from tolokaforge.core.models import Message, MessageRole

from ._capability import Capability, ModelCertificate
from .registry import ALL_MODELS


class _TextBlock(BaseModel):
    kind: Literal["text"]
    text: str = Field(description="Text content.")


class _ImageBlock(BaseModel):
    kind: Literal["image"]
    url: str = Field(description="Image URL.")


_Block = Annotated[Union[_TextBlock, _ImageBlock], Field(discriminator="kind")]


class _SubmitBlocks(BaseModel):
    blocks: list[_Block] = Field(
        min_length=1, description="Polymorphic array of text|image blocks, in order."
    )


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
def test_heterogeneous_array_tool_call(
    cert: ModelCertificate,
    live_client,
    skip_unless_capability_declared,
) -> None:
    """``blocks`` must round-trip as a list of native dicts with the
    correct per-element ``kind`` (text vs image).

    Assertions:

    1. ``result.tool_calls`` is non-empty.
    2. ``arguments["blocks"]`` is a list of dicts (not stringified).
    3. Both a ``text`` element and an ``image`` element are present -
       proves the array carried >1 variant and was not flattened.
    """
    skip_unless_capability_declared(cert, Capability.HETEROGENEOUS_ARRAY_TOOL_CALL)

    tools = [
        _tool("submit_blocks", "Submit an ordered list of content blocks.", _SubmitBlocks),
        _tool("ping", "Log a free-form ping.", _Ping),
    ]
    client = live_client(cert)
    result = client.generate(
        system=(
            "You are a content-builder. Prefer calling tools over replying in "
            "prose. When asked to submit blocks, issue a single `submit_blocks` "
            "call with every block included, in order."
        ),
        messages=[
            Message(
                role=MessageRole.USER,
                content=(
                    "Submit three content blocks in order: first a text block "
                    "that says hello world, then an image block whose url is "
                    "http://example.com/a.png, then a text block that says the "
                    "end. Then you're done."
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
    assert isinstance(
        args, dict
    ), f"{cert.model_id}: arguments must parse as dict, got {type(args).__name__}: {args!r}"

    blocks = args.get("blocks")
    assert (
        isinstance(blocks, list) and blocks
    ), f"{cert.model_id}: `blocks` must be a non-empty list, got {type(blocks).__name__}: {blocks!r}"
    assert all(
        isinstance(b, dict) for b in blocks
    ), f"{cert.model_id}: every block must be a native dict (not stringified). Got: {blocks!r}"
    kinds = {b.get("kind") for b in blocks}
    assert {"text", "image"} <= kinds, (
        f"{cert.model_id}: expected both a text and an image element; the "
        f"polymorphic array was flattened to {kinds}. Full blocks: {blocks!r}"
    )
