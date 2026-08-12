"""Canonical sanitizer-contract snapshots per preset.

Feeds a deterministic sample of Pydantic-generated tool schemas through
:func:`tolokaforge.core.llm.presets.build_capabilities` for every preset
registered in [`model_presets.yaml`](../../tolokaforge_models/src/tolokaforge_models/data/model_presets.yaml)
and snapshots the sanitised output. Guards against silent drift in:

* the YAML preset routing (name glob → policy mapping);
* the per-policy sanitizer behaviour (``PassthroughSchema`` /
  :class:`StrictSchema`).

In-process Pydantic inputs (no crawl of external ``tasks/``) → hermetic CI.
Two permanent invariants run alongside the snapshot comparison and apply
to every preset's output:

1. **No RE2-incompatible regex** — guards P1 (Decimal-pattern 500s).
2. **``required ⊆ properties.keys()``** for every object schema in every
   tool — guards the property-name-as-metadata-key bug class
   (post-PR-#88: ``d365_api_create_case`` declared ``title`` required but
   sanitiser dropped it from properties).
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

import pytest
from pydantic import BaseModel, Field, TypeAdapter

from tolokaforge.core.llm import build_capabilities

pytestmark = pytest.mark.canonical


# ---------------------------------------------------------------------------
# Pydantic models — cover every JSON-Schema feature the presets must handle
# ---------------------------------------------------------------------------


class Address(BaseModel):
    street: str = Field(description="Street line.")
    city: str = Field(description="City name.")
    zip_code: str = Field(description="Postal code.")


class OrderLine(BaseModel):
    sku_id: str = Field(description="SKU identifier.")
    quantity: int = Field(description="Requested units.", ge=0)


class Contact(BaseModel):
    name: str
    home: Address = Field(description="Home address.")
    work: Address | None = Field(default=None, description="Work address (optional).")


class ScalarsInput(BaseModel):
    """Plain scalar tool parameters — nothing to strip."""

    count: int = Field(description="An integer count.")
    label: str = Field(description="A string label.")
    enabled: bool = Field(description="A boolean flag.")
    ratio: float = Field(description="A floating-point ratio.")


class DecimalInput(BaseModel):
    """Pydantic Decimal idiom (primary P1 target)."""

    amount: Decimal = Field(description="A monetary amount.")
    tax: Decimal = Field(description="Sales tax.")


class DateTimeInput(BaseModel):
    """``format: date-time`` hints on string fields."""

    occurred_at: datetime = Field(description="When it happened.")
    due_by: datetime = Field(description="Deadline.")


class UnionsInput(BaseModel):
    """``Optional`` / ``Union`` / ``Literal`` unions — non-Decimal ``anyOf``."""

    note: str | None = None
    status: Literal["pending", "active", "closed"] = Field(description="Status.")
    count_or_label: int | str = Field(description="Either a count or a label.")


class DictMapInput(BaseModel):
    """Typed dict-map (``additionalProperties: {schema}``)."""

    lines: dict[str, OrderLine] = Field(description="Map of sku_id -> line.")


class NestedInput(BaseModel):
    """One- and two-level nested models."""

    primary: Address = Field(description="Primary address.")
    contact: Contact = Field(description="Contact with nested addresses.")


class ListInput(BaseModel):
    """``List[SomeModel]`` of nested models."""

    lines: list[OrderLine] = Field(description="Order lines.")


class ExplicitPatternInput(BaseModel):
    """Explicit ``Field(pattern=…)`` — non-Decimal regex annotation."""

    sku: str = Field(
        description="SKU identifier matching the canonical pattern.",
        pattern=r"^SKU-[A-Z0-9]+$",
    )


_MODEL_SPECS: list[tuple[str, type[BaseModel], str]] = [
    ("scalars", ScalarsInput, "Plain scalar parameters."),
    ("decimal", DecimalInput, "Monetary amounts with Decimal."),
    ("datetime", DateTimeInput, "Timestamps as datetime."),
    ("unions", UnionsInput, "Optional/Union/Literal parameters."),
    ("dict_map", DictMapInput, "Typed dict-map parameter."),
    ("nested", NestedInput, "Nested-model parameters."),
    ("list_nested", ListInput, "List-of-model parameter."),
    ("explicit_pattern", ExplicitPatternInput, "Explicit Field(pattern=…)."),
]


def _build_tool_list() -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for name, model, description in _MODEL_SPECS:
        schema = TypeAdapter(model).json_schema()
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": schema,
                },
            }
        )
    return tools


# ---------------------------------------------------------------------------
# Preset routing — one representative (model_name, provider) pair per preset
#
# Routing rules live in ``tolokaforge_models/data/model_presets.yaml``; these
# picks exercise the glob patterns for each preset block.
# ---------------------------------------------------------------------------


_PRESET_SELECTORS: list[tuple[str, str, str]] = [
    # preset name, model_name, provider
    ("default", "some/unregistered-model", "other"),
    ("anthropic", "anthropic/claude-sonnet-4-6", "openrouter"),
    ("openai_gpt5", "openai/gpt-5.5", "openrouter"),
    ("xai_grok", "x-ai/grok-4.20", "openrouter"),
    ("qwen", "qwen/qwen3.6-plus", "openrouter"),
    ("aws_nova", "nova-pro-v1", "nova"),
]


# Presets that go through ``StrictSchema`` — same RE2 invariant applies.
# Qwen now uses ``PassthroughSchema`` (the strict array conversion was a
# no-op in production and contradicted DictMapHints + task docs); its
# Pydantic-shape regex / format keys are tolerated by Qwen's tool-call
# validator and surface failures via the ``json_coerce`` response policy
# rather than schema rewriting.
_STRICT_PRESETS: list[tuple[str, str, str]] = [
    ("openai_gpt5", "openai/gpt-5.5", "openrouter"),
    ("xai_grok", "x-ai/grok-4.20", "openrouter"),
]


# ---------------------------------------------------------------------------
# Permanent invariants — applied to every preset's output
# ---------------------------------------------------------------------------


_RE2_INCOMPAT = re.compile(r"\(\?!|\(\?=|\(\?<!|\(\?<=|\\[1-9]")


def _contains_re2_incompat(obj: Any) -> bool:
    return _RE2_INCOMPAT.search(json.dumps(obj)) is not None


def _walk_object_schemas(node: Any, path: str):
    """Yield every ``(path, object_schema)`` pair where the schema declares
    both ``properties`` and ``required``. Used by the structural-invariant
    walker."""
    if isinstance(node, list):
        for i, item in enumerate(node):
            yield from _walk_object_schemas(item, f"{path}[{i}]")
        return
    if not isinstance(node, dict):
        return
    if "properties" in node and "required" in node:
        yield path, node
    # Recurse into all known schema-bearing positions
    for key, value in node.items():
        sub_path = f"{path}.{key}" if path else key
        if key in ("properties", "patternProperties", "$defs", "definitions"):
            if isinstance(value, dict):
                for prop_name, prop_schema in value.items():
                    yield from _walk_object_schemas(prop_schema, f"{sub_path}.{prop_name}")
        elif key in ("anyOf", "oneOf", "allOf", "prefixItems"):
            if isinstance(value, list):
                for i, branch in enumerate(value):
                    yield from _walk_object_schemas(branch, f"{sub_path}[{i}]")
        elif key in ("items", "not", "if", "then", "else", "contains") or (
            key == "additionalProperties" and isinstance(value, dict)
        ):
            yield from _walk_object_schemas(value, sub_path)


def _assert_required_subset_of_properties(tools: list[dict[str, Any]]) -> None:
    """Walk every object schema in the tool list; raise if any ``required``
    entry references a property name not declared in ``properties``."""
    for tool in tools:
        params = tool.get("function", {}).get("parameters")
        if not isinstance(params, dict):
            continue
        tool_name = tool.get("function", {}).get("name", "<anon>")
        for path, schema in _walk_object_schemas(params, f"{tool_name}.parameters"):
            props = schema.get("properties") or {}
            required = schema.get("required") or []
            missing = [r for r in required if r not in props]
            assert not missing, (
                f"Structural invariant violated at {path}: required field(s) "
                f"{missing!r} are not declared in properties "
                f"(declared: {sorted(props.keys())!r}). The model cannot "
                "satisfy this schema; this is the property-name-as-metadata-"
                "key bug class. See plans/eval_post_pr88_schema_sanitizer_diagnosis.md."
            )


# ---------------------------------------------------------------------------
# Snapshot test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("preset_name", "model_name", "provider"),
    _PRESET_SELECTORS,
    ids=[s[0] for s in _PRESET_SELECTORS],
)
def test_sanitizer_contract_snapshot(
    canon_snapshot, preset_name: str, model_name: str, provider: str
) -> None:
    """Snapshot sanitised tool list for each preset.

    Update baselines with::

        uv run pytest tests/canonical/test_sanitizer_contract.py --update-canon
    """
    capabilities = build_capabilities(model_name, provider)
    tools = _build_tool_list()
    sanitised = capabilities.schema_sanitizer.sanitize(copy.deepcopy(tools))

    snap = canon_snapshot("sanitizer_contract")
    snap.assert_match({"tools": sanitised}, f"{preset_name}.json")


@pytest.mark.parametrize(
    ("preset_name", "model_name", "provider"),
    _STRICT_PRESETS,
    ids=[s[0] for s in _STRICT_PRESETS],
)
def test_strict_preset_has_no_re2_incompatible_regex(
    preset_name: str, model_name: str, provider: str
) -> None:
    """Permanent P1 regression guard — every :class:`StrictSchema` preset.

    After ``StrictSchema`` runs for any preset in :data:`_STRICT_PRESETS`,
    NO tool in the output may contain ``(?!`` / ``(?=`` / ``(?<!`` /
    ``(?<=`` or a backreference. If this test fails, Decimal-bearing
    schemas are once again 500-ing on the provider's tool-schema validator.

    Covers: ``openai_gpt5`` (original P1), ``xai_grok`` (shares validator),
    ``qwen`` (Stage 2 P2 addition — same strict trio).
    """
    capabilities = build_capabilities(model_name, provider)
    tools = _build_tool_list()
    sanitised = capabilities.schema_sanitizer.sanitize(copy.deepcopy(tools))
    assert not _contains_re2_incompat(sanitised), (
        f"{preset_name} sanitiser leaked an RE2-incompatible regex — P1 regressed. "
        f"Output: {json.dumps(sanitised)[:600]}…"
    )


@pytest.mark.parametrize(
    ("preset_name", "model_name", "provider"),
    _PRESET_SELECTORS,
    ids=[s[0] for s in _PRESET_SELECTORS],
)
def test_every_preset_preserves_required_subset_of_properties(
    preset_name: str, model_name: str, provider: str
) -> None:
    """Permanent regression guard for the property-name-as-metadata-key bug.

    For every preset, every object schema in every tool must satisfy
    ``set(required) ⊆ set(properties.keys())``. Pre-fix, the recursive
    walker stripped properties literally named ``title`` / ``examples`` /
    ``format`` while leaving them in ``required``, producing schemas the
    model could not satisfy. The post-PR-#88 ots_bank_hr_d365 + ots_travel_
    marketplace_external_support runs each had ~85-95 % of trials rejected
    with ``Input validation failed: title: Field required``.

    This test runs against an in-process tool fixture that includes a tool
    with a property literally named ``title``. If the sanitiser ever drops
    such a property without dropping it from ``required``, this guard
    fires before the schema is ever sent to a live provider.
    """
    capabilities = build_capabilities(model_name, provider)
    tools = _build_tool_list() + [_collide_with_metadata_keyword_tool()]
    sanitised = capabilities.schema_sanitizer.sanitize(copy.deepcopy(tools))
    _assert_required_subset_of_properties(sanitised)


def _collide_with_metadata_keyword_tool() -> dict[str, Any]:
    """Tool whose property names collide with JSON-Schema metadata keywords.

    Mirrors the ots_*_d365_api_create_case shape from production: ``title``
    is both a Pydantic-emitted metadata key on every property AND a
    user-visible required property name. Pre-fix sanitisers stripped the
    property entry as if it were a metadata key.
    """
    return {
        "type": "function",
        "function": {
            "name": "collide_with_metadata_keyword",
            "description": "Property names colliding with JSON-Schema keywords.",
            "parameters": {
                "type": "object",
                "required": ["account_id", "title", "examples", "format"],
                "properties": {
                    "account_id": {
                        "type": "string",
                        "description": "Account identifier.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Short free-text title.",
                        "examples": ["Refund request"],
                        "title": "Title",
                    },
                    "examples": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Examples list.",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["pdf", "json", "html"],
                    },
                },
            },
        },
    }
