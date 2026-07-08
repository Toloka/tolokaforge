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
    "RecursiveItemUnwrapResponse",
]


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
# XML repeated-element ``{"item": X}`` recovery (recursive, schema-agnostic)
# ---------------------------------------------------------------------------
#
# Some providers route tool calls through a provider-side XML → JSON conversion
# that renders a *repeated XML element* as a single-key dict keyed on ``item``
# instead of a JSON array::
#
#   <children><item>…</item><item>…</item></children>  (the model's XML)
#     → {"item": [ … , … ]}                            (what litellm parses)
#
# MiniMax-M3 exhibits this on *every* array-valued site (not just the
# ``tags`` sites the earlier ``minimax_m3_tags`` policy was scoped to): the
# observe run shows it on ``root.children`` (recursive trees), ``blocks``
# (heterogeneous arrays nested in objects), ``lines`` / ``items`` (order
# maps) — the wrapper appears at arbitrary depth. A fixed site allowlist can
# no longer bound it, so the recovery walks the whole argument tree.

# Bound the ``{"item": {"item": …}}`` unwrap recursion. Real payloads top out
# at a couple of nesting levels; deeper only comes from pathological input, so
# past the cap the value is returned unchanged rather than raising
# RecursionError at the tool-call assembly site.
_MAX_ITEM_UNWRAP_DEPTH = 64


def _unwrap_item_wrappers(value: Any, _depth: int = 0) -> Any:
    """Recursively rewrite the XML repeated-element ``{"item": X}`` artefact.

    Rule: a dict whose *only* key is ``item`` normalises to a list — recurse
    into ``X`` first (so ``{"item": {"item": "a"}}`` flattens to ``["a"]``),
    then return ``X`` as-is when it is already a list, else ``[X]``. Any other
    dict is recursed into key-by-key but never rewritten (a multi-key dict
    gives no safe way to guess which key is the value — AGENTS.md rule #1:
    refuse rather than corrupt). Lists are recursed element-wise; scalars pass
    through untouched.

    Schema-agnostic on purpose: unlike a declared-array coercion, the trigger
    is the *shape* ``{"item": …}`` alone, which is the exact fingerprint of the
    provider's XML conversion. The single-key requirement is the guard — a
    legitimate object rarely carries a lone field literally named ``item`` — so
    correctly-emitted native lists/dicts pass through unchanged (no false
    positives on the probes that already round-trip clean). A future schema
    that declares a real single-field object named ``item`` would be
    mis-unwrapped; none of the current tool domains do.
    """
    if _depth >= _MAX_ITEM_UNWRAP_DEPTH:
        return value
    if isinstance(value, dict):
        if set(value.keys()) == {"item"}:
            inner = _unwrap_item_wrappers(value["item"], _depth + 1)
            return inner if isinstance(inner, list) else [inner]
        return {k: _unwrap_item_wrappers(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap_item_wrappers(v, _depth + 1) for v in value]
    return value


class RecursiveItemUnwrapResponse:
    """Recover the provider-side XML repeated-element ``{"item": X}`` artefact.

    **Recovery, not transformation.** Native lists/dicts the model emitted
    correctly pass through unchanged; only the single-key ``{"item": …}``
    wrapper — the fingerprint of a provider-side XML → JSON tool-call
    conversion — is rewritten to the native ``list`` the schema declares.

    Pipeline:

    1. :func:`_coerce_json_strings` — first recover any top-level container
       argument the model serialised as a JSON string (the M3 dual shape:
       XML-wrap *or* stringified JSON).
    2. :func:`_unwrap_item_wrappers` — walk the whole argument tree and
       rewrite every ``{"item": X}`` single-key dict to a list, recursing so
       nested and deeply-recursive sites (``root.children`` chains, ``blocks``
       inside a message object) are all recovered.

    Example (recursive tree)::

        # LLM produces (every array wrapped):
        {"root": {"label": "A",
                  "children": {"item": [{"label": "B",
                                         "children": {"item": {"label": "D"}}},
                                        {"label": "C"}]}}}
        # Recovered:
        {"root": {"label": "A",
                  "children": [{"label": "B", "children": [{"label": "D"}]},
                               {"label": "C"}]}}

    ``param_types`` is accepted for Protocol compliance but unused: the
    trigger is the ``{"item": …}`` shape, not a declared type, so recovery
    reaches array sites nested arbitrarily deep where root-level
    ``param_types`` says nothing.
    """

    def parse_arguments(
        self,
        arguments: dict[str, Any],
        *,
        param_types: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        del param_types  # trigger is the {"item": …} shape, not a declared type
        if not isinstance(arguments, dict):
            return arguments
        return _unwrap_item_wrappers(_coerce_json_strings(arguments))
