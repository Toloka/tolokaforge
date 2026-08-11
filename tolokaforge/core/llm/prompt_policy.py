"""System-prompt enrichment policies.

Some providers (notably GPT-5.4) silently drop tool parameters that use
``additionalProperties: {schema}`` typed-dict-map schemas. :class:`DictMapHints`
injects explicit formatting hints into the system prompt so the model keeps
the parameter in the tool call. Per-model subclasses (e.g.
:class:`~tolokaforge_models.policies.inkling.RefResolvingDictMapHints`)
ship in :mod:`tolokaforge_models.policies` and reach the engine through the
``tolokaforge.policies`` entry-point group.

``detect_dict_maps`` / :class:`DictMapParam` are re-exported here for
observability (client.py logs the detected params).
"""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

from tolokaforge.core.llm.dict_maps import (
    DictMapParam,
    detect_dict_maps,
)

__all__ = [
    "SystemPromptPolicy",
    "NoPromptEnrichment",
    "DictMapHints",
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
        hints = self.build_hints(tools)
        return system + hints if hints else system

    def build_hints(self, tools: list[dict[str, Any]]) -> str:
        """Public hook — override to compose custom system-prompt hints.

        Generates the hint text for tool parameters using dict-map schemas.
        Called by :meth:`enrich` when both ``system`` and ``tools`` are
        non-empty.

        Public API. Stable within the v0.17.x minor series; removal or
        signature change requires a deprecation announcement.
        """
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
