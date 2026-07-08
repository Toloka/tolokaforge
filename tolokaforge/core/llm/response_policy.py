"""Tool-call argument post-processing.

Each :class:`ResponsePolicy` reverses provider-specific quirks in the
emitted tool-call arguments *after* litellm parsed them:

- :class:`StandardResponse` — no transformation.
- :class:`UnwrapInputResponse` — strip the Nova/Bedrock ``{input: {...}}``
  wrapper.
- :class:`JsonCoerceResponse` — JSON-decode stringified container arguments
  back to native ``list`` / ``dict`` values, and (when given the post-
  sanitised tool schema) coerce ``''`` → ``[]`` / ``''`` → ``{}`` for
  parameters declared as ``array`` / ``object``. Open-weights models
  (Qwen, Kimi, …) occasionally serialise array / object parameters as
  JSON-encoded strings or send empty strings for empty containers; this
  policy recovers the native shape the tool implementation expects.
- :class:`ArrayDictMapResponse` — composes JSON-string coercion with the
  :class:`~tolokaforge.core.llm.schema_sanitizer.StrictSchema` array →
  ``Dict[str, T]`` pivot so that tool implementations receive the
  ``Dict[str, T]`` shape they declared.
- :class:`JsonRecursiveCoerceResponse` — recursive, tags-site-scoped variant
  of :class:`JsonCoerceResponse` for the MiniMax-M3 ``tags`` corruption.
- :class:`ItemRecursiveUnwrapResponse` — unwraps the MiniMax-M3 XML
  repeated-element ``{"item": X}`` artefact into a native list, scoped to the
  same ``tags`` sites.
- :class:`MinimaxM3TagRecoveryResponse` — the composite the ``minimax`` preset
  wires in: ``JsonRecursiveCoerce`` then ``ItemRecursiveUnwrap``.

All policies accept an optional ``param_types`` keyword argument: a
``Mapping[str, str]`` from root-level parameter name to its JSON-Schema
``type`` (after sanitisation). When supplied, schema-aware recovery
(empty-container coercion) fires; when omitted, only schema-agnostic
recovery runs (JSON-string decode), and other arguments pass through
unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from tolokaforge.core.llm.schema_sanitizer import StrictSchema

__all__ = [
    "ResponsePolicy",
    "StandardResponse",
    "UnwrapInputResponse",
    "JsonCoerceResponse",
    "ArrayDictMapResponse",
    "JsonRecursiveCoerceResponse",
    "ItemRecursiveUnwrapResponse",
    "MinimaxM3TagRecoveryResponse",
]

#: Declared-array ``tags`` sites the MiniMax-M3 recursive recovery policies are
#: scoped to: the field ``tags`` directly under an ``updates`` parent
#: (``zendesk_update_item``) or an ``item`` parent (``zendesk_create_item``).
#: Each entry is a ``(parent_param, field)`` pair. Scoping is mandatory: a
#: schema-agnostic empty-string → ``[]`` coercion was proven net-harmful — on
#: MiniMax-M2.7 it produced false-positive scalar corruptions (``''`` on
#: ``resolution_category__c`` / ``employee_id`` / ``keyword``). The allowlist
#: ties the recovery to the only paths where ``tags`` is a declared array
#: inside the schemaless ``additionalProperties: true`` ``updates`` / ``item``
#: object, so the empty-string guard can never fire on a scalar field.
ARRAY_SITES: frozenset[tuple[str, str]] = frozenset({("updates", "tags"), ("item", "tags")})


def _coerce_empty_containers(
    arguments: dict[str, Any],
    param_types: Mapping[str, str] | None,
) -> dict[str, Any]:
    """Coerce ``''`` → ``[]`` / ``''`` → ``{}`` based on declared param types.

    Schema-aware recovery for the qwen ``equipment: ''`` failure mode (post-PR-#88
    diagnosis): the model occasionally emits an empty string for what the
    schema declares as an array or object. The receiving tool then rejects
    with ``Input should be a valid list`` / ``valid dictionary``.

    Constraints by design:

    * No coercion when ``param_types`` is ``None`` — without schema info
      we cannot tell ``''`` for ``string`` (valid) from ``''`` for ``array``
      (invalid). Pass through and surface the validation error downstream.
    * No coercion for ``string`` (``''`` is a valid string value).
    * No coercion for non-string values (``None``, integers, etc).
    * Unknown parameter names in ``param_types`` are tolerated (defensive).
    * ``"dict_map"`` (``StrictSchema``-converted dict-map → array shape)
      coerces to ``{}`` — the receiving tool's Pydantic validator
      expects ``Dict[str, T]`` even though the wire-level sanitised
      schema declares ``array``.
    """
    if not param_types or not isinstance(arguments, dict):
        return arguments
    out = dict(arguments)
    for key, value in arguments.items():
        if value != "":
            continue
        declared = param_types.get(key)
        if declared == "array":
            out[key] = []
        elif declared in ("object", "dict_map"):
            out[key] = {}
    return out


def _coerce_json_strings(arguments: dict[str, Any]) -> dict[str, Any]:
    """Decode stringified JSON arrays / objects back to native values.

    Heuristic: a value is decoded only when it is a ``str`` whose first
    non-whitespace character is ``[`` or ``{`` *and* ``json.loads`` returns
    a ``list`` or ``dict``. Strings that don't look like containers,
    invalid JSON, and non-string values are passed through unchanged. We
    deliberately do *not* promote scalar JSON literals (``"42"`` → 42)
    because string IDs are common and would silently corrupt.
    """
    if not isinstance(arguments, dict):
        return arguments
    out = dict(arguments)
    for key, value in arguments.items():
        if not isinstance(value, str):
            continue
        stripped = value.lstrip()
        if not stripped or stripped[0] not in "[{":
            continue
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            continue
        if isinstance(decoded, (list, dict)):
            out[key] = decoded
    return out


@runtime_checkable
class ResponsePolicy(Protocol):
    """Post-processes tool call arguments from model response.

    Implementations accept an optional ``param_types`` mapping (root-level
    parameter name → JSON-Schema ``type``) so schema-aware recovery (empty-
    container coercion) can fire. When omitted, only schema-agnostic
    recovery runs.
    """

    def parse_arguments(
        self,
        arguments: dict[str, Any],
        *,
        param_types: Mapping[str, str] | None = None,
    ) -> dict[str, Any]: ...


class StandardResponse:
    """No transformation — arguments are used as-is.

    The ``param_types`` keyword is accepted for Protocol compliance but
    ignored: this policy is the explicit no-op for providers whose tool
    calls round-trip cleanly without recovery.
    """

    def parse_arguments(
        self,
        arguments: dict[str, Any],
        *,
        param_types: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        del param_types  # explicitly unused — see class docstring
        return arguments


class UnwrapInputResponse:
    """Unwrap Nova/Bedrock ``{input: {actual_args}}`` wrapper."""

    def parse_arguments(
        self,
        arguments: dict[str, Any],
        *,
        param_types: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        del param_types
        if isinstance(arguments, dict) and "input" in arguments and len(arguments) == 1:
            inner = arguments["input"]
            if isinstance(inner, dict):
                return inner
        return arguments


class JsonCoerceResponse:
    """Recover stringified JSON arrays / objects in tool-call arguments.

    **Recovery, not transformation.** This policy is *defence against a
    model-side quirk* — it does not reshape arguments the model emitted
    correctly. Native dicts / lists pass through unchanged.

    Why this is needed (Qwen 3.x, Kimi, MiniMax, Grok-non-strict):

    * The wire schema is **dict-shape**: ``PassthroughSchema.sanitize`` is
      identity for these presets, so the model sees
      ``additionalProperties: {schema}`` for every ``Dict[str, T]``
      parameter — exactly what its training expects.
    * The system prompt is **dict-shape**:
      :class:`~tolokaforge.core.llm.prompt_policy.DictMapHints` injects an
      explicit ``{"key": {"field": ...}}`` example.
    * The task author's documentation is **dict-shape**.

    All three independent surfaces agree. Despite this, these models'
    function-calling adapters occasionally serialise container values as
    JSON-encoded strings::

        {"lines": "{\\"SKU-A\\": {\\"qty\\": 1}}"}     # what the model emitted
        {"lines": {"SKU-A": {"qty": 1}}}             # what the tool expects

    The stringification is uncontrollable from our side — it is not a
    schema bug, hint bug, prompt bug, or transport bug (verified by
    reading the persisted ``trajectory.yaml`` ``system_prompt`` field
    and confirming the wire shape). On ``tau_manufacturing_v2`` Qwen
    emitted 252 native dicts, 806 stringified dicts, and 0 array shapes
    — we cannot make the model stop stringifying, so we recover.

    Decoding rule: ``json.loads`` runs only when ``value`` is a ``str``
    whose first non-whitespace character is ``[`` or ``{`` AND the
    parsed result is a ``list`` or ``dict``. Plain strings (``order_id``,
    free-text), scalar-shaped strings (``"42"`` is left alone — IDs are
    often all-digits), and parse failures are passed through unchanged.

    See [the post-fix diagnosis](../../../plans/eval_tau_manufacturing_v2_post_fix_diagnosis.md)
    for the empirical justification.

    Empty-container coercion (schema-aware) runs first when ``param_types``
    is supplied: ``''`` → ``[]`` for ``array`` params, ``''`` → ``{}`` for
    ``object`` params. This catches the post-PR-#88 ``equipment: ''`` shape
    where the model emits an empty string for what the schema declares as
    a container. See
    [the post-PR-#88 analysis](../../../plans/eval_tau_manufacturing_v2_post_pr88_analysis.md)
    Recommendation 2 for the empirical justification.
    """

    def parse_arguments(
        self,
        arguments: dict[str, Any],
        *,
        param_types: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        # Schema-aware empty-container coercion runs BEFORE JSON-string
        # decoding so empty strings never reach ``json.loads``.
        coerced = _coerce_empty_containers(arguments, param_types)
        return _coerce_json_strings(coerced)


class ArrayDictMapResponse:
    """JSON-coerce, then reverse the ``StrictSchema`` dict-map→array conversion.

    Two-stage pipeline:

    1. :func:`_coerce_json_strings` — recover stringified containers.
    2. ``additionalProperties → array of {key, …}`` reverse pivot:
       ``StrictSchema`` converts ``additionalProperties: {schema}``
       parameters into ``type: array`` with ``items`` containing a synthetic
       ``key`` field. This policy converts those arrays back into the
       ``Dict[str, T]`` format that tool implementations expect.

    Example::

        # LLM produces (array format):
        {"lines": [{"key": "SKU-001", "qty": 10}]}

        # Converted to (dict format for tool):
        {"lines": {"SKU-001": {"qty": 10}}}
    """

    KEY_FIELD = StrictSchema.KEY_FIELD

    def parse_arguments(
        self,
        arguments: dict[str, Any],
        *,
        param_types: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        # Same recovery order as JsonCoerceResponse: empty-container coercion
        # → JSON-string decode → array → dict pivot.
        coerced = _coerce_empty_containers(arguments, param_types)
        result = dict(_coerce_json_strings(coerced))
        for param_name, value in list(result.items()):
            if isinstance(value, list):
                if not value:
                    # Empty array → empty dict ONLY when the param was
                    # originally a dict-map (``StrictSchema`` converted
                    # ``additionalProperties: {schema}`` to
                    # ``type: array`` on the wire). The receiving tool's
                    # Pydantic validator still expects ``Dict[str, T]``,
                    # so without this pivot the tool rejects with the
                    # confusing ``"Input should be a valid dictionary"``
                    # error instead of any meaningful tool-level
                    # constraint message (verified live 2026-05-20 on
                    # ``output/new_collected/tau_manufacturing /
                    # gemini_35_flash`` — empty ``lines`` on
                    # ``create_order``). For genuine ``array`` params
                    # (e.g. ``equipment: list[str]``) we leave the empty
                    # list intact — the receiver wants ``[]``, not
                    # ``{}``. The marker comes from
                    # :func:`_resolve_declared_type`'s ``"dict_map"``
                    # output: with no schema info we can't tell the two
                    # apart, so we conservatively leave the array as-is.
                    if param_types and param_types.get(param_name) == "dict_map":
                        result[param_name] = {}
                elif all(isinstance(item, dict) and self.KEY_FIELD in item for item in value):
                    # Array of items with key field → convert to dict
                    dict_map: dict[str, Any] = {}
                    for item in value:
                        item_copy = dict(item)
                        key = str(item_copy.pop(self.KEY_FIELD))
                        dict_map[key] = item_copy
                    result[param_name] = dict_map
            elif isinstance(value, dict) and self.KEY_FIELD in value:
                # Single dict with key field → convert to single-entry dict.
                # This handles models that produce a flat dict instead of a 1-item array.
                value_copy = dict(value)
                key = str(value_copy.pop(self.KEY_FIELD))
                result[param_name] = {key: value_copy}
        return result


# ---------------------------------------------------------------------------
# MiniMax-M3 ``tags`` recovery (recursive, tags-site-scoped)
# ---------------------------------------------------------------------------
#
# MiniMax-M3 routes its tool calls through a provider-side XML → JSON
# conversion that systematically corrupts the ``tags`` argument: 2505/2505
# airlines occurrences are malformed. The two dominant shapes are recovered by
# the two policies below; both are scoped to :data:`ARRAY_SITES` only so the
# recovery can never touch a scalar field elsewhere in the call. The shipped
# :class:`JsonCoerceResponse` / :class:`ArrayDictMapResponse` helpers
# (:func:`_coerce_json_strings`, :func:`_coerce_empty_containers`) are reused;
# the new behaviour is *recursion* into the ``updates`` / ``item`` parent plus
# the site allowlist that bounds it.


def _recover_json_string_at_tags_site(value: Any) -> Any:
    """Recover one ``tags`` value's stringification artefacts.

    Two shapes, both proven on the M3 airlines census:

    * stringified JSON list (``'["a","b"]'``) → the native ``list`` via
      :func:`_coerce_json_strings` (which only promotes ``[`` / ``{`` strings
      whose ``json.loads`` yields a container — scalar strings are never
      promoted, by design).
    * empty string (``''``) → ``[]``. This is the empty-string → ``[]``
      coercion the spec restricts to declared-array ``tags`` sites: the caller
      only invokes this helper for paths in :data:`ARRAY_SITES`, so it can
      never fire on a scalar field (the M2.7 false-positive class).

    Any other value (real list, ``None``, scalar string, number) passes
    through unchanged.

    Note: the site allowlist is keyed on the field *name* (``tags``), not a
    verified declared type. It assumes ``updates.tags`` / ``item.tags`` is
    always a declared array (true for the current mock-tools domains); a
    future domain declaring a *scalar* field literally named ``tags`` under
    one of these parents would be mis-coerced.
    """
    if value == "" and isinstance(value, str):
        return []
    # Reuse the shipped JSON-string decoder by wrapping in a single-key dict;
    # it never promotes scalar strings and leaves non-string values untouched.
    return _coerce_json_strings({"tags": value})["tags"]


# Bound the ``{"item": {"item": ...}}`` unwrap recursion. The observed M3
# census tops out at one nesting level; greater depth only comes from
# pathological / adversarial input, so past the cap the value is returned
# unchanged (refuse to recover rather than raise RecursionError at the
# tool-call assembly site).
_MAX_UNWRAP_DEPTH = 64


def _unwrap_item_value(value: Any, _depth: int = 0) -> Any:
    """Recursively unwrap the ``{"item": X}`` XML repeated-element artefact.

    Rule (per spec): a single-key dict ``{"item": X}`` normalises to a list —
    recurse into ``X`` first (so ``{"item": {"item": "a"}}`` flattens to
    ``["a"]``), then return ``X`` as-is when it is already a list, else
    ``[X]``. Multi-key dicts are **left unchanged** (no guessing which key is
    the real value). Non-dict values pass through untouched.
    """
    if not isinstance(value, dict) or set(value.keys()) != {"item"}:
        return value
    if _depth >= _MAX_UNWRAP_DEPTH:
        return value
    inner = _unwrap_item_value(value["item"], _depth + 1)
    return inner if isinstance(inner, list) else [inner]


class JsonRecursiveCoerceResponse:
    """Recursive, tags-site-scoped variant of :class:`JsonCoerceResponse`.

    MiniMax-M3's XML → JSON tool-call conversion serialises the ``tags`` array
    as a JSON-encoded *string* (``'["receipt-issued"]'``, 550/2505 ≈ 22 %) or
    as an empty string (``''``, 27/2505 ≈ 1 %). Unlike the shipped flat
    :class:`JsonCoerceResponse`, the corrupt ``tags`` lives **one level deep**
    inside the schemaless ``additionalProperties: true`` ``updates`` / ``item``
    object, so recovery has to recurse into that parent.

    Scope (load-bearing): only the ``(parent, field)`` paths in
    :data:`ARRAY_SITES` are touched — ``updates.tags`` and ``item.tags``. The
    empty-string → ``[]`` coercion is therefore tied to declared-array sites
    and can never fire on a scalar field. A schema-agnostic empty-string → ``[]``
    was proven net-harmful: on MiniMax-M2.7 it corrupted scalar fields such as
    ``resolution_category__c`` / ``employee_id`` / ``keyword``.

    Never promotes scalar strings (delegates to :func:`_coerce_json_strings`),
    never touches ``None`` / ``null`` (passes straight through).
    """

    def parse_arguments(
        self,
        arguments: dict[str, Any],
        *,
        param_types: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        del param_types  # scoping comes from ARRAY_SITES, not declared types
        if not isinstance(arguments, dict):
            return arguments
        out = dict(arguments)
        for parent, field in ARRAY_SITES:
            container = out.get(parent)
            if not isinstance(container, dict) or field not in container:
                continue
            new_container = dict(container)
            new_container[field] = _recover_json_string_at_tags_site(container[field])
            out[parent] = new_container
        return out


class ItemRecursiveUnwrapResponse:
    """Unwrap the MiniMax-M3 ``{"item": X}`` XML repeated-element artefact.

    The provider's XML → JSON conversion renders a repeated XML element
    (``<tags><item>a</item><item>b</item></tags>``) as a single-key dict keyed
    on ``item`` rather than a JSON array — 1901/2505 ≈ 76 % of corrupt M3
    airlines ``tags``, the single largest shape. This policy normalises
    ``{"item": X}`` to a list (see :func:`_unwrap_item_value`):

    * ``{"item": "receipt-issued"}`` → ``["receipt-issued"]``
    * ``{"item": ["a", "b"]}`` → ``["a", "b"]`` (already a list — kept flat)
    * ``{"item": {"item": "a"}}`` → ``["a"]`` (recurses first, then flattens)

    Multi-key dicts (e.g. ``{"item": "a", "refund-requested": ""}``) are left
    unchanged — there is no safe way to guess which key is the value, so we
    refuse rather than corrupt (AGENTS.md rule #1).

    Scoped to :data:`ARRAY_SITES` (``updates.tags`` / ``item.tags``) only.
    """

    def parse_arguments(
        self,
        arguments: dict[str, Any],
        *,
        param_types: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        del param_types  # scoping comes from ARRAY_SITES, not declared types
        if not isinstance(arguments, dict):
            return arguments
        out = dict(arguments)
        for parent, field in ARRAY_SITES:
            container = out.get(parent)
            if not isinstance(container, dict) or field not in container:
                continue
            new_container = dict(container)
            new_container[field] = _unwrap_item_value(container[field])
            out[parent] = new_container
        return out


class MinimaxM3TagRecoveryResponse:
    """Composite MiniMax-M3 ``tags`` recovery — the ``minimax`` preset's policy.

    A preset has a single ``response_policy`` slot. Following the
    :class:`ArrayDictMapResponse` precedent (one named policy composing two
    independent transforms), this composite chains the two M3 ``tags`` recovery
    stages in order:

    1. :class:`JsonRecursiveCoerceResponse` — stringified-list → ``list`` and
       ``''`` → ``[]`` at the tags sites. Runs first so a stringified list
       becomes a real list before unwrapping, and ``''`` becomes ``[]`` rather
       than being mistaken for anything else.
    2. :class:`ItemRecursiveUnwrapResponse` — ``{"item": X}`` → list at the
       tags sites.

    Both stages are scoped to :data:`ARRAY_SITES`; both are no-ops on a valid
    ``list[str]`` (the order is irrelevant for an already-recovered list, so
    valid tags pass through unchanged — zero false positives). Gated to the
    ``minimax`` preset (``minimax-m3*``) only; other models never see it.
    """

    def __init__(self) -> None:
        self._coerce = JsonRecursiveCoerceResponse()
        self._unwrap = ItemRecursiveUnwrapResponse()

    def parse_arguments(
        self,
        arguments: dict[str, Any],
        *,
        param_types: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        coerced = self._coerce.parse_arguments(arguments, param_types=param_types)
        return self._unwrap.parse_arguments(coerced, param_types=param_types)


class GeminiDictMapResponse(ArrayDictMapResponse):
    """``ArrayDictMapResponse`` extended for two dict-map shapes it misses.

    The base policy pivots ``array of {key, …}`` → ``Dict[str, T]`` only at
    the *top level* of the argument object and leaves the map value objects
    intact. Two live-observed Gemini 3.1 Pro shapes (2026-07-07 observe
    artifact) fall through:

    1. **Dict-map nested inside an object param**
       (``test_variant_dict_map[nested_in_object]``): the pivot never reaches
       the inner array, so the tool receives ``got list`` instead of a dict.
       This subclass pivots recursively at every depth.
    2. **Scalar-valued dict-map**
       (``test_variant_dict_map[scalar_values]``): paired with
       :class:`~tolokaforge.core.llm.schema_sanitizer.GeminiRecursiveSchema`,
       which wraps the scalar value in a synthetic ``{value: <scalar>}``
       object so it survives the schema-side array pivot. This policy unwraps
       that ``{value: …}`` wrapper back to the native scalar. The unwrap is
       narrow — it fires only on a single-key ``{value: <scalar>}`` dict, so
       object-valued maps (``{qty, price, …}``) pass through unchanged.
    """

    #: Synthetic scalar-value wrapper key — paired with
    #: ``GeminiRecursiveSchema.VALUE_FIELD``.
    VALUE_FIELD = "value"

    def parse_arguments(
        self,
        arguments: dict[str, Any],
        *,
        param_types: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        # Base recovery first: JSON-string decode, empty-container coercion,
        # and the top-level array → dict pivot.
        result = super().parse_arguments(arguments, param_types=param_types)
        out: dict[str, Any] = {}
        for name, value in result.items():
            top_is_map = bool(param_types) and param_types.get(name) == "dict_map"
            out[name] = self._transform(value, in_map=top_is_map)
        return out

    def _transform(self, value: Any, in_map: bool = False) -> Any:
        """Recurse the array → dict pivot and unwrap scalar value wrappers."""
        if isinstance(value, list):
            if value and all(isinstance(item, dict) and self.KEY_FIELD in item for item in value):
                pivoted: dict[str, Any] = {}
                for item in value:
                    inner = {k: self._transform(v) for k, v in item.items() if k != self.KEY_FIELD}
                    pivoted[str(item[self.KEY_FIELD])] = self._unwrap_value(inner)
                return pivoted
            return [self._transform(v) for v in value]
        if isinstance(value, dict):
            if in_map:
                # This dict is itself a map (top-level param typed dict_map):
                # unwrap each value's scalar wrapper.
                return {k: self._unwrap_value(self._transform(v)) for k, v in value.items()}
            return {k: self._transform(v) for k, v in value.items()}
        return value

    @classmethod
    def _unwrap_value(cls, value: Any) -> Any:
        """``{value: <scalar>}`` → ``<scalar>``; everything else unchanged."""
        if isinstance(value, dict) and set(value.keys()) == {cls.VALUE_FIELD}:
            inner = value[cls.VALUE_FIELD]
            if not isinstance(inner, (dict, list)):
                return inner
        return value
