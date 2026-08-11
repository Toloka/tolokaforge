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
    "coerce_empty_containers",
    "coerce_json_strings",
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


def coerce_json_strings(arguments: dict[str, Any]) -> dict[str, Any]:
    """Decode stringified JSON arrays / objects back to native values.

    Public API. Stable within the v0.17.x minor series; removal or signature
    change requires a deprecation announcement.

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
        coerced = coerce_empty_containers(arguments, param_types)
        return coerce_json_strings(coerced)


class ArrayDictMapResponse:
    """JSON-coerce, then reverse the ``StrictSchema`` dict-map→array conversion.

    Two-stage pipeline:

    1. :func:`coerce_json_strings` — recover stringified containers.
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
        coerced = coerce_empty_containers(arguments, param_types)
        result = dict(coerce_json_strings(coerced))
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
