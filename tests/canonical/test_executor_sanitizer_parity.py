"""Canonical parity: the sanitized schema IS the accepted-args schema.

For every preset ``P`` and every tool ``T`` in the fixture set,

    ToolExecutor.execute(
        T.name, ARGS, call_id="canon", validation_schema=sanitize(T)
    )

accepts ``ARGS`` when ``ARGS`` conforms to ``sanitize(T)`` and rejects
``ARGS`` when ``ARGS`` violates ``sanitize(T)``.

Reuses the shared preset fixture (``_PRESET_SELECTORS``) and the tool
description list (``_MODEL_SPECS``) from
:mod:`tests.canonical.test_sanitizer_contract` unchanged, and extends both
locally with:

* one additional preset — ``gemini`` — because
  :attr:`GeminiSchema.flatten_oneof_discriminator` is the only sanitizer
  path that rewrites a ``oneOf``+``discriminator`` shape, and the shared
  fixture does not carry a Gemini row;
* one additional Pydantic model — :class:`DiscriminatedUnionInput` — whose
  parameters carry ``Annotated[Ticket | Task, Field(discriminator="kind")]``.
  Under the ``gemini`` preset this pair yields ``sanitize(T) != T``, so the
  invariant here exercises the flatten seam rather than collapsing to
  "the executor accepts what it always did".

The local extensions live in this module rather than in the shared file
because the shared file drives snapshot tests
(``test_sanitizer_contract_snapshot``) whose baselines are keyed by preset
name and tool count; extending them here keeps this stage's landing
snapshot-free.
"""

from __future__ import annotations

import copy
from typing import Annotated, Any, Literal

import pytest
from jsonschema import ValidationError
from jsonschema import validate as jsonschema_validate
from pydantic import BaseModel, Field, TypeAdapter

from tests.canonical.test_sanitizer_contract import (
    _MODEL_SPECS as _BASE_MODEL_SPECS,
)
from tests.canonical.test_sanitizer_contract import (
    _PRESET_SELECTORS as _BASE_PRESET_SELECTORS,
)
from tolokaforge.core.llm import build_capabilities
from tolokaforge.tools.registry import (
    Tool,
    ToolExecutionStatus,
    ToolExecutor,
    ToolRegistry,
    ToolResult,
)

pytestmark = pytest.mark.canonical


# ---------------------------------------------------------------------------
# Local fixture extension — Gemini preset + discriminated-union model
# ---------------------------------------------------------------------------


class Ticket(BaseModel):
    kind: Literal["ticket"]
    ticket_id: str = Field(description="Ticket identifier.")
    subject: str = Field(description="Ticket subject line.")


class Task(BaseModel):
    kind: Literal["task"]
    task_id: str = Field(description="Task identifier.")
    title: str = Field(description="Task title.")


class DiscriminatedUnionInput(BaseModel):
    """Pydantic-emitted ``oneOf`` + ``discriminator`` shape — the surface
    :meth:`GeminiSchema._flatten_oneof_discriminator` collapses into a
    single object schema unioning every branch's ``properties``."""

    item: Annotated[Ticket | Task, Field(discriminator="kind")]


_EXTENDED_MODEL_SPECS: list[tuple[str, type[BaseModel], str]] = list(_BASE_MODEL_SPECS) + [
    (
        "discriminated_union",
        DiscriminatedUnionInput,
        "Discriminated Ticket|Task item.",
    ),
]


_EXTENDED_PRESET_SELECTORS: list[tuple[str, str, str]] = list(_BASE_PRESET_SELECTORS) + [
    # ``google/gemini-3-pro-preview`` routes to :class:`GeminiSchema` (the
    # base flatten-enabled sanitizer). A ``gemini-3.5-flash`` name routes to
    # :class:`GeminiRecursiveSchema` (a subclass); either would exercise the
    # flatten path, but the base preset stays closer to the canonical shape.
    ("gemini", "google/gemini-3-pro-preview", "openrouter"),
]


def _build_extended_tool_list() -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for name, model, description in _EXTENDED_MODEL_SPECS:
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
# Minimal-instance generator — dumb by design
# ---------------------------------------------------------------------------


