"""Capability test - :attr:`Capability.RECURSIVE_REF_TOOL_CALL`.

A tool whose argument is a recursive self-referential object (a node
whose ``children`` are themselves nodes) compiles to a recursive
``$ref`` cycle in JSON-Schema. Models that special-case flat shapes
tend to either stringify the subtree, flatten it to one level, or
truncate the recursion. This guards that a model declaring
``RECURSIVE_REF_TOOL_CALL`` emits the tree as a native nested dict.

Variance is built in: the same capability is exercised at several
structural shapes (``shape`` parametrize axis) rather than one, because
shape-dependent mis-shapes are intermittent and a single top-level probe
misses them. Empirical motivation - the MiniMax-M3 ``{"item": [...]}``
artifact was worse when the construct was NESTED inside another object
than at top level, so ``nested_in_object`` earns its keep. This is our
own test, so the shapes live here rather than in a separate
``test_variant_`` file.

Derived from the JSON Schema Test Suite recursive-``$ref`` surface;
synthetic, no private content.
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


class _TreeNode(BaseModel):
    label: str = Field(description="Node label.")
    children: list[_TreeNode] = Field(default_factory=list, description="Child nodes (recursive).")


_TreeNode.model_rebuild()


class _SubmitTree(BaseModel):
    root: _TreeNode = Field(description="Root node (recursive: children are nodes).")


class _Document(BaseModel):
    title: str = Field(description="Document title.")
    tree: _TreeNode = Field(description="A labelled tree nested inside the document.")


class _SubmitDocument(BaseModel):
    doc: _Document = Field(description="A document whose `tree` field is a recursive node.")


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


_TREE_TOOL = _tool("submit_tree", "Submit a tree of labelled nodes.", _SubmitTree)
_DOC_TOOL = _tool(
    "submit_document", "Submit a document containing a labelled tree.", _SubmitDocument
)
_PING_TOOL = _tool("ping", "Log a free-form ping.", _Ping)

_SYS_BUILDER = (
    "You are a structure-builder. Prefer calling tools over replying in prose. "
    "Issue a single tool call whose arguments carry the full nested structure "
    "exactly as described."
)


def _max_depth(node: Any, depth: int = 1) -> int:
    if not isinstance(node, dict):
        return 0
    kids = node.get("children")
    if not isinstance(kids, list) or not kids:
        return depth
    return max((_max_depth(k, depth + 1) for k in kids if isinstance(k, dict)), default=depth)


def _check_simple(args: dict[str, Any]) -> None:
    """Baseline: root with children, at least one grandchild (depth >= 2)."""
    root = args.get("root")
    assert isinstance(
        root, dict
    ), f"`root` must be a native dict, got {type(root).__name__}: {root!r}"
    assert "label" in root and "children" in root, f"`root` missing label/children: {root!r}"
    children = root.get("children")
    assert (
        isinstance(children, list) and children
    ), f"`root.children` must be non-empty: {children!r}"
    assert _max_depth(root) >= 2, f"no depth>=2 node - recursion flattened/truncated: {root!r}"


def _check_deep_chain(args: dict[str, Any]) -> None:
    root = args.get("root")
    assert isinstance(root, dict) and "children" in root, f"`root` not a recursive node: {root!r}"
    depth = _max_depth(root)
    assert depth >= 3, f"recursion flattened/truncated: max depth {depth} < 3. root={root!r}"


def _check_wide_tree(args: dict[str, Any]) -> None:
    root = args.get("root")
    assert isinstance(root, dict), f"`root` not a dict: {root!r}"
    kids = root.get("children")
    assert isinstance(kids, list) and len(kids) >= 4, f"expected >=4 children, got {kids!r}"
    grandchildren = [
        gc
        for k in kids
        if isinstance(k, dict)
        for gc in (k.get("children") or [])
        if isinstance(gc, dict)
    ]
    assert grandchildren, f"breadth kept but depth lost (no grandchild): {root!r}"


def _check_nested_in_object(args: dict[str, Any]) -> None:
    doc = args.get("doc")
    assert isinstance(doc, dict), f"`doc` must be a native dict, got {type(doc).__name__}: {doc!r}"
    tree = doc.get("tree")
    assert (
        isinstance(tree, dict) and "children" in tree
    ), f"`doc.tree` must be a nested recursive node, got {tree!r}"
    depth = _max_depth(tree)
    assert depth >= 2, f"nested tree flattened: max depth {depth} < 2. doc={doc!r}"


@dataclass(frozen=True)
class _Shape:
    """One structural shape of the recursive-ref probe."""

    shape_id: str
    tool: dict[str, Any]
    user: str
    check: Callable[[dict[str, Any]], None]


_SHAPES: list[_Shape] = [
    _Shape(
        shape_id="simple",
        tool=_TREE_TOOL,
        user=(
            "Submit a tree where the root node is labelled A. A has two children, "
            "labelled B and C. B itself has one child, labelled D. Then you're done."
        ),
        check=_check_simple,
    ),
    _Shape(
        shape_id="deep_chain",
        tool=_TREE_TOOL,
        user=(
            "Submit a tree that is a single deep chain: root A has one child B; "
            "B has one child C; C has one child D. No other nodes."
        ),
        check=_check_deep_chain,
    ),
    _Shape(
        shape_id="wide_tree",
        tool=_TREE_TOOL,
        user=(
            "Submit a tree where root R has five children: A, B, C, D, E. "
            "A has one child A1, and B has one child B1. The rest have no children."
        ),
        check=_check_wide_tree,
    ),
    _Shape(
        shape_id="nested_in_object",
        tool=_DOC_TOOL,
        user=(
            "Submit a document titled 'Org Chart'. Its tree has root A; A has two "
            "children B and C; B has one child D. Put the tree in the document's "
            "`tree` field."
        ),
        check=_check_nested_in_object,
    ),
]


@pytest.mark.parametrize("cert", ALL_MODELS, ids=lambda c: c.model_id)
@pytest.mark.parametrize("shape", _SHAPES, ids=lambda s: s.shape_id)
def test_recursive_ref_tool_call(
    cert: ModelCertificate,
    shape: _Shape,
    live_client,
    skip_unless_capability_declared,
) -> None:
    """Recursive ``$ref`` argument must round-trip as a native nested dict.

    Same contract at every ``shape``: a non-empty tool call whose arguments are
    a native dict, then a shape-specific structural check that the recursion was
    neither stringified nor flattened.
    """
    skip_unless_capability_declared(cert, Capability.RECURSIVE_REF_TOOL_CALL)

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
