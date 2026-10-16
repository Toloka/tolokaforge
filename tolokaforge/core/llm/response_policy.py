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

Per-model response subclasses (``ScalarArrayDictMapResponse``,
``MinimaxM3TagRecoveryResponse``, ``JsonRecursiveCoerceResponse``,
``ItemRecursiveUnwrapResponse``) live in
:mod:`tolokaforge_models.policies` and reach the engine's registry via
the ``tolokaforge.policies`` entry-point group.

All policies accept an optional ``param_types`` keyword argument: a
``Mapping[str, str]`` from root-level parameter name to its JSON-Schema
``type`` (after sanitisation). When supplied, schema-aware recovery
(empty-container coercion) fires, and a parameter it declares ``string`` is
never JSON-decoded; when omitted, only schema-agnostic recovery runs
(JSON-string decode), and other arguments pass through unchanged.

Policies that reverse a sanitiser rewrite also accept ``param_schema``: the
tool's **original** (pre-sanitisation) ``parameters`` JSON Schema. It drives
:func:`reverse_dict_map_arrays`, which walks the emitted arguments and the
schema together and undoes ``StrictSchema``'s dict-map → array pivot at every
depth — nested objects, array items, ``Optional[...]`` branches and the values
of other dict-maps — not just at the root. The engine passes it whenever the
bound policy's ``parse_arguments`` accepts the keyword, so a policy written
against the older two-argument contract keeps working.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from tolokaforge.core.llm.schema_sanitizer import StrictSchema, typed_dict_map_value_schema

__all__ = [
    "ResponsePolicy",
    "StandardResponse",
    "UnwrapInputResponse",
    "JsonCoerceResponse",
    "ArrayDictMapResponse",
    "coerce_empty_containers",
    "coerce_json_strings",
    "reverse_dict_map_arrays",
]


