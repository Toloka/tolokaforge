"""System-prompt enrichment policies.

Some providers (notably GPT-5.4) silently drop tool parameters that use
``additionalProperties: {schema}`` typed-dict-map schemas. :class:`DictMapHints`
injects explicit formatting hints into the system prompt so the model keeps
the parameter in the tool call.

``detect_dict_maps`` / :class:`DictMapParam` are re-exported here for
observability (client.py logs the detected params).
"""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

from tolokaforge.core.llm.dict_maps import (
    DictMapParam,
    detect_dict_maps,
    find_additional_properties,
)

__all__ = [
    "SystemPromptPolicy",
    "NoPromptEnrichment",
    "DictMapHints",
    "RefResolvingDictMapHints",
    "DictMapParam",
    "detect_dict_maps",
]


@runtime_checkable
class SystemPromptPolicy(Protocol):
    """Optionally enriches the system prompt before generation."""

    def enrich(self, system: str | None, tools: list[dict[str, Any]] | None) -> str | None: ...


class NoPromptEnrichment:
    """No system prompt modification."""

    def enrich(
        self,
        system: str | None,
        tools: list[dict[str, Any]] | None,  # noqa: ARG002
    ) -> str | None:
        return system


class DictMapHints:
    """Inject explicit formatting hints for typed dict-map parameters.

    Helps models that silently drop ``additionalProperties`` parameters
    (e.g., GPT-5.4).
    """

    def enrich(self, system: str | None, tools: list[dict[str, Any]] | None) -> str | None:
        if not system or not tools:
            return system
        hints = self._build_hints(tools)
        return system + hints if hints else system

    @staticmethod
    def _build_hints(tools: list[dict[str, Any]]) -> str:
        """Generate system-prompt hints for tool parameters using dict-map schemas."""
        dict_maps = detect_dict_maps(tools)
        if not dict_maps:
            return ""

        hints: list[str] = []
        for dm in dict_maps:
            if dm.value_schema is not None:
                # Typed map — extract value schema fields
                if not dm.value_fields:
                    continue

                field_lines = [f'  - "{name}" ({ftype})' for name, ftype in dm.value_fields]

                # Build example
                example_obj = {"example_key": dm.example_value()}
                wrapped_example = json.dumps({dm.param_name: example_obj})

                hint = (
                    f"CRITICAL: {dm.tool_name} — the '{dm.param_name}' parameter "
                    f"MUST be included in the tool call. It is a JSON object where:\n"
                    f"  - Keys are string identifiers\n"
                    f"  - Values are objects with fields:\n"
                    + "\n".join(field_lines)
                    + f"\n  Example: {wrapped_example}\n"
                    f"  DO NOT omit the '{dm.param_name}' parameter."
                )
                hints.append(hint)

            elif dm.param_description:
                # Boolean additionalProperties: true with description
                hints.append(
                    f"CRITICAL: {dm.tool_name} — the '{dm.param_name}' parameter "
                    f"MUST be included as a JSON object with string keys. {dm.param_description}"
                )

        if not hints:
            return ""
        return "\n\n" + "\n\n".join(hints)


