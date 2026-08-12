"""Capability test - :attr:`Capability.HETEROGENEOUS_ARRAY_TOOL_CALL`.

A list whose elements are a discriminated union (text block | image
block) compiles to an array of ``oneOf``/``anyOf`` branches. Models
historically flatten such arrays to a single variant, drop the
discriminator, or stringify the elements. This guards that a model
declaring ``HETEROGENEOUS_ARRAY_TOOL_CALL`` emits the array with the
correct per-element branch.

Variance is built in: the polymorphic array is exercised at several
structural shapes (``shape`` axis) - flat at top level, nested inside a
wrapper object, and a longer alternating run - because a mis-shape that
only fires when the array is nested (the MiniMax-M3 pattern) is invisible
to a single top-level probe. This is our own test, so the shapes live
here rather than in a separate ``test_variant_`` file.

Synthetic; derived from the JSON Schema Test Suite polymorphic-array
surface, no private content.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Union

import pytest
from pydantic import BaseModel, Field, TypeAdapter

from tolokaforge.core.models import Message, MessageRole
from tolokaforge.testing.certify import ALL_MODELS, Capability, ModelCertificate


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


class _Message(BaseModel):
    title: str = Field(description="Message title.")
    blocks: list[_Block] = Field(
        min_length=1, description="Polymorphic array of text|image blocks, in order."
    )


class _SubmitMessage(BaseModel):
    message: _Message = Field(description="A message whose `blocks` is a polymorphic array.")


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


_BLOCKS_TOOL = _tool("submit_blocks", "Submit an ordered list of content blocks.", _SubmitBlocks)
_MESSAGE_TOOL = _tool(
    "submit_message",
    "Submit a message containing an ordered list of content blocks.",
    _SubmitMessage,
)
_PING_TOOL = _tool("ping", "Log a free-form ping.", _Ping)

_SYS_BUILDER = (
    "You are a content-builder. Prefer calling tools over replying in prose. "
    "Issue a single tool call with every block included, in order."
)


def _assert_mixed_blocks(blocks: Any) -> None:
    assert (
        isinstance(blocks, list) and blocks
    ), f"`blocks` must be a non-empty list, got {type(blocks).__name__}: {blocks!r}"
    assert all(
        isinstance(b, dict) for b in blocks
    ), f"every block must be a native dict (not stringified). Got: {blocks!r}"
    kinds = {b.get("kind") for b in blocks}
    assert {"text", "image"} <= kinds, (
        f"expected both a text and an image element; the polymorphic array was "
        f"flattened to {kinds}. Full blocks: {blocks!r}"
    )


def _check_flat(args: dict[str, Any]) -> None:
    _assert_mixed_blocks(args.get("blocks"))


def _check_nested_in_object(args: dict[str, Any]) -> None:
    message = args.get("message")
    assert isinstance(
        message, dict
    ), f"`message` must be a native dict, got {type(message).__name__}: {message!r}"
    _assert_mixed_blocks(message.get("blocks"))


def _check_long_alternating(args: dict[str, Any]) -> None:
    blocks = args.get("blocks")
    _assert_mixed_blocks(blocks)
    assert len(blocks) >= 4, f"expected a longer run (>=4 blocks), got {len(blocks)}: {blocks!r}"


@dataclass(frozen=True)
class _Shape:
    """One structural shape of the heterogeneous-array probe."""

    shape_id: str
    tool: dict[str, Any]
    user: str
    check: Callable[[dict[str, Any]], None]


_SHAPES: list[_Shape] = [
    _Shape(
        shape_id="flat",
        tool=_BLOCKS_TOOL,
        user=(
            "Submit three content blocks in order: first a text block that says "
            "hello world, then an image block whose url is http://example.com/a.png, "
            "then a text block that says the end. Then you're done."
        ),
        check=_check_flat,
    ),
    _Shape(
        shape_id="nested_in_object",
        tool=_MESSAGE_TOOL,
        user=(
            "Submit a message titled 'Report'. Its blocks, in order: a text block "
            "saying intro, then an image block whose url is http://example.com/b.png, "
            "then a text block saying outro. Put them in the message's `blocks` field."
        ),
        check=_check_nested_in_object,
    ),
    _Shape(
        shape_id="long_alternating",
        tool=_BLOCKS_TOOL,
        user=(
            "Submit five content blocks in order: a text block 'one', an image block "
            "http://example.com/1.png, a text block 'two', an image block "
            "http://example.com/2.png, and a text block 'three'. Then you're done."
        ),
        check=_check_long_alternating,
    ),
]


@pytest.mark.parametrize("cert", ALL_MODELS, ids=lambda c: c.model_id)
@pytest.mark.parametrize("shape", _SHAPES, ids=lambda s: s.shape_id)
def test_heterogeneous_array_tool_call(
    cert: ModelCertificate,
    shape: _Shape,
    live_client,
    skip_unless_capability_declared,
) -> None:
    """Polymorphic array must round-trip as native dicts with per-element ``kind``.

    Same contract at every ``shape``: a non-empty tool call whose arguments are a
    native dict, whose blocks list carries both a ``text`` and an ``image`` element
    (proving the array was not flattened to one variant nor stringified).
    """
    skip_unless_capability_declared(cert, Capability.HETEROGENEOUS_ARRAY_TOOL_CALL)

    client = live_client(cert)
    result = client.generate(
        system=_SYS_BUILDER,
        messages=[Message(role=MessageRole.USER, content=shape.user)],
        tools=[shape.tool, _PING_TOOL],
        tool_choice="auto",
    )

    assert (
        result.tool_calls
    ), f"{cert.model_id}/{shape.shape_id}: expected at least one tool call ({result!r})"
    args = result.tool_calls[0].arguments
    if isinstance(args, str):
        args = json.loads(args)
    assert isinstance(args, dict), (
        f"{cert.model_id}/{shape.shape_id}: arguments must parse as dict, "
        f"got {type(args).__name__}: {args!r}"
    )
    shape.check(args)