def coerce_empty_containers(
    arguments: dict[str, Any],
    param_types: Mapping[str, str] | None,
) -> dict[str, Any]:
    """Coerce ``''`` → ``[]`` / ``''`` → ``{}`` based on declared param types.

    Public API. Stable within the v0.17.x minor series; removal or signature
    change requires a deprecation announcement.

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


def coerce_json_strings(
    arguments: dict[str, Any],
    param_types: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Decode stringified JSON arrays / objects back to native values.

    Public API. Stable within the v0.17.x minor series; removal or signature
    change requires a deprecation announcement.

    Heuristic: a value is decoded only when it is a ``str`` whose first
    non-whitespace character is ``[`` or ``{`` *and* ``json.loads`` returns
    a ``list`` or ``dict``. Strings that don't look like containers,
    invalid JSON, and non-string values are passed through unchanged. We
    deliberately do *not* promote scalar JSON literals (``"42"`` → 42)
    because string IDs are common and would silently corrupt.

    A parameter *param_types* declares as ``string`` is never decoded: it
    carries text the tool parses itself, so handing the tool the decoded object
    breaks every such call. Without *param_types*, or for a parameter it does not
    name, the heuristic applies.
    """
    if not isinstance(arguments, dict):
        return arguments
    out = dict(arguments)
    for key, value in arguments.items():
        if not isinstance(value, str):
            continue
        if param_types is not None and param_types.get(key) == "string":
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
    container coercion) can fire, and an optional ``param_schema`` (the
    tool's original ``parameters`` JSON Schema) so a sanitiser rewrite can be
    reversed at every depth. When both are omitted, only schema-agnostic
    recovery runs.

    The engine passes ``param_schema`` only to a policy whose
    ``parse_arguments`` accepts the keyword, so an implementation written
    against the two-argument contract stays valid.
    """

    def parse_arguments(
        self,
        arguments: dict[str, Any],
        *,
        param_types: Mapping[str, str] | None = None,
        param_schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...


class StandardResponse:
    """No transformation — arguments are used as-is.

    The ``param_types`` / ``param_schema`` keywords are accepted for Protocol
    compliance but ignored: this policy is the explicit no-op for providers
    whose tool calls round-trip cleanly without recovery.
    """

    def parse_arguments(
        self,
        arguments: dict[str, Any],
        *,
        param_types: Mapping[str, str] | None = None,
        param_schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        del param_types, param_schema  # explicitly unused — see class docstring
        return arguments


class UnwrapInputResponse:
    """Unwrap Nova/Bedrock ``{input: {actual_args}}`` wrapper."""

    def parse_arguments(
        self,
        arguments: dict[str, Any],
        *,
        param_types: Mapping[str, str] | None = None,
        param_schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        del param_types, param_schema
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
        param_schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        del param_schema  # nothing to reverse: the wire schema was the original
        # Schema-aware empty-container coercion runs BEFORE JSON-string
        # decoding so empty strings never reach ``json.loads``.
        coerced = coerce_empty_containers(arguments, param_types)
        return coerce_json_strings(coerced, param_types)


class ArrayDictMapResponse:
    """JSON-coerce, then reverse the ``StrictSchema`` dict-map→array conversion.

    Two-stage pipeline:

    1. :func:`coerce_json_strings` — recover stringified containers.
    2. ``additionalProperties → array of {key, …}`` reverse pivot:
       ``StrictSchema`` converts ``additionalProperties: {schema}``
       parameters into ``type: array`` with ``items`` containing a synthetic
       ``key`` field (plus a synthetic ``value`` field for scalar-valued
       maps). This policy converts those arrays back into the ``Dict[str, T]``
       format that tool implementations expect.

    Example::

        # LLM produces (array format):
        {"lines": [{"key": "SKU-001", "qty": 10}]}
        {"path":  [{"key": "issueId", "value": "PROJ-1"}]}

        # Converted to (dict format for tool):
        {"lines": {"SKU-001": {"qty": 10}}}
        {"path":  {"issueId": "PROJ-1"}}

    **Schema-driven, recursive.** With ``param_schema`` (the tool's original
    ``parameters`` schema, which the engine passes on every call) the reverse
    pivot is :func:`reverse_dict_map_arrays`: it walks arguments and schema
    together and fires exactly where the original schema declares a dict-map
    — at the root, inside nested objects (``arguments.path``), inside array
    items, behind ``Optional[...]`` branches and in the values of other
    dict-maps. A genuine ``list[Model]`` whose model happens to have a field
    named ``key`` is never touched, because the schema says it is an array.

    **Without ``param_schema``** (a caller on the older contract) the policy
    falls back to the historical root-level shape heuristic: any root list
    whose items all carry ``key`` is pivoted. Scalar values carried under
    ``value`` are then left wrapped (``{k: {"value": v}}``) — pair the
    heuristic path with a scalar-aware subclass if that matters.
    """

    KEY_FIELD = StrictSchema.KEY_FIELD
    VALUE_FIELD = StrictSchema.VALUE_FIELD

    def parse_arguments(
        self,
        arguments: dict[str, Any],
        *,
        param_types: Mapping[str, str] | None = None,
        param_schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Same recovery order as JsonCoerceResponse: empty-container coercion
        # → JSON-string decode → array → dict pivot.
        coerced = coerce_empty_containers(arguments, param_types)
        result = dict(coerce_json_strings(coerced, param_types))
        if param_schema is not None:
            return reverse_dict_map_arrays(
                result,
                param_schema,
                key_field=self.KEY_FIELD,
                value_field=self.VALUE_FIELD,
            )
        return self._reverse_root_by_shape(result, param_types)

    def _reverse_root_by_shape(
        self, result: dict[str, Any], param_types: Mapping[str, str] | None
    ) -> dict[str, Any]:
        """Historical root-level pivot, keyed on the ``{key, …}`` item shape."""
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
# Schema-driven reverse pivot
# ---------------------------------------------------------------------------

#: Hard stop for the schema walk. The walk is bounded by the *arguments*
#: depth (a cyclic ``$ref`` in the schema cannot loop without a matching
#: nesting in the emitted value), so the cap is a backstop, not a limit any
#: real argument tree approaches.
_REVERSE_MAX_DEPTH = 64

_JSON_TYPE_BY_PY: tuple[tuple[type, str], ...] = (
    (bool, "boolean"),  # before int: ``bool`` is an ``int`` subclass
    (int, "integer"),
    (float, "number"),
    (str, "string"),
    (list, "array"),
    (dict, "object"),
    (type(None), "null"),
)


def reverse_dict_map_arrays(
    arguments: dict[str, Any],
    param_schema: Mapping[str, Any],
    *,
    key_field: str = StrictSchema.KEY_FIELD,
    value_field: str = StrictSchema.VALUE_FIELD,
) -> dict[str, Any]:
    """Undo ``StrictSchema``'s dict-map → array pivot wherever *param_schema*
    declares a dict-map, at any depth.

    Public API. *param_schema* is the tool's **original** ``parameters``
    JSON Schema (``$defs`` / ``$ref`` are resolved on the fly, so a recursive
    model is fine — the walk is bounded by the emitted value, not the
    schema). *arguments* is the model's emitted argument dict after JSON
    coercion.

    Rules, per schema node:

    * ``additionalProperties: {schema}`` (typed dict-map — the same
      definition the sanitiser pivots by, :func:`typed_dict_map_value_schema`,
      so a node that also declares ``properties`` reverses too):

      - a list whose items all carry *key_field* becomes a dict keyed by it;
        each item minus the key is the entry. When the value schema declares
        no property of its own named *value_field* and the remaining item is
        exactly ``{value_field: X}``, the entry is the bare ``X`` — the scalar
        carriage (``Dict[str, str]`` → ``[{key, value}]``) coming back.
      - a dict is the native map shape: kept as a map. An entry that is
        exactly ``{value_field: X}`` is unwrapped to ``X`` **only when** ``X``
        is something the value schema can hold — ``{k: {"value": "v"}}`` on
        a ``Dict[str, str]`` is the wrapper some models emit natively, while
        ``{host: {"value": "token"}}`` on a ``Dict[str, Dict[str, str]]`` is
        a genuine inner map and stays one. An empty list is an empty map.
      - a single flat item (``{key, …}`` with every other field declared by
        the value schema, or exactly ``{key, value}`` for a scalar map with
        ``value`` holding what the value schema declares) becomes a one-entry
        map — some models emit one item instead of a one-item array.
        ``{"key": "abc"}`` alone, or ``{"key": "k", "value": "x"}`` on a
        ``Dict[str, int]``, is a native map. For a ``Dict[str, str]`` the
        shape ``{"key": …, "value": …}`` is inherently ambiguous and is read
        as the item the wire schema asked for.
      - every entry is then walked against the value schema, so a dict-map
        nested in a dict-map value reverses too.

    * ``additionalProperties: true`` / ``{}`` (free-form object): untouched —
      the sanitiser never pivots these.
    * ``properties``: each declared field is walked against its schema;
      undeclared fields pass through.
    * ``items``: each element is walked against the item schema.
    * ``allOf``: every branch applies — the value is walked through each in
      turn, then against the node's own keywords.
    * ``anyOf`` / ``oneOf``: the branch that best matches the value is taken:
      a property with a ``const`` (a Pydantic discriminator) must agree with
      the value, then the branch sharing the most property names with the
      value wins, then the first branch whose JSON type fits. A dict-map
      branch fits the pivoted list and any dict. With no fitting branch the
      value passes through.
    * A ``str`` where the schema declares an object or array (and not a
      string) is JSON-decoded when it parses to a container — the nested
      counterpart of :func:`coerce_json_strings`, which only sees the root.

    Nothing else is changed: a value the schema does not describe is
    returned as emitted.
    """
    if not isinstance(arguments, dict) or not isinstance(param_schema, Mapping):
        return arguments
    walker = _DictMapReverser(param_schema, key_field=key_field, value_field=value_field)
    reversed_args = walker.walk(arguments, param_schema, 0)
    return reversed_args if isinstance(reversed_args, dict) else arguments


class _DictMapReverser:
    """One schema-guided walk; holds the ``$defs`` table and field names."""

    def __init__(self, root: Mapping[str, Any], *, key_field: str, value_field: str) -> None:
        defs = root.get("$defs")
        if not isinstance(defs, Mapping):
            defs = root.get("definitions")
        self._defs: Mapping[str, Any] = defs if isinstance(defs, Mapping) else {}
        self._key = key_field
        self._value = value_field

    # -- schema helpers ----------------------------------------------------

    def resolve(self, schema: Any) -> Any:
        """Follow ``$ref`` chains (``#/$defs/X`` / ``#/definitions/X``),
        merging sibling keywords over the target. Unknown refs resolve to the
        node itself."""
        seen = 0
        while isinstance(schema, Mapping) and isinstance(schema.get("$ref"), str) and seen < 32:
            ref = schema["$ref"]
            name = (
                ref.rsplit("/", 1)[-1] if ref.startswith(("#/$defs/", "#/definitions/")) else None
            )
            target = self._defs.get(name) if name is not None else None
            if not isinstance(target, Mapping):
                return schema
            siblings = {k: v for k, v in schema.items() if k != "$ref"}
            schema = {**target, **siblings} if siblings else target
            seen += 1
        return schema

    def dict_map_value_schema(self, schema: Any) -> Any | None:
        """The value schema when *schema* is a typed dict-map, else ``None``
        — the sanitiser's own definition, so both directions agree."""
        return typed_dict_map_value_schema(schema, resolve=self.resolve)

    def declared_properties(self, schema: Any, depth: int = 0) -> set[str]:
        """Property names the value type declares (union / ``allOf`` branches
        included). Bounded by *depth* because this recursion follows the
        schema alone — a recursive union would otherwise never bottom out."""
        resolved = self.resolve(schema)
        if not isinstance(resolved, Mapping) or depth > 8:
            return set()
        props = resolved.get("properties")
        names: set[str] = set(props.keys()) if isinstance(props, Mapping) else set()
        for branch_key in ("anyOf", "oneOf", "allOf"):
            branches = resolved.get(branch_key)
            if isinstance(branches, list):
                for branch in branches:
                    names |= self.declared_properties(branch, depth + 1)
        return names

    def declared_type(self, schema: Mapping[str, Any]) -> str | list[str] | None:
        """The JSON type a node declares, inferred from shape keywords when
        ``type`` is absent."""
        declared = schema.get("type")
        if isinstance(declared, str) or (
            isinstance(declared, list) and all(isinstance(t, str) for t in declared)
        ):
            return declared
        if "properties" in schema or "additionalProperties" in schema:
            return "object"
        if "items" in schema or "prefixItems" in schema:
            return "array"
        return None

    # -- walk --------------------------------------------------------------

    def walk(self, value: Any, schema: Any, depth: int) -> Any:
        if depth > _REVERSE_MAX_DEPTH:
            return value
        schema = self.resolve(schema)
        if not isinstance(schema, Mapping):
            return value

        for branch_key in ("anyOf", "oneOf"):
            branches = schema.get(branch_key)
            if isinstance(branches, list):
                branch = self.pick_branch(value, branches)
                return value if branch is None else self.walk(value, branch, depth + 1)

        all_of = schema.get("allOf")
        if isinstance(all_of, list):
            # Every branch applies (the sanitiser pivots inside each one), so
            # the value goes through each in turn before the node's own
            # keywords — ``{"allOf": [{"$ref": …}], "description": …}`` is the
            # Pydantic-v1 / OpenAPI-3.0 way to attach a description to a ref.
            for branch in all_of:
                value = self.walk(value, branch, depth + 1)

        value = self.decode_container(value, schema)

        value_schema = self.dict_map_value_schema(schema)
        if value_schema is not None:
            return self.reverse_dict_map(value, value_schema, depth)

        props = schema.get("properties")
        if isinstance(props, Mapping) and isinstance(value, dict):
            out = dict(value)
            for field, field_schema in props.items():
                if field in out:
                    out[field] = self.walk(out[field], field_schema, depth + 1)
            return out

        items = schema.get("items")
        if isinstance(items, Mapping) and isinstance(value, list):
            return [self.walk(item, items, depth + 1) for item in value]

        return value

    def decode_container(self, value: Any, schema: Mapping[str, Any]) -> Any:
        """JSON-decode a stringified container where the schema declares an
        object or array and never a string. The nested counterpart of
        :func:`coerce_json_strings`: a ``str`` that does not parse, or parses
        to a scalar, is left as emitted."""
        if not isinstance(value, str):
            return value
        declared = self.declared_type(schema)
        types = set(declared) if isinstance(declared, list) else {declared}
        if "string" in types or not (types & {"object", "array"}):
            return value
        stripped = value.lstrip()
        if not stripped or stripped[0] not in "[{":
            return value
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value
        return decoded if isinstance(decoded, (list, dict)) else value

    def pick_branch(self, value: Any, branches: list[Any]) -> Any | None:
        candidates = [
            b for b in branches if isinstance(b, Mapping) and self.resolve(b).get("type") != "null"
        ]
        if len(candidates) == 1:
            return candidates[0]
        best: Any | None = None
        best_score: int | None = None
        for branch in candidates:
            score = self.branch_score(self.resolve(branch), value)
            if score is None:
                continue
            if best_score is None or score > best_score:
                best, best_score = branch, score
        return best

    def branch_score(self, schema: Any, value: Any) -> int | None:
        """``None`` when the branch cannot hold *value*; otherwise how
        specifically it matches. A discriminator (a property with ``const``)
        that agrees outranks shared property names, which outrank a bare JSON
        type fit; a discriminator that disagrees rules the branch out."""
        if not isinstance(schema, Mapping):
            return None
        value_schema = self.dict_map_value_schema(schema)
        if value_schema is not None:
            if self.is_key_item_list(value):
                return 1
            if isinstance(value, dict):
                flat = self._key in value and self.is_flat_item(
                    value, self.declared_properties(value_schema), value_schema
                )
                return 1 if flat else 0
            return None
        if not self.branch_fits(schema, value):
            return None
        props = schema.get("properties")
        if not isinstance(value, dict) or not isinstance(props, Mapping):
            return 0
        score = 0
        for name, prop_schema in props.items():
            if name not in value:
                continue
            prop = self.resolve(prop_schema)
            if isinstance(prop, Mapping) and "const" in prop:
                if prop["const"] != value[name]:
                    return None
                score += 1000
            else:
                score += 1
        return score

    def branch_fits(self, schema: Mapping[str, Any], value: Any) -> bool:
        declared = self.declared_type(schema)
        if declared is None:
            return False
        if isinstance(declared, list):
            return any(self.branch_fits({**schema, "type": t}, value) for t in declared)
        for py_type, json_type in _JSON_TYPE_BY_PY:
            if isinstance(value, py_type):
                return declared == json_type or (declared == "number" and json_type == "integer")
        return False

    def fits(self, value: Any, schema: Any) -> bool:
        """Can *schema* hold *value*? A schema that declares no type at all
        holds anything; a dict-map holds a dict or the pivoted list; a union
        holds what any branch holds."""
        resolved = self.resolve(schema)
        if not isinstance(resolved, Mapping):
            return True
        for branch_key in ("anyOf", "oneOf"):
            branches = resolved.get(branch_key)
            if isinstance(branches, list):
                return any(self.fits(value, b) for b in branches)
        if self.dict_map_value_schema(resolved) is not None:
            return isinstance(value, dict) or self.is_key_item_list(value)
        if self.declared_type(resolved) is None:
            return True
        return self.branch_fits(resolved, value)

    def is_key_item_list(self, value: Any) -> bool:
        return (
            isinstance(value, list)
            and bool(value)
            and all(isinstance(item, dict) and self._key in item for item in value)
        )

    def reverse_dict_map(self, value: Any, value_schema: Any, depth: int) -> Any:
        declared = self.declared_properties(value_schema)
        scalar_carried = self._value not in declared

        def entry_of(item: dict[str, Any], *, from_wire: bool) -> Any:
            # ``{value: X}`` is the scalar carriage only when the value type
            # has no ``value`` of its own. Coming off the wire shape the
            # carriage is what the schema asked for, so it is unwrapped as
            # is; in the native map shape it is unwrapped only when ``X`` fits
            # the value schema — otherwise it is a genuine one-entry object
            # (``{host: {"value": "token"}}`` on a map of maps).
            if scalar_carried and set(item.keys()) == {self._value}:
                inner = item[self._value]
                if from_wire or self.fits(inner, value_schema):
                    return inner
            return item

        if isinstance(value, list) and not value:
            # The schema says map; the wire said array. An empty array is an
            # empty map — the tool's ``Dict[str, T]`` validator rejects ``[]``.
            return {}

        if self.is_key_item_list(value):
            pivoted: dict[str, Any] = {}
            for item in value:
                item_copy = dict(item)
                key = str(item_copy.pop(self._key))
                pivoted[key] = self.walk(
                    entry_of(item_copy, from_wire=True), value_schema, depth + 1
                )
            return pivoted

        if isinstance(value, dict):
            if self._key in value and self.is_flat_item(value, declared, value_schema):
                # One item emitted bare instead of as a one-item array.
                item_copy = dict(value)
                key = str(item_copy.pop(self._key))
                return {
                    key: self.walk(entry_of(item_copy, from_wire=True), value_schema, depth + 1)
                }
            # Native map shape — the shape the tool wants. Walk the entries so
            # a wrapped scalar or a nested dict-map inside a value reverses.
            return {
                str(k): self.walk(
                    entry_of(v, from_wire=False) if isinstance(v, dict) else v,
                    value_schema,
                    depth + 1,
                )
                for k, v in value.items()
            }

        return value

    def is_flat_item(self, value: dict[str, Any], declared: set[str], value_schema: Any) -> bool:
        """Is *value* one pivoted item emitted bare rather than a native map?
        With a typed value model every other field must be one it declares;
        with a scalar map the item is exactly ``{key, value}`` **and** the
        value holds what the value schema declares. ``{"key": "AIza…",
        "value": "42"}`` on a ``Dict[str, int]`` is therefore a two-entry
        map; on a ``Dict[str, str]`` the shape is ambiguous and is read as
        the item the wire schema asked for."""
        if not isinstance(value.get(self._key), str):
            return False
        others = set(value.keys()) - {self._key}
        if declared:
            return others <= declared
        return others == {self._value} and self.fits(value[self._value], value_schema)
