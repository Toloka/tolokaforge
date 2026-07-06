"""Structural variants of the shape-sensitive capability probes (observe stage).

The canonical capability tests under ``tests/integration/llm/`` test each construct
at ONE (usually top-level) shape and are the per-model certificate gate; we do NOT
touch them. This module adds VARIANTS: the same capability stressed at a different
nesting / breadth / container, so intermittent shape-dependent mis-shapes surface
that a single top-level probe misses. Empirical motivation: the MiniMax-M3
``{"item": [...]}`` artifact was materially worse when the construct was NESTED
inside another object (``order.items`` failed 100% of trials) than at top level
(the dict-map probe failed only ~20%), so a nested variant is the highest-value
addition.

Each :class:`ShapeVariant` is self-contained (its own schema + prompt + structural
check). The runner is :mod:`tests.integration.llm.variants.test_shape_variants`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, TypeAdapter

from .._capability import Capability


def _tool(name: str, description: str, model: type[BaseModel]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": TypeAdapter(model).json_schema(),
        },
    }


# --------------------------------------------------------------------------
# recursive_ref shapes
# --------------------------------------------------------------------------
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


_TREE_TOOL = _tool("submit_tree", "Submit a tree of labelled nodes.", _SubmitTree)
_DOC_TOOL = _tool(
    "submit_document", "Submit a document containing a labelled tree.", _SubmitDocument
)


def _max_depth(node: Any, depth: int = 1) -> int:
    if not isinstance(node, dict):
        return 0
    kids = node.get("children")
    if not isinstance(kids, list) or not kids:
        return depth
    return max((_max_depth(k, depth + 1) for k in kids if isinstance(k, dict)), default=depth)


def _check_deep_chain(args: dict[str, Any]) -> None:
    root = args.get("root")
    assert isinstance(root, dict) and "children" in root, f"`root` not a recursive node: {root!r}"
    depth = _max_depth(root)
    assert depth >= 3, f"recursion flattened/truncated: max depth {depth} < 3. root={root!r}"


def _check_wide_tree(args: dict[str, Any]) -> None:
    root = args.get("root")
    assert isinstance(root, dict), f"`root` not a dict: {root!r}"
    kids = root.get("children")
    assert (
        isinstance(kids, list) and len(kids) >= 4
    ), f"expected a wide root (>=4 children), got {kids!r}"
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
class ShapeVariant:
    """One structural variation of a shape-sensitive capability probe."""

    capability: Capability
    variant_id: str
    tools: tuple[dict[str, Any], ...]
    system: str
    user: str
    check: Callable[[dict[str, Any]], None]


_SYS_BUILDER = (
    "You are a structure-builder. Prefer calling tools over replying in prose. "
    "Issue a single tool call whose arguments carry the full nested structure "
    "exactly as described."
)


SHAPE_VARIANTS: list[ShapeVariant] = [
    ShapeVariant(
        capability=Capability.RECURSIVE_REF_TOOL_CALL,
        variant_id="deep_chain",
        tools=(_TREE_TOOL,),
        system=_SYS_BUILDER,
        user=(
            "Submit a tree that is a single deep chain: root A has one child B; "
            "B has one child C; C has one child D. No other nodes."
        ),
        check=_check_deep_chain,
    ),
    ShapeVariant(
        capability=Capability.RECURSIVE_REF_TOOL_CALL,
        variant_id="wide_tree",
        tools=(_TREE_TOOL,),
        system=_SYS_BUILDER,
        user=(
            "Submit a tree where root R has five children: A, B, C, D, E. "
            "A has one child A1, and B has one child B1. The rest have no children."
        ),
        check=_check_wide_tree,
    ),
    ShapeVariant(
        capability=Capability.RECURSIVE_REF_TOOL_CALL,
        variant_id="nested_in_object",
        tools=(_DOC_TOOL,),
        system=_SYS_BUILDER,
        user=(
            "Submit a document titled 'Org Chart'. Its tree has root A; A has two "
            "children B and C; B has one child D. Put the tree in the document's "
            "`tree` field."
        ),
        check=_check_nested_in_object,
    ),
]
