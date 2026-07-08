"""Schema-driven array-conversion recovery test (Pydantic serialization oracle).

The MiniMax-M3 class of corruption is a provider-side XML->JSON conversion that mangles ARRAY
arguments into one of three shapes: ``{"item": [...]}`` (repeated-element wrapper), a JSON-
encoded string (``'["a"]'``), or an empty string. Rather than hand-author "input -> expected
output" pairs (an answer-key that encodes the fix), this test states the property structurally:

  take a VALID tool call (a Pydantic instance) -> inject each corruption shape into its array
  fields -> run the model's resolved response policy -> the result must (a) VALIDATE against the
  Pydantic schema and (b) ROUND-TRIP back to the original valid call.

The oracle is Pydantic validation + round-trip, not a hand-written answer. An uncorrupted valid
call is included too: it must come back UNCHANGED (the over-reach guard - a whole-tree fix that
listifies a legitimate single-key ``item`` object breaks this). Pure Python; no live model.

Per-model schema+instance is unavoidable (recovery is scoped to the real declared-array fields),
but it carries NO policy-output answer-key: it is a real valid call, corrupted and required to be
restored. Add a model by adding a CASES entry.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, TypeAdapter

from tolokaforge.core.llm.presets import build_capabilities


# --- schemas mirroring the zendesk-style tools where the M3 tags corruption lands -----------
class _Updates(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tags: list[str]


class _ZendeskUpdate(BaseModel):
    """update_item: the array (`tags`) lives one level deep under `updates`."""

    model_config = ConfigDict(extra="forbid")
    updates: _Updates


class _Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tags: list[str]


class _ZendeskCreate(BaseModel):
    """create_item: a single-key object literally named `item`, with a nested `tags` array."""

    model_config = ConfigDict(extra="forbid")
    item: _Item


def _corrupt(value: list[Any], shape: str) -> Any:
    """Apply one XML->JSON array-mangling shape to a native list."""
    if shape == "item_wrap":
        return {"item": value}
    if shape == "stringified":
        return json.dumps(value)
    if shape == "empty":
        return ""
    raise ValueError(shape)


def _inject(instance: dict[str, Any], path: tuple[str, ...], shape: str) -> dict[str, Any]:
    """Return a copy of `instance` with the array at `path` replaced by its corrupted form."""
    out = copy.deepcopy(instance)
    node = out
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = _corrupt(node[path[-1]], shape)
    return out


# (model_slug, schema, valid_instance, corrupt_path | None, shape_label). A None path is the
# over-reach guard: an uncorrupted valid call that must survive the policy unchanged.
_CASES: list[tuple[str, type[BaseModel], dict[str, Any], tuple[str, ...] | None, str]] = []
for _slug, _schema, _valid, _path in [
    (
        "minimax/minimax-m3",
        _ZendeskUpdate,
        {"updates": {"tags": ["vip", "urgent"]}},
        ("updates", "tags"),
    ),
    (
        "minimax/minimax-m3",
        _ZendeskCreate,
        {"item": {"tags": ["receipt-issued"]}},
        ("item", "tags"),
    ),
]:
    _CASES.append((_slug, _schema, _valid, None, "uncorrupted"))  # over-reach guard
    for _shape in ("item_wrap", "stringified"):
        _CASES.append((_slug, _schema, _valid, _path, _shape))
# empty string recovers to [], so use an already-empty array as the valid ground truth.
_CASES.append(
    ("minimax/minimax-m3", _ZendeskUpdate, {"updates": {"tags": []}}, ("updates", "tags"), "empty")
)


@pytest.mark.parametrize(
    "slug,schema,valid,path,shape",
    _CASES,
    ids=[f"{s.split('/')[-1]}:{sc.__name__}:{sh}" for s, sc, _v, _p, sh in _CASES],
)
def test_policy_recovers_array_corruption(
    slug: str,
    schema: type[BaseModel],
    valid: dict[str, Any],
    path: tuple[str, ...] | None,
    shape: str,
) -> None:
    """Corrupt a valid call's arrays, run the resolved policy, require validate + round-trip."""
    TypeAdapter(schema).validate_python(valid)  # the ground truth really is valid
    corrupted = _inject(valid, path, shape) if path is not None else copy.deepcopy(valid)
    policy = build_capabilities(slug, "openrouter").response_policy
    root_types = {k: ("object" if isinstance(v, dict) else "array") for k, v in valid.items()}
    out = policy.parse_arguments(corrupted, param_types=root_types)
    try:
        TypeAdapter(schema).validate_python(out)
    except Exception as exc:  # noqa: BLE001 - surface which shape the policy mishandled
        pytest.fail(
            f"{slug} {schema.__name__} [{shape}] did not validate after recovery:\n"
            f"  corrupted: {corrupted}\n  out: {out}\n  err: {exc}"
        )
    assert out == valid, (
        f"{slug} {schema.__name__} [{shape}] did not round-trip to the valid call:\n"
        f"  corrupted: {corrupted}\n  out: {out}\n  want: {valid}"
    )
