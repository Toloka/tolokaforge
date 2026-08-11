"""Per-model policy subclasses for the Google Gemini family.

Three classes ship in this module:

* :class:`GeminiSchema` — schema sanitiser tuned for the Gemini
  JSON-Schema subset (``$defs`` inlining, ``oneOf`` + ``discriminator``
  flattening, Pydantic Decimal ``anyOf`` collapse retained, RE2-pattern
  strip disabled).
* :class:`GeminiRecursiveSchema` — Gemini sanitiser tolerating cyclic
  ``$ref`` by substituting a permissive open-object schema at the point
  of re-entry, plus scalar dict-map value carriage.
* :class:`ScalarArrayDictMapResponse` — response policy that pairs with
  :class:`GeminiRecursiveSchema`'s scalar dict-map carriage by unwrapping
  the synthetic ``value`` field on the model's emitted arguments.

Registered with the engine via the ``tolokaforge.policies`` entry-point
group (see :mod:`tolokaforge.core.model_data.load_policy_registrations`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tolokaforge.core.llm.response_policy import ArrayDictMapResponse
from tolokaforge.core.llm.schema_sanitizer import StrictSchema

__all__ = [
    "GeminiRecursiveSchema",
    "GeminiSchema",
    "ScalarArrayDictMapResponse",
]


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


class GeminiRecursiveSchema(GeminiSchema):
    """Gemini sanitiser that tolerates **self-referential** (cyclic) ``$ref``.

    ``StrictSchema.inline_refs_in_tool`` fully inlines every ``$ref`` and raises
    ``"$ref resolution exceeded depth 16"`` the moment a ``$def`` references
    itself (a Pydantic recursive model such as ``TreeNode.children:
    list[TreeNode]`` compiles to exactly this cyclic ``$ref``). The raise
    fires in-engine *before* the request is sent, so the whole trial fails
    with a sanitiser ``ValueError`` rather than the model ever seeing the
    tool. Observed on ``google/gemini-3.5-flash`` as ``test_recursive_ref_
    tool_call`` failing 0/15 on all four shapes (simple / deep_chain /
    wide_tree / nested_in_object).

    This subclass inlines every *non-cyclic* ``$ref`` exactly as the parent
    does, but breaks a genuine cycle by substituting a permissive open-object
    schema (``{"type": "object", "additionalProperties": true}``) at the point
    of re-entry. The declared schema is thus finite and Gemini receives a valid
    tool spec; the *receiving* Pydantic validator still accepts an
    arbitrary-depth nested tree, so a model that emits the full recursion
    passes. Cycle detection is by def-name on the active resolution stack — a
    diamond (the same def referenced twice on *different* branches) still
    inlines on each branch; only a ref that re-enters a def already being
    resolved on the current path is pruned.
    """

    #: Pair the recursive-ref tolerance with scalar dict-map value carriage —
    #: both are Gemini-observed schema-loss surfaces on the same route.
    carry_scalar_dict_map_value = True

    @classmethod
    def inline_refs_in_tool(cls, tool: Any) -> Any:
        """Override of :meth:`StrictSchema.inline_refs_in_tool` — inlines every
        non-cyclic ``$ref`` exactly as the base does, but substitutes a
        permissive open-object schema at any point of cyclic re-entry.
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
        resolved_params = cls._inline_refs_cycle_tolerant(params, defs, active=())
        if isinstance(resolved_params, dict):
            resolved_params.pop("$defs", None)
        new_func = dict(func)
        new_func["parameters"] = resolved_params
        new_tool = dict(tool)
        new_tool["function"] = new_func
        return new_tool

    #: Substituted at the point a cyclic ``$ref`` re-enters a def already on
    #: the active resolution stack. Permissive so the model can still emit the
    #: recursive subtree; the receiving Pydantic validator enforces the shape.
    _CYCLE_STUB: dict[str, Any] = {"type": "object", "additionalProperties": True}

    @classmethod
    def _inline_refs_cycle_tolerant(
        cls, schema: Any, defs: dict[str, Any], active: tuple[str, ...]
    ) -> Any:
        """Inline ``$ref`` like the parent, but prune a ref that re-enters a
        def already on the ``active`` resolution stack (a genuine cycle)
        rather than recursing until the depth cap raises.
        """
        if isinstance(schema, list):
            return [cls._inline_refs_cycle_tolerant(item, defs, active) for item in schema]
        if not isinstance(schema, dict):
            return schema
        ref = schema.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            target_name = ref.removeprefix("#/$defs/")
            if target_name in active:
                # Cyclic self-reference — stop inlining, emit a permissive
                # open object so the wire schema stays finite.
                return dict(cls._CYCLE_STUB)
            target = defs.get(target_name)
            if target is None:
                raise ValueError(
                    f"StrictSchema: $ref {ref!r} points to a missing "
                    f"$defs entry. Available: {sorted(defs.keys())!r}"
                )
            resolved = cls._inline_refs_cycle_tolerant(target, defs, active + (target_name,))
            siblings = {k: v for k, v in schema.items() if k != "$ref"}
            if not siblings:
                return resolved
            if isinstance(resolved, dict):
                merged = dict(resolved)
                for k, v in siblings.items():
                    merged[k] = cls._inline_refs_cycle_tolerant(v, defs, active)
                return merged
            return resolved
        return {k: cls._inline_refs_cycle_tolerant(v, defs, active) for k, v in schema.items()}


