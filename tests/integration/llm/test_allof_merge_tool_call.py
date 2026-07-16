"""Capability test - :attr:`Capability.ALLOF_MERGE_TOOL_CALL`.

A parameter typed as an ``allOf`` composition must satisfy BOTH (or all)
merged subschemas at once. Models (and some schema sanitizers)
historically honour only one side of the merge, dropping fields
contributed by the other branch. This guards that a model declaring
``ALLOF_MERGE_TOOL_CALL`` populates fields from every subschema.

Variance is built in: the merge is exercised at several structural
shapes (``shape`` axis) - a two-branch merge at top level, the same
merge nested inside an outer object, and a three-branch merge - because
a model that drops a branch may only do so at a particular arity or
nesting depth. This is our own test, so the shapes live here rather than
in a separate ``test_variant_`` file.

The ``allOf`` schema is injected directly (Pydantic does not emit a bare
``allOf`` from a simple model); synthetic, no private content.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import BaseModel, Field, TypeAdapter

from tolokaforge.core.models import Message, MessageRole

from ._capability import Capability, ModelCertificate
from .registry import ALL_MODELS


def _allof_payload_schema(branches: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "object", "allOf": branches}


_BRANCH_A = {"properties": {"a": {"type": "string"}}, "required": ["a"]}
_BRANCH_B = {"properties": {"b": {"type": "integer"}}, "required": ["b"]}
_BRANCH_C = {"properties": {"c": {"type": "boolean"}}, "required": ["c"]}


def _payload_tool(parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "submit_payload",
            "description": (
                "Submit a payload whose fields must satisfy EVERY part of an allOf "
                "schema (each branch contributes a required field)."
            ),
            "parameters": parameters,
        },
    }


# Two-branch allOf at the top level: {a: string} AND {b: integer}.
_TOP_LEVEL_TOOL = _payload_tool(
    {
        "type": "object",
        "properties": {"payload": _allof_payload_schema([_BRANCH_A, _BRANCH_B])},
        "required": ["payload"],
    }
)

# Same two-branch merge, but nested one object deeper (envelope.payload).
_NESTED_TOOL = _payload_tool(
    {
        "type": "object",
        "properties": {
            "envelope": {
                "type": "object",
                "properties": {"payload": _allof_payload_schema([_BRANCH_A, _BRANCH_B])},
                "required": ["payload"],
            }
        },
        "required": ["envelope"],
    }
)

# Three-branch merge: {a: string} AND {b: integer} AND {c: boolean}.
_THREE_WAY_TOOL = _payload_tool(
    {
        "type": "object",
        "properties": {"payload": _allof_payload_schema([_BRANCH_A, _BRANCH_B, _BRANCH_C])},
        "required": ["payload"],
    }
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


_PING_TOOL = _tool("ping", "Log a free-form ping.", _Ping)

_SYS_BUILDER = (
    "You are a payload-builder. Prefer calling tools over replying in prose. "
    "Issue a single `submit_payload` call with a complete payload."
)


def _assert_ab(payload: Any) -> None:
    assert isinstance(
        payload, dict
    ), f"`payload` must be a native dict, got {type(payload).__name__}: {payload!r}"
    assert isinstance(
        payload.get("a"), str
    ), f"allOf field `a` (string) missing/wrong - a merge branch was dropped: {payload!r}"
    assert isinstance(payload.get("b"), int) and not isinstance(
        payload.get("b"), bool
    ), f"allOf field `b` (integer) missing/wrong - a merge branch was dropped: {payload!r}"


def _check_top_level(args: dict[str, Any]) -> None:
    _assert_ab(args.get("payload"))


def _check_nested(args: dict[str, Any]) -> None:
    envelope = args.get("envelope")
    assert isinstance(
        envelope, dict
    ), f"`envelope` must be a native dict, got {type(envelope).__name__}: {envelope!r}"
    _assert_ab(envelope.get("payload"))


def _check_three_way(args: dict[str, Any]) -> None:
    payload = args.get("payload")
    _assert_ab(payload)
    assert isinstance(
        payload.get("c"), bool
    ), f"allOf field `c` (boolean) missing/wrong - the third merge branch was dropped: {payload!r}"


@dataclass(frozen=True)
class _Shape:
    """One structural shape of the allOf-merge probe."""

    shape_id: str
    tool: dict[str, Any]
    user: str
    check: Callable[[dict[str, Any]], None]


_SHAPES: list[_Shape] = [
    _Shape(
        shape_id="top_level",
        tool=_TOP_LEVEL_TOOL,
        user="Submit a payload where a is hello and b is 42. Then you're done.",
        check=_check_top_level,
    ),
    _Shape(
        shape_id="nested",
        tool=_NESTED_TOOL,
        user=(
            "Submit an envelope whose payload has a is hello and b is 42. "
            "Put the payload in the envelope's `payload` field. Then you're done."
        ),
        check=_check_nested,
    ),
    _Shape(
        shape_id="three_way",
        tool=_THREE_WAY_TOOL,
        user="Submit a payload where a is hello, b is 42, and c is true. Then you're done.",
        check=_check_three_way,
    ),
]


@pytest.mark.parametrize("cert", ALL_MODELS, ids=lambda c: c.model_id)
@pytest.mark.parametrize("shape", _SHAPES, ids=lambda s: s.shape_id)
def test_allof_merge_tool_call(
    cert: ModelCertificate,
    shape: _Shape,
    live_client,
    skip_unless_capability_declared,
) -> None:
    """Payload must carry the fields from EVERY allOf branch, not just one.

    Same contract at every ``shape``: a non-empty tool call whose arguments are a
    native dict, whose payload populates all merged fields (proving no branch was
    silently dropped by the model or a schema sanitizer).
    """
    skip_unless_capability_declared(cert, Capability.ALLOF_MERGE_TOOL_CALL)

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
