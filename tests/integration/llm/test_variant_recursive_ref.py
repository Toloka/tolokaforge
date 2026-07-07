"""Structural VARIANTS of the recursive_ref capability probe (observe stage).

The canonical ``test_recursive_ref_tool_call`` tests the construct at ONE shape;
this file stresses the SAME capability at different nesting / breadth / container
so intermittent shape-dependent mis-shapes surface that a single top-level probe
misses. Empirical motivation: the MiniMax-M3 ``{"item": [...]}`` artifact was worse
when the construct was NESTED inside another object (100% of trials) than at top
level, so ``nested_in_object`` is the highest-value variant.

Self-contained (its own schema + prompt + structural check per variant), living in
the canonical package with a ``test_variant_`` filename prefix. Gated by the cert
AND ``TF_RUN_VARIANTS`` so a normal CI run skips it; the auto-integration observe
workflow runs it (sets the flag + injects the all-required candidate cert). The
``ShapeVariant`` helper is kept local here; if a second variant file needs it, lift
it into a shared module then, not before.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import BaseModel, Field, TypeAdapter

from tolokaforge.core.models import Message, MessageRole

from ._capability import Capability, ModelCertificate
from .registry import ALL_MODELS


def _tool(name: str, description: str, model: type[BaseModel]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": TypeAdapter(model).json_schema(),
        },
    }


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


_VARIANTS: list[ShapeVariant] = [
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


@pytest.mark.parametrize("cert", ALL_MODELS, ids=lambda c: c.model_id)
@pytest.mark.parametrize(
    "variant", _VARIANTS, ids=lambda v: f"{v.capability.value}__{v.variant_id}"
)
def test_variant_recursive_ref(
    cert: ModelCertificate,
    variant: ShapeVariant,
    live_client,
    skip_unless_capability_declared,
) -> None:
    """Stress recursive_ref with one structural variation.

    Same contract as the canonical probe: a non-empty tool call whose arguments
    are a native dict, then a variant-specific structural check.
    """
    if not os.getenv("TF_RUN_VARIANTS"):
        pytest.skip("shape-variant suite runs only in the observe stage (set TF_RUN_VARIANTS)")
    skip_unless_capability_declared(cert, variant.capability)

    client = live_client(cert)
    result = client.generate(
        system=variant.system,
        messages=[Message(role=MessageRole.USER, content=variant.user)],
        tools=list(variant.tools),
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
