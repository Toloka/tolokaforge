"""Tool-schema sanitizer: rewrite JSON Schemas the model cannot accept verbatim.

The :class:`ToolSchemaSanitizer` Protocol is the contract every per-model
sanitizer implements. :class:`SchemaCapability` enumerates the fine-grained
capabilities the preset system composes.

Design principle — **preserve information by default, fail loudly on hazards**.
The sanitizer must never silently drop schema information the model relies on
for argument generation. JSON-Schema metadata (``title``, ``examples``,
``format``, regex ``pattern``, ``additionalProperties:true``) carries
**training-time signal** every modern function-calling model uses; stripping
it shifts the failure mode from "model emits the right shape" to "model
guesses". Three classes of past silent-drop bugs:

1. The recursive walker iterated ``properties: {…}`` and stripped property-name
   entries that *happened* to collide with a metadata keyword (``title``,
   ``examples``, ``format``). Result: ``required: [account_id, title,
   description]`` survived but ``properties.title`` was deleted. Models
   returned ``Field required`` errors on every call. Bug found post-PR-#88.

2. ``additionalProperties: true`` (the Pydantic free-form-object marker) was
   stripped together with ``examples``, leaving bare ``{type:object}`` with
   no shape hint. GPT-5.5 alternated between omitting the field, flat-packing
   inner fields at the parent level, and (rarely) the right shape — 99.9 %
   of trials hit a schema validation error.

3. ``examples`` were stripped from primitive strings, removing the only
   formatting hint when ``pattern`` and ``format`` were absent. The model
   guessed the wrong format (``"YYYY-MM-DD to YYYY-MM-DD"`` instead of
   ``"YYYY-MM-DD_YYYY-MM-DD"``).

The new walker is **position-aware**: it distinguishes JSON-Schema *metadata
keys* (``type``, ``properties``, ``items``, …) from *property-name keys*
inside ``properties: {…}`` / ``$defs: {…}`` / ``patternProperties: {…}``.
Property names are opaque strings and are never matched against any
metadata-strip list.

After sanitisation a structural-invariant validator runs and raises
:class:`ValueError` if any sanitised schema would be incoherent — currently:

* ``set(required) ⊆ set(properties.keys())`` for every object schema.
* No RE2-incompatible lookaround / backreference regex remains.

This is the "surface failures" rule: ship a schema or raise loudly, never
ship a poisoned schema.

What :class:`StrictSchema` actually rewrites (everything else passes through):

* The Pydantic ``Decimal`` ``anyOf`` idiom
  ``[{type:number}, {type:string, pattern:"…"}]`` collapses to plain
  ``{type:"number"}``. Pydantic's negative-lookahead regex is RE2-incompat;
  collapsing to ``number`` keeps the type info while shedding the regex.
* ``additionalProperties: {schema}`` (typed dict-map) on an object property
  is converted to ``type:array`` with explicit ``items.properties`` so
  GPT-5 / Grok / Qwen-strict has structural type info to anchor on.
  ``ArrayDictMapResponse`` reverses this on the model's emitted arguments.
* RE2-incompatible ``pattern`` values (lookarounds ``(?!``/``(?=``/``(?<!``/
  ``(?<=`` and backreferences ``\\1``..``\\9``) are stripped — this is the
  only key-strip the sanitiser still does, and it's *value-conditional*, not
  blanket. Safe patterns (e.g. ``^SKU-[A-Z0-9]+$``) pass through unchanged.
* ``description`` at the parameters root is stripped (Pydantic emits the
  class docstring there; redundant with ``function.description``).
* ``$defs`` is inlined and removed from output.
"""

from __future__ import annotations

import json
import re
from enum import Enum
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "SchemaCapability",
    "ToolSchemaSanitizer",
    "PassthroughSchema",
    "StrictSchema",
    "GeminiSchema",
    "SchemaInvariantError",
]


class SchemaCapability(str, Enum):
    """Schema features a model can natively accept.

    Presets declare the **required** capability set on the per-model side.
    A sanitizer's :meth:`ToolSchemaSanitizer.supported_capabilities` advertises
    which capabilities the *output* of ``sanitize()`` still carries unchanged;
    the difference between "required" and "supported" is where transformations
    must compose.
    """

    DICT_MAP_TYPED = "dict_map_typed"
    REGEX_PATTERN = "regex_pattern"
    DATE_TIME_FORMAT = "date_time_format"
    ANYOF_NUMERIC_STRING = "anyof_numeric_string"


_ALL_CAPABILITIES: frozenset[SchemaCapability] = frozenset(SchemaCapability)