class ScalarArrayDictMapResponse(ArrayDictMapResponse):
    """:class:`ArrayDictMapResponse` plus a **scalar** dict-map value unwrap.

    Pairs with :class:`GeminiRecursiveSchema`'s
    ``carry_scalar_dict_map_value``: a ``Dict[str, int]`` / ``Dict[str, str]``
    parameter is sent to the model as ``[{key, value}]`` (the scalar carried
    under the synthetic ``value`` field). The inherited array → dict pivot turns
    ``[{"key": "SKU-A", "value": 10}]`` into ``{"SKU-A": {"value": 10}}`` — one
    step short of the ``{"SKU-A": 10}`` the tool's ``Dict[str, int]`` validator
    wants. This subclass finishes the job by unwrapping every single-field
    ``{"value": X}`` map entry back to the bare scalar ``X``.

    The same unwrap also recovers the model's *native* wrapper quirk: a model
    that ignores the array shape and emits ``{"SKU-A": {"value": 10}}`` directly
    (observed on ``google/gemini-3.5-flash``, 15/15) lands on the identical
    post-pivot shape, so both the array path and the native-wrapper path
    converge here.

    Scoped to ``dict_map`` params (via ``param_types``) so a genuine
    object-valued map that happens to carry a field literally named ``value``
    is never touched — and even within a dict-map, only a *single-key*
    ``{"value": …}`` entry is unwrapped, so a multi-field value object is left
    intact.

    **Nested dict-maps.** The inherited pivot fires only at the root; a dict-map
    *nested inside an object* param (``order.lines`` where ``order`` is an
    object and ``lines`` is the ``Dict[str, T]``) is converted to the array
    shape on the wire like any other dict-map, but the root-only pivot never
    reaches it — the model then emits ``order.lines`` as the value-less array
    and the tool rejects it (observed on ``google/gemini-3.5-flash``
    ``dict_map__nested_in_object``, 0/15). This subclass recurses one level into
    each ``object`` param and pivots any nested list whose items *all* carry the
    synthetic ``key`` field back to a dict (and applies the same scalar-``value``
    unwrap). The recursion is bounded to ``object`` params and keyed on the
    ``key``-field item signature — the same schema-loss shape the sanitiser
    produces — so it is a no-op on a genuine ``list[X]`` whose items don't carry
    ``key``. Its exact reach across domains is data-bound (which nested fields
    are dict-maps lives in the domain tool schemas, not the probe), so this arm
    is flagged for a human domain-scope check before merge.
    """

    def parse_arguments(
        self,
        arguments: dict[str, Any],
        *,
        param_types: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        result = super().parse_arguments(arguments, param_types=param_types)
        if not param_types:
            return result
        out = dict(result)
        for param_name, value in list(out.items()):
            declared = param_types.get(param_name)
            if declared == "dict_map" and isinstance(value, dict):
                out[param_name] = self._unwrap_scalar_values(value)
            elif declared == "object" and isinstance(value, dict):
                out[param_name] = self._pivot_nested_dict_maps(value)
        return out

    @classmethod
    def _unwrap_scalar_values(cls, mapping: dict[str, Any]) -> dict[str, Any]:
        """Unwrap single-field ``{"value": X}`` map entries to the bare scalar."""
        unwrapped: dict[str, Any] = {}
        for k, v in mapping.items():
            if isinstance(v, dict) and set(v.keys()) == {StrictSchema.VALUE_FIELD}:
                unwrapped[k] = v[StrictSchema.VALUE_FIELD]
            else:
                unwrapped[k] = v
        return unwrapped

    @classmethod
    def _pivot_nested_dict_maps(cls, obj: dict[str, Any]) -> dict[str, Any]:
        """Recurse an ``object`` param, pivoting nested dict-map-shaped arrays
        (``[{key, …}]``) back to ``Dict[str, T]`` and scalar-unwrapping the
        result. Bounded to the ``key``-field item signature so genuine
        ``list[X]`` values pass through untouched.
        """
        out: dict[str, Any] = {}
        for field, value in obj.items():
            if (
                isinstance(value, list)
                and value
                and all(isinstance(item, dict) and cls.KEY_FIELD in item for item in value)
            ):
                pivoted: dict[str, Any] = {}
                for item in value:
                    item_copy = dict(item)
                    key = str(item_copy.pop(cls.KEY_FIELD))
                    pivoted[key] = item_copy
                out[field] = cls._unwrap_scalar_values(pivoted)
            elif isinstance(value, dict):
                # A direct dict field one level in is either a native-wrapper
                # dict-map (``{k: {"value": N}}``) to scalar-unwrap, or a plain
                # nested object. Recovery is BOUNDED to this one level - the
                # depth the observe evidence covers (``order.lines``) - so any
                # deeper subtree passes through untouched. The scalar unwrap is
                # a no-op on multi-field values, so plain objects are safe.
                out[field] = cls._unwrap_scalar_values(value)
            else:
                out[field] = value
        return out
