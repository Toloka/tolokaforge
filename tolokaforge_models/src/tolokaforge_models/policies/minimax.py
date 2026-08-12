"""Per-model policy subclasses for the MiniMax family.

MiniMax-M3 routes its tool calls through a provider-side XML → JSON
conversion that systematically corrupts the ``tags`` argument on every
emission (2505/2505 airlines occurrences). This module ships the two
recursive, tags-site-scoped recovery policies plus the composite the
``minimax`` preset wires in:

* :class:`JsonRecursiveCoerceResponse` — stringified list → native list,
  ``''`` → ``[]`` at the tags sites.
* :class:`ItemRecursiveUnwrapResponse` — ``{"item": X}`` XML repeated-
  element artefact → list at the tags sites.
* :class:`MinimaxM3TagRecoveryResponse` — composite chaining the two in
  ``coerce`` then ``unwrap`` order.

Only the composite is entry-point-registered as a top-level policy; the
two component classes ship as public symbols on this module for
out-of-tree code that needs them individually.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tolokaforge.core.llm.response_policy import coerce_json_strings

__all__ = [
    "ItemRecursiveUnwrapResponse",
    "JsonRecursiveCoerceResponse",
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


# Bound the ``{"item": {"item": ...}}`` unwrap recursion. The observed M3
# census tops out at one nesting level; greater depth only comes from
# pathological / adversarial input, so past the cap the value is returned
# unchanged (refuse to recover rather than raise RecursionError at the
# tool-call assembly site).
_MAX_UNWRAP_DEPTH = 64


def _recover_json_string_at_tags_site(value: Any) -> Any:
    """Recover one ``tags`` value's stringification artefacts.

    Two shapes, both proven on the M3 airlines census:

    * stringified JSON list (``'["a","b"]'``) → the native ``list`` via
      :func:`coerce_json_strings` (which only promotes ``[`` / ``{`` strings
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
    return coerce_json_strings({"tags": value})["tags"]


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
    """Recursive, tags-site-scoped variant of the shipped ``JsonCoerceResponse``.

    MiniMax-M3's XML → JSON tool-call conversion serialises the ``tags`` array
    as a JSON-encoded *string* (``'["receipt-issued"]'``, 550/2505 ≈ 22 %) or
    as an empty string (``''``, 27/2505 ≈ 1 %). Unlike the shipped flat
    :class:`~tolokaforge.core.llm.response_policy.JsonCoerceResponse`, the
    corrupt ``tags`` lives **one level deep** inside the schemaless
    ``additionalProperties: true`` ``updates`` / ``item`` object, so recovery
    has to recurse into that parent.

    Scope (load-bearing): only the ``(parent, field)`` paths in
    :data:`ARRAY_SITES` are touched — ``updates.tags`` and ``item.tags``. The
    empty-string → ``[]`` coercion is therefore tied to declared-array sites
    and can never fire on a scalar field. A schema-agnostic empty-string → ``[]``
    was proven net-harmful: on MiniMax-M2.7 it corrupted scalar fields such as
    ``resolution_category__c`` / ``employee_id`` / ``keyword``.

    Never promotes scalar strings (delegates to
    :func:`~tolokaforge.core.llm.response_policy.coerce_json_strings`), never
    touches ``None`` / ``null`` (passes straight through).
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
    refuse rather than corrupt.

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
    ``ArrayDictMapResponse`` precedent (one named policy composing two
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
