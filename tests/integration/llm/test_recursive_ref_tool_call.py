"""Capability test - :attr:`Capability.RECURSIVE_REF_TOOL_CALL`.

A tool whose argument is a recursive self-referential object (a node
whose ``children`` are themselves nodes) compiles to a recursive
``$ref`` cycle in JSON-Schema. Models that special-case flat shapes
tend to either stringify the subtree, flatten it to one level, or
truncate the recursion. This guards that a model declaring
``RECURSIVE_REF_TOOL_CALL`` emits the tree as a native nested dict.

Derived from the JSON Schema Test Suite recursive-``$ref`` surface;
synthetic, no private content.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel, Field, TypeAdapter

from tolokaforge.core.models import Message, MessageRole

from ._capability import Capability, ModelCertificate
from .registry import ALL_MODELS


class _TreeNode(BaseModel):
    label: str = Field(description="Node label.")
    children: list[_TreeNode] = Field(default_factory=list, description="Child nodes (recursive).")


_TreeNode.model_rebuild()


class _SubmitTree(BaseModel):
    root: _TreeNode = Field(description="Root node (recursive: children are nodes).")


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
def test_recursive_ref_tool_call(
    cert: ModelCertificate,
    live_client,
    skip_unless_capability_declared,
) -> None:
    """``root`` must round-trip as a native nested dict with a recursive
    ``children`` list - not a stringified blob nor a flattened tree.

    Assertions:

    1. ``result.tool_calls`` is non-empty.
    2. ``arguments["root"]`` is a dict with ``label`` + ``children``.
    3. ``children`` is a list, and at least one grandchild exists at
       depth >= 2 (proves the recursion was not flattened/truncated).
    """
    skip_unless_capability_declared(cert, Capability.RECURSIVE_REF_TOOL_CALL)

    tools = [
        _tool("submit_tree", "Submit a tree of labelled nodes.", _SubmitTree),
        _tool("ping", "Log a free-form ping.", _Ping),
    ]
    client = live_client(cert)
    result = client.generate(
        system=(
            "You are a structure-builder. Prefer calling tools over replying in "
            "prose. When asked to submit a tree, issue a single `submit_tree` "
            "call whose `root` carries the full nested structure."
        ),
        messages=[
            Message(
                role=MessageRole.USER,
                content=(
                    "Submit a tree where the root node is labelled A. A has two "
                    "children, labelled B and C. B itself has one child, labelled "
                    "D. Then you're done."
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

    root = args.get("root")
    assert isinstance(root, dict), (
        f"{cert.model_id}: `root` must be a native dict (recursive node), got "
        f"{type(root).__name__}: {root!r}"
    )
    assert (
        "label" in root and "children" in root
    ), f"{cert.model_id}: `root` missing label/children. Got: {root!r}"
    children = root.get("children")
    assert (
        isinstance(children, list) and children
    ), f"{cert.model_id}: `root.children` must be a non-empty list, got {children!r}"
    grandchildren = [
        gc
        for ch in children
        if isinstance(ch, dict)
        for gc in (ch.get("children") or [])
        if isinstance(gc, dict)
    ]
    assert grandchildren, (
        f"{cert.model_id}: no depth>=2 node - recursion was flattened/truncated. "
        f"Full root: {root!r}"
    )
