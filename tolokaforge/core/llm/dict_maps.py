"""Dict-map parameter detection shared by the prompt policy and the client's
observability logging.

Also re-exported from :mod:`tolokaforge.core.llm.prompt_policy` for
convenience.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["DictMapParam", "detect_dict_maps", "find_additional_properties"]


@dataclass(frozen=True)
class DictMapParam:
    """A dict-map parameter detected in a tool schema.

    Represents a parameter using ``additionalProperties: {schema}`` or
    ``additionalProperties: true`` — typed dictionaries mapping string keys
    to structured values.
    """

    tool_name: str
    param_name: str
    value_schema: dict[str, Any] | None  # None when additionalProperties is True (boolean)
    param_description: str  # Original description from schema

    @property
    def value_fields(self) -> list[tuple[str, str]]:
        """List of (field_name, field_type) for the value schema."""
        if not self.value_schema:
            return []
        props = self.value_schema.get("properties", {})
        return [(name, prop.get("type", "any")) for name, prop in props.items()]

    @property
    def required_fields(self) -> list[str]:
        """Required field names in the value schema."""
        if not self.value_schema:
            return []
        return self.value_schema.get("required", [])

    def describe_value(self) -> str:
        """Human-readable description of value schema fields."""
        if not self.value_fields:
            return ""
        parts = [f"{name} ({ftype})" for name, ftype in self.value_fields]
        desc = "{" + ", ".join(parts) + "}"
        if self.required_fields:
            desc += f" required: [{', '.join(self.required_fields)}]"
        return desc

    def example_value(self) -> dict[str, Any]:
        """Build an example value from schema types."""
        if not self.value_schema:
            return {}
        example: dict[str, Any] = {}
        for name, ftype in self.value_fields:
            if ftype in ("number", "integer"):
                example[name] = 0
            elif ftype == "string":
                example[name] = ""
            elif ftype == "boolean":
                example[name] = True
            else:
                example[name] = None
        return example


def find_additional_properties(prop_schema: dict[str, Any]) -> dict[str, Any] | bool | None:
    """Locate an ``additionalProperties`` declaration on *prop_schema* or any
    of its ``anyOf`` / ``oneOf`` branches.

    Public API. Stable within the v0.17.x minor series; removal or signature
    change requires a deprecation announcement.

    Pydantic emits ``Optional[Dict[str, T]]`` as
    ``anyOf=[{additionalProperties:T, type:object}, {type:null}]`` and
    ``Union[Dict[str, T], …]`` analogously. The detector must therefore
    treat ``anyOf`` / ``oneOf`` branches as alternative locations for the
    dict-map declaration; the first non-null branch carrying
    ``additionalProperties`` wins.

    Returns the ``additionalProperties`` value (a dict for typed maps, the
    literal ``True`` for free-form maps, or ``False`` / ``None`` when not
    present). Does not recurse into ``properties`` / ``items`` — the
    contract is "this *parameter*'s top type is a dict-map", not "any
    nested field anywhere is".
    """
    direct = prop_schema.get("additionalProperties")
    if direct is not None and direct is not False:
        return direct

    for union_key in ("anyOf", "oneOf"):
        branches = prop_schema.get(union_key)
        if not isinstance(branches, list):
            continue
        for branch in branches:
            if not isinstance(branch, dict):
                continue
            if branch.get("type") == "null":
                continue
            inner = branch.get("additionalProperties")
            if inner is not None and inner is not False:
                return inner
    return None


def detect_dict_maps(tools: list[dict[str, Any]]) -> list[DictMapParam]:
    """Scan tool schemas for parameters using additionalProperties dict-map patterns.

    Detects both typed maps (``additionalProperties: {schema}``) and boolean
    maps (``additionalProperties: true``). Recurses into ``anyOf`` / ``oneOf``
    branches so that ``Optional[Dict[str, T]]`` and ``Union[Dict[str, T], …]``
    parameters are surfaced — Pydantic wraps optional dict-maps in
    ``anyOf[{additionalProperties:T}, {null}]``.
    """
    results: list[DictMapParam] = []
    for tool in tools:
        func = tool.get("function", {})
        tool_name = func.get("name", "")
        params = func.get("parameters", {})
        props = params.get("properties", {})

        for prop_name, prop_schema in props.items():
            if not isinstance(prop_schema, dict):
                continue
            additional = find_additional_properties(prop_schema)
            if additional is None or additional is False:
                continue

            value_schema = additional if isinstance(additional, dict) else None
            results.append(
                DictMapParam(
                    tool_name=tool_name,
                    param_name=prop_name,
                    value_schema=value_schema,
                    param_description=prop_schema.get("description", ""),
                )
            )
    return results