def _minimal_instance(schema: dict[str, Any], defs: dict[str, Any] | None = None) -> Any:
    """Return one minimal value conforming to ``schema``.

    Handles the JSON-Schema constructs the fixture's sanitized outputs use:
    ``enum`` / ``const`` (first value), ``$ref`` (resolved via ``defs``),
    ``oneOf`` / ``anyOf`` / ``allOf`` (first branch), and the leaf types
    (``object`` recurses into ``required``; ``array`` → ``[]``; scalars →
    their zero value). Constraints the generator does not model (``pattern``,
    ``minimum``, ``minLength``, …) are ignored; the round-trip assertion
    below re-validates the produced instance and skips the row when the
    witness lands invalid, keeping the generator "deliberately dumb" as
    specified by the plan.
    """
    if not isinstance(schema, dict):
        return None
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/") and defs is not None:
        target = defs.get(ref.removeprefix("#/$defs/"))
        if isinstance(target, dict):
            return _minimal_instance(target, defs)
    if "enum" in schema:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    for key in ("oneOf", "anyOf", "allOf"):
        branches = schema.get(key)
        if isinstance(branches, list) and branches:
            first = next((b for b in branches if isinstance(b, dict)), None)
            if first is not None:
                return _minimal_instance(first, defs)
    node_type = schema.get("type")
    if isinstance(node_type, list):
        node_type = next((t for t in node_type if t != "null"), node_type[0])
    if node_type == "object":
        properties = schema.get("properties") or {}
        required = schema.get("required") or []
        result: dict[str, Any] = {}
        for prop in required:
            prop_schema = properties.get(prop)
            if isinstance(prop_schema, dict):
                result[prop] = _minimal_instance(prop_schema, defs)
        return result
    if node_type == "array":
        return []
    if node_type == "integer":
        return 0
    if node_type == "number":
        return 0
    if node_type == "boolean":
        return False
    if node_type == "null":
        return None
    return ""


_WRONG_TYPE_SUBSTITUTES: dict[str, Any] = {
    "string": 999,
    "integer": "not an integer",
    "number": "not a number",
    "boolean": "not a boolean",
    "object": "not an object",
    "array": "not an array",
}


def _first_wrong_type_mutation(
    schema: dict[str, Any], instance: dict[str, Any], defs: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Return a shallow-mutated instance whose first required field carries a
    value of an incompatible type, or ``None`` when the schema declares no
    required fields at the root. The mutation is enough to trip jsonschema's
    ``type`` check on that field.
    """
    if schema.get("type") != "object":
        return None
    properties = schema.get("properties") or {}
    for prop in schema.get("required") or []:
        prop_schema = properties.get(prop)
        if not isinstance(prop_schema, dict):
            continue
        prop_type = prop_schema.get("type")
        if isinstance(prop_type, list):
            prop_type = next((t for t in prop_type if t != "null"), None)
        # Resolve one level of $ref when the sanitizer left it in place.
        if prop_type is None and defs is not None:
            ref = prop_schema.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = defs.get(ref.removeprefix("#/$defs/"))
                if isinstance(target, dict):
                    prop_type = target.get("type")
        substitute = _WRONG_TYPE_SUBSTITUTES.get(prop_type)
        if substitute is None:
            continue
        mutated = dict(instance)
        mutated[prop] = substitute
        return mutated
    return None


# ---------------------------------------------------------------------------
# Test tool — parameters come from the fixture's original (unsanitised) schema
# ---------------------------------------------------------------------------


class _AcceptingTool(Tool):
    """A tool that reports success on any arg dict — the executor's
    validation gate is the subject of these tests, not the tool's body."""

    def __init__(self, tool_name: str, parameters: dict[str, Any]) -> None:
        super().__init__(tool_name, "canonical parity fixture")
        self._parameters = parameters

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "canonical parity fixture",
                "parameters": self._parameters,
            },
        }

    def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult(success=True, output="ok")


_PARITY_ROWS: list[tuple[str, str, str, str]] = [
    (preset_name, model_name, provider, tool_name)
    for preset_name, model_name, provider in _EXTENDED_PRESET_SELECTORS
    for tool_name, _, _ in _EXTENDED_MODEL_SPECS
]


# ---------------------------------------------------------------------------
# Fixture-drift guard — the point of adding Gemini + discriminated_union
# ---------------------------------------------------------------------------


def test_at_least_one_row_has_sanitizer_rewriting_the_schema() -> None:
    """Locks the fixture-extension precondition: without at least one
    ``(preset, tool)`` pair where ``sanitize(T) != T``, every parametric row
    below would exercise the trivial "sanitizer left it alone" path and the
    invariant would not fire on a Gemini-flatten regression.
    """
    tools = _build_extended_tool_list()
    tools_by_name = {t["function"]["name"]: t for t in tools}
    rewrites = 0
    for _preset_name, model_name, provider in _EXTENDED_PRESET_SELECTORS:
        capabilities = build_capabilities(model_name, provider)
        sanitised = capabilities.schema_sanitizer.sanitize(copy.deepcopy(tools))
        for sanitised_tool in sanitised:
            tool_name = sanitised_tool["function"]["name"]
            if (
                sanitised_tool["function"]["parameters"]
                != tools_by_name[tool_name]["function"]["parameters"]
            ):
                rewrites += 1
    assert rewrites > 0, (
        "Fixture drift: every (preset, tool) row now has sanitize(T) == T. "
        "The parity invariant collapses to the trivial case; add a "
        "sanitizer-transforming (preset, model) pair to _EXTENDED_PRESET_SELECTORS "
        "and _EXTENDED_MODEL_SPECS."
    )


