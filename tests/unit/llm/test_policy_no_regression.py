"""Generic no-regression / over-reach guard for recovery policies.

A recovery or transform response policy must NEVER turn VALID tool-call arguments INVALID.
This is the model-agnostic half of the policy oracle: it feeds a corpus of ALREADY-VALID
argument sets - with edge shapes that tempt an over-broad fix (a legitimate single-key object
literally named ``item``, populated native arrays, nested dicts) - through EVERY registered
model's RESOLVED response policy, and asserts the result still validates against the tool's
Pydantic schema.

Unlike the per-model recovery fixtures (which encode a specific quirk), this carries NO quirk
knowledge: the only property is "a valid call stays valid". A policy scoped correctly (like the
shipped MiniMax-M3 tags recovery) passes; a whole-tree fix that listifies any ``{"item": X}``
(the round-2 auto-resolve divergence) turns the legitimate ``item`` object into a list and fails
here - regardless of which model shipped it.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, TypeAdapter

from tolokaforge.core.llm.presets import build_capabilities
from tolokaforge.testing.certify import ALL_MODELS

pytestmark = pytest.mark.unit


class _Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: str
    qty: int


class _Container(BaseModel):
    """A legitimate object whose sole field is literally named ``item``."""

    model_config = ConfigDict(extra="forbid")
    item: _Item


class _Stow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    container: _Container
    labels: list[str]
    memo: str


class _Node(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    values: list[int]
    attrs: dict[str, str]


# (schema, root-level param_types, already-VALID arguments). Each args dict validates against
# its schema as-is; a policy must keep it valid. The `container`/`item` case is the over-reach
# trap - a single-key {"item": {...}} that a whole-tree unwrap would wrongly listify.
_CORPUS: list[tuple[type[BaseModel], dict[str, str], dict[str, Any]]] = [
    (
        _Stow,
        {"container": "object", "labels": "array", "memo": "string"},
        {"container": {"item": {"sku": "A1", "qty": 2}}, "labels": ["x", "y"], "memo": "note"},
    ),
    (
        _Stow,
        {"container": "object", "labels": "array", "memo": "string"},
        {"container": {"item": {"sku": "B2", "qty": 1}}, "labels": [], "memo": ""},
    ),
    (
        _Node,
        {"name": "string", "values": "array", "attrs": "object"},
        {"name": "root", "values": [1, 2, 3], "attrs": {"k": "v"}},
    ),
]

_MODELS = [(m.provider, m.name) for m in ALL_MODELS]


@pytest.mark.skipif(not _MODELS, reason="no models registered")
@pytest.mark.parametrize("provider,name", _MODELS, ids=[f"{p}:{n}" for p, n in _MODELS])
def test_resolved_policy_never_breaks_valid_args(provider: str, name: str) -> None:
    """Every model's resolved response policy must keep already-valid args valid."""
    policy = build_capabilities(name, provider).response_policy
    for schema, param_types, args in _CORPUS:
        # sanity: the corpus item is valid to begin with
        TypeAdapter(schema).validate_python(args)
        out = policy.parse_arguments(copy.deepcopy(args), param_types=param_types)
        try:
            TypeAdapter(schema).validate_python(out)
        except Exception as exc:  # noqa: BLE001 - surface which policy broke which case
            pytest.fail(
                f"{provider}:{name} response policy turned a VALID {schema.__name__} arg "
                f"INVALID (over-reach).\n  in:  {args}\n  out: {out}\n  err: {exc}"
            )