class RefResolvingDictMapHints(DictMapHints):
    """:class:`DictMapHints` that also fires when the value schema is a
    ``$ref`` and when the dict-map is one level inside an ``object`` param.

    Motivation — ``thinkingmachines/inkling`` (observe stage). The model emits
    dict-map tool calls flawlessly at the canonical shape (``test_dict_map_tool_call``
    and the ``scalar_values`` variant both 15/15), but on the harder-worded
    ``nested_in_object`` (5/15) and ``wide_map`` (2/15) variants it intermittently
    answers in **prose and omits the tool call entirely** — an attention lapse the
    "MUST be included / DO NOT omit" nudge is designed to close. The shipped
    :class:`DictMapHints` never actually fired on either variant, so the nudge was
    never applied:

    * ``wide_map`` (``add_order_lines.lines: Dict[str, _LineItem]``) — Pydantic
      emits the value schema as ``{"$ref": "#/$defs/_LineItem"}``, so the parent's
      ``value_fields`` is empty and the typed-map hint is skipped.
    * ``nested_in_object`` (``submit_order.order.lines`` where ``order`` is an
      object) — the dict-map lives one level down, and ``detect_dict_maps`` does
      not recurse into ``properties``, so the param is not detected at all.

    This subclass adds ONLY that delta:

    1. Resolve a ``$ref`` value schema against the tool's ``$defs`` (one hop; a
       chained ``$ref`` inside the resolved def is left as-is) so the fields
       surface.
    2. Recurse ONE level into each top-level ``object`` param, emitting a hint for
       any dict-map field found there (``<param>.<field>``). Bounded to one level —
       the depth the observe evidence covers (``order.lines``).

    Everything else — the per-param hint string, the example construction, the
    boolean ``additionalProperties: true`` arm — is delegated to the parent by
    reusing :class:`DictMapParam` and the parent ``_build_hints`` for the
    already-flat top-level maps. Purely additive: a schema the parent already
    handled produces the same hint (a top-level typed map with inline
    ``properties`` still flows through ``detect_dict_maps``); this only adds hints
    the parent dropped.
    """

    def _build_hints(self, tools: list[dict[str, Any]]) -> str:  # type: ignore[override]
        params = self._collect_dict_map_params(tools)
        if not params:
            return ""

        hints: list[str] = []
        for dm in params:
            if dm.value_schema is not None:
                if not dm.value_fields:
                    continue
                field_lines = [f'  - "{name}" ({ftype})' for name, ftype in dm.value_fields]
                example_obj = {"example_key": dm.example_value()}
                wrapped_example = json.dumps({dm.param_name: example_obj})
                hints.append(
                    f"CRITICAL: {dm.tool_name} — the '{dm.param_name}' parameter "
                    f"MUST be included in the tool call. It is a JSON object where:\n"
                    f"  - Keys are string identifiers\n"
                    f"  - Values are objects with fields:\n"
                    + "\n".join(field_lines)
                    + f"\n  Example: {wrapped_example}\n"
                    f"  DO NOT omit the '{dm.param_name}' parameter."
                )
            elif dm.param_description:
                hints.append(
                    f"CRITICAL: {dm.tool_name} — the '{dm.param_name}' parameter "
                    f"MUST be included as a JSON object with string keys. {dm.param_description}"
                )

        if not hints:
            return ""
        return "\n\n" + "\n\n".join(hints)

    @classmethod
    def _collect_dict_map_params(cls, tools: list[dict[str, Any]]) -> list[DictMapParam]:
        """Top-level dict-maps (``$ref`` value schemas resolved) plus dict-maps
        found one level inside an ``object`` param, reported as ``<param>.<field>``.
        """
        results: list[DictMapParam] = []
        for tool in tools:
            func = tool.get("function", {})
            tool_name = func.get("name", "")
            params = func.get("parameters", {})
            defs = params.get("$defs", {})
            props = params.get("properties", {})
            if not isinstance(props, dict):
                continue

            for prop_name, prop_schema in props.items():
                if not isinstance(prop_schema, dict):
                    continue
                additional = find_additional_properties(prop_schema)
                if additional is not None and additional is not False:
                    results.append(
                        cls._make_param(tool_name, prop_name, additional, prop_schema, defs)
                    )
                    continue
                # One level into an object param — resolve a $ref'd object too.
                resolved = cls._resolve_ref(prop_schema, defs)
                if resolved.get("type") != "object":
                    continue
                nested_props = resolved.get("properties", {})
                if not isinstance(nested_props, dict):
                    continue
                for sub_name, sub_schema in nested_props.items():
                    if not isinstance(sub_schema, dict):
                        continue
                    sub_additional = find_additional_properties(sub_schema)
                    if sub_additional is None or sub_additional is False:
                        continue
                    results.append(
                        cls._make_param(
                            tool_name, f"{prop_name}.{sub_name}", sub_additional, sub_schema, defs
                        )
                    )
        return results

    @classmethod
    def _make_param(
        cls,
        tool_name: str,
        param_name: str,
        additional: Any,
        prop_schema: dict[str, Any],
        defs: dict[str, Any],
    ) -> DictMapParam:
        value_schema = additional if isinstance(additional, dict) else None
        if value_schema is not None:
            value_schema = cls._resolve_ref(value_schema, defs)
        return DictMapParam(
            tool_name=tool_name,
            param_name=param_name,
            value_schema=value_schema,
            param_description=prop_schema.get("description", ""),
        )

    @staticmethod
    def _resolve_ref(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
        """Resolve a single local ``#/$defs/Name`` ``$ref`` hop; pass through
        anything else (a non-ref schema, or a ref we cannot resolve) unchanged.
        """
        ref = schema.get("$ref")
        if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
            return schema
        name = ref.split("/")[-1]
        resolved = defs.get(name)
        return resolved if isinstance(resolved, dict) else schema