@runtime_checkable
class ToolSchemaSanitizer(Protocol):
    """Rewrite tool JSON Schemas to match the model's accepted subset."""

    def sanitize(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return a sanitized copy of ``tools`` suitable for the target model."""

    def supported_capabilities(self) -> frozenset[SchemaCapability]:
        """Capabilities that pass through ``sanitize()`` unmodified."""


class PassthroughSchema:
    """No transformation — model handles full JSON Schema."""

    def sanitize(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return tools

    def supported_capabilities(self) -> frozenset[SchemaCapability]:
        return _ALL_CAPABILITIES


# ---------------------------------------------------------------------------
# StrictSchema
# ---------------------------------------------------------------------------


class SchemaInvariantError(ValueError):
    """Raised when a sanitised tool schema violates a structural invariant.

    The most common cause is a mismatch between ``required`` and ``properties``:
    a property name appears in ``required`` but not in ``properties``. This
    indicates either an upstream-broken input schema or a sanitiser bug that
    silently dropped a property — in both cases we refuse to ship.
    """


# Matches any RE2-incompatible regex construct — the four lookaround forms
# and any backreference ``\1``..``\9``. Used both as the conditional pattern
# strip rule and as a hard post-condition on :meth:`StrictSchema.sanitize`.
_RE2_INCOMPATIBLE = re.compile(r"\(\?!|\(\?=|\(\?<!|\(\?<=|\\[1-9]")


# JSON-Schema keyword positions. Walking the tree must distinguish:
#
# * SCHEMA position — keys are metadata keywords (``type``, ``properties``,
#   ``items``, ``description``, ``examples``, …). Recurse on schema-valued
#   keywords; pass through metadata-valued keywords (``description``,
#   ``examples``, ``title``, …) verbatim unless we have a specific reason to
#   transform them (Decimal anyOf collapse, RE2-incompat pattern strip).
#
# * PROPERTIES-MAP position — keys are user-defined property names → schemas.
#   Property names are opaque strings; never strip them. Recurse into values
#   as SCHEMA position.
#
# * SCHEMA-LIST position — values of ``anyOf`` / ``oneOf`` / ``allOf`` /
#   ``prefixItems`` are lists of schemas.
#
# * SCHEMA-VALUED position — values of ``items`` / ``not`` / ``if`` /
#   ``then`` / ``else`` / ``contains`` are individual schemas.
#
# Any keyword name not in one of the four routing sets is treated as opaque
# metadata (carried through unchanged).

_PROPERTY_MAP_KEYS: frozenset[str] = frozenset(
    {"properties", "patternProperties", "$defs", "definitions"}
)
_SCHEMA_LIST_KEYS: frozenset[str] = frozenset({"anyOf", "oneOf", "allOf", "prefixItems"})
_SCHEMA_VALUED_KEYS: frozenset[str] = frozenset({"items", "not", "if", "then", "else", "contains"})


class StrictSchema:
    """Position-aware sanitiser for OpenAI / xAI / Qwen function-calling.

    The sanitiser preserves JSON-Schema metadata by default and only rewrites
    constructs known to break the target providers' tool-schema validators:

    1. ``$defs`` / ``$ref`` resolution (pre-pass; refs inlined, ``$defs`` removed).
    2. Pydantic Decimal ``anyOf`` collapse — ``[{type:number}, {type:string,
       pattern:"…"}]`` becomes plain ``{type:"number"}`` with description preserved.
    3. Typed dict-map → array conversion — ``{type:object,
       additionalProperties:{schema}}`` becomes ``{type:array, items:{type:object,
       properties:{key, …value_props}}}``. Reversed by :class:`ArrayDictMapResponse`.
    4. RE2-incompatible regex stripping — ``pattern`` keys whose value contains
       a lookaround or backreference are removed; safe patterns pass through.

    Everything else — ``title``, ``examples``, ``format``, ``minProperties``,
    ``maxProperties``, ``additionalProperties: true``, ``additionalProperties:
    false``, plain regex patterns, enum values — passes through unchanged.

    After sanitisation a structural-invariant pass walks the output and raises
    :class:`SchemaInvariantError` if any object schema's ``required`` list
    references a property name not declared in ``properties``. This is the
    regression guard for the "property-name-as-metadata-key" bug class.
    """

    #: Name of the synthetic key field added when converting dict-maps → arrays.
    KEY_FIELD = "key"

    #: Name of the synthetic value field added for *scalar*-valued dict-maps
    #: (e.g. ``Dict[str, int]``). Object-valued maps lift the value model's
    #: own fields onto the synthetic item object and need no wrapper; a scalar
    #: value has no fields to lift, so without this field the scalar would be
    #: silently dropped and the model left with nowhere to put it.
    #: :class:`~tolokaforge.core.llm.response_policy.ArrayDictMapResponse`
    #: unwraps ``{value: X}`` items back to the bare scalar ``X``.
    VALUE_FIELD = "value"

    _MAX_REF_DEPTH = 16

    #: When ``True``, flatten ``oneOf`` discriminated unions into a single
    #: object schema unioning all branch ``properties``. Gemini's tool
    #: spec is a JSON-Schema *subset* that does not document ``oneOf`` /
    #: ``discriminator``; with these constructs present the model emits
    #: description-derived field names rather than the registered ones.
    #: Subclass overrides this for Gemini routes; the GPT-5 / xAI Grok
    #: routes leave it ``False`` since both providers handle ``oneOf``
    #: natively.
    flatten_oneof_discriminator: bool = False

    #: When ``True``, strip Pydantic's class-level ``description`` artefact
    #: at the parameters root (e.g. ``"Input model for d365_api.create_case
    #: tool."``) because it duplicates ``function.description``. The
    #: default ``True`` is appropriate for OpenAI / xAI Grok which have
    #: stricter validators and don't benefit from the redundant text.
    #: Subclasses set ``False`` when production evidence shows the strip
    #: hurts (e.g. Gemini sometimes drops optional fields when the root
    #: description is absent — observed as a regression on flat
    #: ``d365_api_create_case`` tools in a travel marketplace evaluation).
    strip_parameters_root_description: bool = True

    #: When ``True``, remove ``pattern`` values that contain RE2-incompatible
    #: constructs (lookarounds ``(?!``/``(?=``/``(?<!``/``(?<=`` and
    #: backreferences ``\1``..``\9``). OpenAI / xAI / Qwen-strict providers
    #: 500 on these patterns, so the default ``True`` preserves their
    #: pre-existing safety guard. Providers that don't enforce RE2 (Gemini
    #: appears to pass them through unchanged) should override to ``False``
    #: — the pattern carries format-hint signal the model uses for
    #: optional-field selection. Removing it on travel_marketplace's
    #: 7 Pydantic-Decimal-string patterns correlated with a 50 % → 10 %
    #: Gemini 3.5 Flash regression on that domain's evaluation.
    strip_re2_incompatible_patterns: bool = True

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def sanitize(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        sanitised: list[dict[str, Any]] = []
        for tool in tools:
            tool = self._inline_refs_in_tool(tool)
            tool = self._sanitise_tool(tool)
            sanitised.append(tool)
        self._validate_invariants(sanitised)
        return sanitised

    def supported_capabilities(self) -> frozenset[SchemaCapability]:
        # ``StrictSchema`` rewrites two structural idioms (``DICT_MAP_TYPED``
        # and ``ANYOF_NUMERIC_STRING``) and conditionally strips RE2-unsafe
        # patterns. ``DATE_TIME_FORMAT`` and the broader ``REGEX_PATTERN``
        # capability survive because we now preserve safe patterns and all
        # ``format`` values.
        return _ALL_CAPABILITIES - {
            SchemaCapability.DICT_MAP_TYPED,
            SchemaCapability.ANYOF_NUMERIC_STRING,
        }

    # ------------------------------------------------------------------
    # Per-tool rewriter
    # ------------------------------------------------------------------

    def _sanitise_tool(self, tool: Any) -> Any:
        """Apply the sanitiser to one tool's parameters block.

        Top-level cleanup runs alongside the recursive walk: the parameters
        root may carry Pydantic's class-level ``description`` (the model
        class's docstring) which is redundant with ``function.description``.
        """
        if not isinstance(tool, dict):
            return tool
        func = tool.get("function")
        if not isinstance(func, dict):
            return tool
        params = func.get("parameters")
        if not isinstance(params, dict):
            return tool

        sanitised_params = self._sanitise_schema(params)
        if isinstance(sanitised_params, dict) and self.strip_parameters_root_description:
            # Strip Pydantic's class-docstring artefact at the parameters root —
            # it duplicates ``function.description`` and is the one place a
            # ``description`` key is universally noise for OpenAI / xAI Grok.
            sanitised_params.pop("description", None)

        new_func = dict(func)
        new_func["parameters"] = sanitised_params
        new_tool = dict(tool)
        new_tool["function"] = new_func
        return new_tool

    # ------------------------------------------------------------------
    # ``$ref`` resolution — runs *before* the recursive walker
    # ------------------------------------------------------------------

    @classmethod
    def _inline_refs_in_tool(cls, tool: Any) -> Any:
        """Resolve every ``$ref`` against the tool's parameter-level
        ``$defs`` block, then drop the now-stale ``$defs``.

        Without this pre-pass the dict-map → array conversion sees a
        ``value_schema`` of the form ``{"$ref": "#/$defs/<Model>"}`` and
        accesses ``.get("properties", {})`` on the ref dict, which yields
        an empty list of fields. The resulting ``items`` schema then
        carries only the synthetic ``key`` property and the model has no
        idea where to put the value-side fields — observed live as
        GPT-5.5 / Grok-4 packing ``qty`` and ``price`` into the ``key``
        string itself (e.g. ``"SKU-A|qty=10|price=9.99"``).

        Each tool's ``$defs`` is local to its parameters block; resolving
        per-tool keeps siblings independent. Cycles are detected via a
        depth-bounded resolution (a $ref pointing through itself raises
        rather than recursing forever).
        """
        if not isinstance(tool, dict):
            return tool
        func = tool.get("function")
        if not isinstance(func, dict):
            return tool
        params = func.get("parameters")
        if not isinstance(params, dict):
            return tool
        defs = params.get("$defs")
        if not isinstance(defs, dict):
            return tool
        resolved_params = cls._inline_refs(params, defs, depth=0)
        if isinstance(resolved_params, dict):
            resolved_params.pop("$defs", None)
        new_func = dict(func)
        new_func["parameters"] = resolved_params
        new_tool = dict(tool)
        new_tool["function"] = new_func
        return new_tool

    @classmethod
    def _inline_refs(
        cls,
        schema: Any,
        defs: dict[str, Any],
        depth: int,
        ref_path: tuple[str, ...] = (),
    ) -> Any:
        """Recursively replace ``{"$ref": "#/$defs/<Name>"}`` with the
        referenced sub-schema. Other dict / list nodes pass through with
        children resolved.

        Recursive (cyclic) ``$ref`` chains — e.g. a ``TreeNode`` whose
        ``children`` are themselves ``TreeNode`` — cannot be fully inlined and
        used to raise here. Instead the resolver tracks the ``$ref`` names on
        the active resolution path (``ref_path``): a ``$ref`` that revisits a
        name already on the path is a genuine cycle and terminates *that
        branch* with a permissive ``{"type": "object"}`` node (description
        preserved). The model has already seen the recursive shape at the outer
        level and keeps nesting from the user instruction; the permissive
        terminal simply stops the inliner without discarding the surrounding
        structure. ``depth`` remains a defensive backstop against pathological
        non-cyclic nesting and likewise degrades to the permissive terminal
        rather than raising.
        """
        if depth > cls._MAX_REF_DEPTH:
            return {"type": "object"}
        if isinstance(schema, list):
            return [cls._inline_refs(item, defs, depth + 1, ref_path) for item in schema]
        if not isinstance(schema, dict):
            return schema
        ref = schema.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            target_name = ref.removeprefix("#/$defs/")
            target = defs.get(target_name)
            if target is None:
                raise ValueError(
                    f"StrictSchema: $ref {ref!r} points to a missing "
                    f"$defs entry. Available: {sorted(defs.keys())!r}"
                )
            if target_name in ref_path:
                # Cyclic self-reference — inline one level then terminate this
                # branch with a permissive object so recursion halts safely.
                terminal: dict[str, Any] = {"type": "object"}
                desc = schema.get("description")
                if isinstance(desc, str) and desc:
                    terminal["description"] = desc
                return terminal
            # Resolve the target (which may itself contain $refs), then merge
            # any sibling keys from the original node — Pydantic occasionally
            # emits ``{"$ref": ..., "description": ...}`` to attach a per-use
            # description without redefining the model.
            resolved = cls._inline_refs(target, defs, depth + 1, ref_path + (target_name,))
            siblings = {k: v for k, v in schema.items() if k != "$ref"}
            if not siblings:
                return resolved
            if isinstance(resolved, dict):
                merged = dict(resolved)
                for k, v in siblings.items():
                    merged[k] = cls._inline_refs(v, defs, depth + 1, ref_path)
                return merged
            return resolved
        return {k: cls._inline_refs(v, defs, depth + 1, ref_path) for k, v in schema.items()}

    # ------------------------------------------------------------------
    # Position-aware recursive walker
    # ------------------------------------------------------------------

    def _sanitise_schema(self, schema: Any) -> Any:
        """Process a node in **schema position** — keys are JSON-Schema
        metadata keywords (``type``, ``properties``, ``items``, ``anyOf``,
        ``description``, ``examples``, ``pattern``, …).
        """
        if isinstance(schema, list):
            # Defensive: an unexpected list at schema position. Walk each
            # item as a schema (covers callers passing list-typed schemas).
            return [self._sanitise_schema(item) for item in schema]
        if not isinstance(schema, dict):
            return schema

        # Decimal anyOf collapse runs first — the negative-lookahead pattern
        # in the string branch is RE2-incompat and the whole anyOf has no
        # provider that handles it portably.
        if self._is_decimal_numeric_string_anyof(schema.get("anyOf")):
            collapsed: dict[str, Any] = {"type": "number"}
            if "description" in schema:
                collapsed["description"] = schema["description"]
            return collapsed

        result: dict[str, Any] = {}
        dict_map_value_schema: dict[str, Any] | None = None

        for key, value in schema.items():
            if key in _PROPERTY_MAP_KEYS:
                # Property-name → schema map. Keys are opaque strings; never
                # strip them (this is the position-awareness fix that closes
                # the property-named-"title" bug class).
                result[key] = self._sanitise_property_map(value)

            elif key in _SCHEMA_LIST_KEYS:
                # List of branch schemas.
                if isinstance(value, list):
                    result[key] = [self._sanitise_schema(b) for b in value]
                else:
                    result[key] = value

            elif key in _SCHEMA_VALUED_KEYS:
                # Value is itself a schema — recurse.
                result[key] = self._sanitise_schema(value)

            elif key == "additionalProperties":
                # Three flavours, all preserved by default. Only the
                # ``{schema}`` flavour triggers the dict-map → array pivot.
                if isinstance(value, dict):
                    dict_map_value_schema = value
                    # Don't carry the original ``additionalProperties: {schema}``
                    # into ``result`` — it will be replaced by the array pivot
                    # below. Skip the assignment.
                    continue
                # ``True`` (free-form-object marker) and ``False``
                # (no-extras marker) survive untouched. Both are accepted
                # by every current function-calling provider.
                result[key] = value

            elif key == "pattern":
                # Conditional strip — only when RE2-incompatible AND the
                # subclass opts into the strip. Safe patterns always
                # survive. Providers that don't enforce RE2 (Gemini)
                # override ``strip_re2_incompatible_patterns`` to keep
                # the format-hint signal.
                if (
                    self.strip_re2_incompatible_patterns
                    and isinstance(value, str)
                    and _RE2_INCOMPATIBLE.search(value)
                ):
                    continue
                result[key] = value

            elif key == "required":
                # Plain list of strings (property names). Never recurse into
                # it — these are user-defined names, not schema nodes.
                result[key] = list(value) if isinstance(value, list) else value

            elif key == "enum":
                # Enum values are user-supplied literals (strings, numbers,
                # objects, …); pass through verbatim.
                result[key] = value

            else:
                # Metadata / leaf keys at schema position: ``type``,
                # ``description``, ``examples``, ``title``, ``format``,
                # ``minProperties``, ``maxProperties``, ``minimum``,
                # ``maximum``, ``minLength``, ``maxLength``, ``default``,
                # ``const``, … — preserve verbatim. The "preserve information
                # by default" rule.
                result[key] = value

        if dict_map_value_schema is not None:
            result = self._convert_dict_map_to_array(result, dict_map_value_schema)

        if self.flatten_oneof_discriminator and self._is_discriminated_union(result):
            result = self._flatten_oneof_discriminator(result)

        return result

    # ------------------------------------------------------------------
    # Discriminated-union flattening (Gemini)
    # ------------------------------------------------------------------

    @staticmethod
    def _is_discriminated_union(schema: dict[str, Any]) -> bool:
        """True when *schema* is a ``oneOf`` of object branches with an
        explicit ``discriminator`` keyword — the wire shape Pydantic emits
        for ``Annotated[A | B, Field(discriminator='kind')]``.

        Bare ``Union[A, B]`` (Pydantic emits ``anyOf`` with inline
        branches, no ``discriminator``) is **not** flattened — Gemini
        handles inline ``anyOf`` branches correctly at the field-name
        level, and flattening loses per-branch ``required`` enforcement
        plus dilutes the model's attention across 4×N merged optional
        fields. A logistics domain eval (40 % → 0 %) regressed when
        this path was over-eager — see `AGENTS.md` gotcha #21.
        """
        if "discriminator" not in schema:
            return False
        branches = schema.get("oneOf")
        if not isinstance(branches, list) or len(branches) < 2:
            return False
        return all(
            isinstance(b, dict)
            and b.get("type") == "object"
            and isinstance(b.get("properties"), dict)
            for b in branches
        )

    def _flatten_oneof_discriminator(self, schema: dict[str, Any]) -> dict[str, Any]:
        """Collapse a ``oneOf`` / ``anyOf`` of object branches into one
        object schema unioning every branch's properties.

        The result:

        * ``properties`` — union of every branch's properties. Where a key
          appears in multiple branches, the first branch's schema wins
          (we don't try to merge incompatible branch types; the
          discriminator gates which fields the model actually populates).
        * ``required`` — intersection of every branch's required list,
          so only fields that are required in *every* branch (typically
          just the discriminator) survive.
        * ``oneOf`` / ``anyOf`` / ``discriminator`` — removed.

        This is lossy for type fidelity (e.g. the model might emit
        ``ticket_id`` on a ``kind=ticket`` item, which the schema no
        longer prohibits) but is the only way to make a discriminated
        union round-trip on Gemini's schema subset. The system prompt
        and the existing ``description`` text remain the source of
        per-discriminator-value field guidance.
        """
        branches = schema["oneOf"]

        merged_props: dict[str, Any] = {}
        per_field_branch_schemas: dict[str, list[dict[str, Any]]] = {}
        required_sets: list[set[str]] = []
        for branch in branches:
            for name, sub in branch["properties"].items():
                per_field_branch_schemas.setdefault(name, []).append(sub)
            required_sets.append(set(branch.get("required", []) or []))

        for name, sub_schemas in per_field_branch_schemas.items():
            merged_props[name] = self._merge_branch_property_schemas(sub_schemas)

        common_required: set[str] = set.intersection(*required_sets) if required_sets else set()

        flat: dict[str, Any] = {
            k: v for k, v in schema.items() if k not in ("oneOf", "discriminator")
        }
        flat["type"] = "object"
        flat["properties"] = merged_props
        flat["required"] = sorted(common_required)
        return flat

    @staticmethod
    def _merge_branch_property_schemas(sub_schemas: list[dict[str, Any]]) -> dict[str, Any]:
        """Merge per-branch schemas for the same property name.

        Special-cased: when a property is the discriminator (Pydantic emits
        each branch with ``Literal["X"]`` → ``{type: "string", const: "X"}``),
        gather every ``const`` and lift it into a single ``enum``. Without
        this the merged schema would advertise only the first branch's
        ``const``, which forbids the model from emitting the other
        discriminator values.

        For non-discriminator properties, the first branch's schema wins —
        the model is gated by the per-branch description / by the
        discriminator value in practice.
        """
        if len(sub_schemas) == 1:
            return sub_schemas[0]

        consts = [s.get("const") for s in sub_schemas if "const" in s]
        if consts and len(consts) == len(sub_schemas):
            # Every branch contributes a const value for this property —
            # discriminator pattern. Merge into an enum.
            merged: dict[str, Any] = {k: v for k, v in sub_schemas[0].items() if k != "const"}
            merged["enum"] = sorted(set(consts))
            return merged

        # No special case applies — first branch wins.
        return sub_schemas[0]

    def _sanitise_property_map(self, mapping: Any) -> Any:
        """Process a node in **property-map position** (children of
        ``properties`` / ``patternProperties`` / ``$defs`` / ``definitions``).

        Keys are property names — opaque strings, **never** matched against
        any metadata-strip set. Values are schemas; recurse with
        :meth:`_sanitise_schema`.
        """
        if not isinstance(mapping, dict):
            return mapping
        return {name: self._sanitise_schema(sub) for name, sub in mapping.items()}

    # ------------------------------------------------------------------
    # Decimal ``anyOf`` recogniser
    # ------------------------------------------------------------------

    @staticmethod
    def _is_decimal_numeric_string_anyof(value: Any) -> bool:
        """True for the Pydantic ``Decimal`` idiom.

        Matches exactly a two-branch ``anyOf`` where one branch is
        ``{type: "number"}`` (optionally with nothing else) and the other is
        ``{type: "string", pattern: "…"}`` (optionally with nothing else).
        Order-insensitive; extra semantic keys on either branch reject the
        match so regular ``anyOf`` unions are left untouched.
        """
        if not isinstance(value, list) or len(value) != 2:
            return False
        branches = [b for b in value if isinstance(b, dict)]
        if len(branches) != 2:
            return False
        number_branch = next(
            (b for b in branches if b.get("type") == "number"),
            None,
        )
        string_branch = next(
            (b for b in branches if b.get("type") == "string" and "pattern" in b),
            None,
        )
        if number_branch is None or string_branch is None:
            return False
        # Number branch may only carry ``type``; anything semantic (e.g. another
        # ``pattern``) disqualifies the collapse.
        if set(number_branch.keys()) - {"type"}:
            return False
        # String branch may only carry ``type`` and ``pattern``.
        if set(string_branch.keys()) - {"type", "pattern"}:
            return False
        return True

    # ------------------------------------------------------------------
    # Dict-map → array transform
    # ------------------------------------------------------------------

    def _convert_dict_map_to_array(
        self, result: dict[str, Any], value_schema: dict[str, Any]
    ) -> dict[str, Any]:
        """Replace a dict-map object schema with an array-of-items schema.

        Transforms ``{type: object, additionalProperties: {schema}}`` into
        ``{type: array, items: {type: object, properties: {key, ...fields}}}``.
        The ``ArrayDictMapResponse`` reverses this when parsing tool arguments.
        """
        # Sanitise the inner value-schema before lifting its properties onto
        # the synthetic items object — preserves Decimal collapse, pattern
        # rules, etc. for fields nested inside the dict-map value type.
        sanitised_value = self._sanitise_schema(value_schema)
        if not isinstance(sanitised_value, dict):
            sanitised_value = {}
        value_props = sanitised_value.get("properties", {}) or {}
        value_required = list(sanitised_value.get("required", []) or [])

        # Build items properties: synthetic ``key`` field + sanitised value fields
        items_props: dict[str, Any] = {
            self.KEY_FIELD: {
                "type": "string",
                "description": "The map key identifier.",
            }
        }
        if value_props:
            for prop_name, prop_schema in value_props.items():
                items_props[prop_name] = prop_schema  # already sanitised above
        else:
            # Scalar-valued dict-map (e.g. ``Dict[str, int]``): the value schema
            # has no ``properties`` to lift onto the item. Without a synthetic
            # value field the scalar is dropped and the model has nowhere to
            # emit it — observed live as the model inventing a ``{"value": N}``
            # wrapper of its own. Carry the scalar schema under ``VALUE_FIELD``;
            # ``ArrayDictMapResponse`` unwraps it back to the bare scalar.
            items_props[self.VALUE_FIELD] = (
                sanitised_value if isinstance(sanitised_value, dict) else {}
            )
            value_required = [self.VALUE_FIELD]

        items_schema: dict[str, Any] = {
            "type": "object",
            "properties": items_props,
            "required": [self.KEY_FIELD] + value_required,
        }

        # Override type → array, set items, keep description
        result["type"] = "array"
        result["items"] = items_schema
        # Remove object-specific keys that don't apply to arrays
        result.pop("properties", None)
        result.pop("required", None)

        # Enrich description so the model knows the wire shape
        existing_desc = result.get("description", "")
        if existing_desc:
            result["description"] = (
                f"{existing_desc} "
                f"Provide as a JSON array of objects, each with a '{self.KEY_FIELD}' "
                f"field as the identifier."
            )

        return result

    # ------------------------------------------------------------------
    # Structural-invariant validator (loud-fail)
    # ------------------------------------------------------------------

    def _validate_invariants(self, tools: list[dict[str, Any]]) -> None:
        """Walk every sanitised tool and raise on structural inconsistency.

        Two invariants:

        1. ``set(required) ⊆ set(properties.keys())`` for every object
           schema. Any deviation means the model cannot satisfy the schema —
           either a sanitiser bug, or an upstream-broken input.
        2. No RE2-incompatible regex remains anywhere. Catches the case
           where Decimal collapse missed a non-canonical ``anyOf`` shape.
        """
        for idx, tool in enumerate(tools):
            params = tool.get("function", {}).get("parameters") if isinstance(tool, dict) else None
            if not isinstance(params, dict):
                continue
            tool_name = tool.get("function", {}).get("name", f"<tool[{idx}]>")
            self._validate_node(params, f"{tool_name}.parameters")
        if self.strip_re2_incompatible_patterns:
            # Only enforce the RE2-safety post-condition when the
            # subclass is the one stripping. Providers that opt out
            # (Gemini) intentionally ship the lookaround / backref —
            # the assertion would false-positive on them.
            self._validate_re2_safe(tools)

    @classmethod
    def _validate_node(cls, node: Any, path: str) -> None:
        if isinstance(node, list):
            for i, item in enumerate(node):
                cls._validate_node(item, f"{path}[{i}]")
            return
        if not isinstance(node, dict):
            return

        # Object-shape invariant: required ⊆ properties.keys()
        props = node.get("properties")
        required = node.get("required")
        if isinstance(props, dict) and isinstance(required, list):
            prop_names = set(props.keys())
            missing = [r for r in required if r not in prop_names]
            if missing:
                raise SchemaInvariantError(
                    f"At {path}: required field(s) {missing!r} are not declared "
                    f"in properties (declared properties: "
                    f"{sorted(prop_names)!r}). This typically means a property "
                    "name was confused with a JSON-Schema metadata key, or the "
                    "input schema itself is broken. The sanitiser refuses to "
                    "ship a schema the model cannot satisfy."
                )

        # Recurse into all sub-schemas so nested invariants are checked too.
        for key, value in node.items():
            if key in _PROPERTY_MAP_KEYS and isinstance(value, dict):
                for prop_name, prop_schema in value.items():
                    cls._validate_node(prop_schema, f"{path}.{key}.{prop_name}")
            elif key in _SCHEMA_LIST_KEYS and isinstance(value, list):
                for i, branch in enumerate(value):
                    cls._validate_node(branch, f"{path}.{key}[{i}]")
            elif key in _SCHEMA_VALUED_KEYS:
                cls._validate_node(value, f"{path}.{key}")
            elif key == "additionalProperties" and isinstance(value, dict):
                cls._validate_node(value, f"{path}.additionalProperties")

    @staticmethod
    def _validate_re2_safe(tools: list[dict[str, Any]]) -> None:
        """Surface any RE2-incompatible construct that survived sanitisation
        loudly rather than ship a poisoned schema that would 500 on
        GPT-5-family / xAI / Qwen-strict providers.
        """
        serialised = json.dumps(tools)
        match = _RE2_INCOMPATIBLE.search(serialised)
        if match is None:
            return
        start, end = match.start(), match.end()
        context = serialised[max(0, start - 60) : min(len(serialised), end + 60)]
        raise SchemaInvariantError(
            "StrictSchema post-condition failed: RE2-incompatible regex "
            f"construct {match.group()!r} survived sanitisation at offset "
            f"{start}. Context: …{context}…"
        )


class GeminiSchema(StrictSchema):
    """Schema sanitiser tuned for Google Gemini via OpenRouter.

    Gemini's tool spec is a JSON-Schema *subset* — it does not
    document support for ``$defs`` / ``$ref`` or ``oneOf`` +
    ``discriminator``. When these constructs appear in the tool
    schema, Gemini ignores the declared property names inside the
    unsupported construct and emits arbitrary description-derived
    English names instead (verified live 2026-05-20: registered
    ``qty`` → emitted ``quantity``; registered ``subject`` →
    emitted ``title``).

    This subclass:

    * Inherits ``StrictSchema``'s ``$ref`` inlining, dict-map →
      array conversion, Decimal collapse, and RE2-incompatible
      pattern strip.
    * Sets ``flatten_oneof_discriminator = True`` so Pydantic
      ``Annotated[..., Field(discriminator='kind')]`` (which emits
      ``oneOf`` + ``discriminator``) is collapsed into a single
      object schema unioning every branch's properties.
      **Bare ``Union[A, B]``** (Pydantic emits ``anyOf`` without
      ``discriminator``) is left untouched — Gemini handles inline
      ``anyOf`` branches correctly at the field-name level, and
      flattening it caused a 40 % → 0 % logistics domain regression.
    * Sets ``strip_parameters_root_description = False`` so the
      redundant Pydantic class-docstring at the parameters root
      (``"Input model for X tool."``) survives. Stripping it
      correlated with a ~10 pp drop on flat tools
      (``d365_api_create_case`` / ``custom_listing_id`` regression). The text is
      semantically redundant but apparently anchors Gemini's
      optional-field selection.
    * Sets ``strip_re2_incompatible_patterns = False`` so the
      Pydantic-emitted RE2-incompatible pattern values (e.g. the
      Decimal-string idiom ``"^(?!^[-+.]*$)[+-]?0*\\d*\\.?\\d*$"``)
      reach Gemini intact. Gemini doesn't enforce RE2; the pattern
      is just a format hint. Stripping it correlated with a
      50 % → 10 % travel_marketplace regression for Gemini 3.5
      Flash, concentrated on the
      7 Pydantic-Decimal-string patterns the domain happens to
      carry on its toolset.
    """

    flatten_oneof_discriminator = True
    strip_parameters_root_description = False
    strip_re2_incompatible_patterns = False