# ---------------------------------------------------------------------------
# The parity invariant — accept a valid minimal instance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("preset_name", "model_name", "provider", "tool_name"),
    _PARITY_ROWS,
    ids=[f"{row[0]}::{row[3]}" for row in _PARITY_ROWS],
)
def test_sanitized_schema_accepts_minimal_valid_instance(
    preset_name: str,
    model_name: str,
    provider: str,
    tool_name: str,
) -> None:
    """The ``ToolExecutor`` accepts a witness arg dict generated from the
    sanitized schema. A regression that made the executor reject a
    sanitized-conforming instance fires here with the specific ``(preset,
    tool)`` row that first breaks."""
    tools = _build_extended_tool_list()
    original = next(t for t in tools if t["function"]["name"] == tool_name)
    capabilities = build_capabilities(model_name, provider)
    sanitised = capabilities.schema_sanitizer.sanitize(copy.deepcopy(tools))
    sanitised_params = next(
        t["function"]["parameters"] for t in sanitised if t["function"]["name"] == tool_name
    )
    assert isinstance(sanitised_params, dict) and sanitised_params.get("type") == "object", (
        f"{preset_name}::{tool_name}: sanitized parameters root is not object-typed; "
        "the fixture-drift guard should have caught this."
    )

    registry = ToolRegistry()
    registry.register(_AcceptingTool(tool_name, original["function"]["parameters"]))
    executor = ToolExecutor(registry)

    defs = (
        sanitised_params.get("$defs") if isinstance(sanitised_params.get("$defs"), dict) else None
    )
    args = _minimal_instance(sanitised_params, defs)
    if not isinstance(args, dict):
        pytest.skip(
            f"{preset_name}::{tool_name}: generator produced a non-dict witness "
            f"({type(args).__name__}); no witness derivable at this row."
        )
    try:
        jsonschema_validate(instance=args, schema=sanitised_params)
    except ValidationError as exc:
        pytest.skip(
            f"{preset_name}::{tool_name}: dumb generator's witness violates a "
            f"constraint the generator does not model (pattern/min/max/…): {exc.message}"
        )

    result = executor.execute(tool_name, args, call_id="canon", validation_schema=sanitised_params)
    assert result.success is True, (
        f"{preset_name}::{tool_name}: sanitized-conforming witness rejected. "
        f"status={result.status!r} error={result.error!r} args={args!r}"
    )


# ---------------------------------------------------------------------------
# The parity invariant — reject an instance that violates the sanitized schema
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("preset_name", "model_name", "provider", "tool_name"),
    _PARITY_ROWS,
    ids=[f"{row[0]}::{row[3]}" for row in _PARITY_ROWS],
)
def test_sanitized_schema_rejects_wrong_type_at_required_scalar(
    preset_name: str,
    model_name: str,
    provider: str,
    tool_name: str,
) -> None:
    """The ``ToolExecutor`` rejects a witness with one required scalar
    replaced by a wrong-typed value. A ``(preset, tool)`` row whose
    sanitized schema has no required scalars (e.g. ``unions`` with only
    optional fields) is skipped — the mutation surface is empty by
    construction."""
    tools = _build_extended_tool_list()
    original = next(t for t in tools if t["function"]["name"] == tool_name)
    capabilities = build_capabilities(model_name, provider)
    sanitised = capabilities.schema_sanitizer.sanitize(copy.deepcopy(tools))
    sanitised_params = next(
        t["function"]["parameters"] for t in sanitised if t["function"]["name"] == tool_name
    )

    defs = (
        sanitised_params.get("$defs") if isinstance(sanitised_params.get("$defs"), dict) else None
    )
    valid_args = _minimal_instance(sanitised_params, defs)
    if not isinstance(valid_args, dict):
        pytest.skip(f"{preset_name}::{tool_name}: cannot produce a base witness to mutate.")
    mutated = _first_wrong_type_mutation(sanitised_params, valid_args, defs)
    if mutated is None:
        pytest.skip(
            f"{preset_name}::{tool_name}: sanitized schema has no required scalar leaf "
            "field to mutate; no witness of an invalid instance is derivable here."
        )

    registry = ToolRegistry()
    registry.register(_AcceptingTool(tool_name, original["function"]["parameters"]))
    executor = ToolExecutor(registry)

    result = executor.execute(
        tool_name, mutated, call_id="canon", validation_schema=sanitised_params
    )
    not_rejected_msg = f"{preset_name}::{tool_name}: mutation not rejected. args={mutated!r}"
    assert not result.success, not_rejected_msg
    wrong_status_msg = (
        f"{preset_name}::{tool_name}: mutation rejected with wrong status "
        f"{result.status!r}; parity invariant demands INVALID_ARGUMENTS."
    )
    assert result.status is ToolExecutionStatus.INVALID_ARGUMENTS, wrong_status_msg
