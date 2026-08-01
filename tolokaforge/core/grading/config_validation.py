"""Substrate-neutral types for checking grading authoring against a task's tools.

The types here carry no adapter, no filesystem and no substrate: an adapter
resolves the task's tool set into a :class:`ToolInventory`, and the authoring
checks read only that. A tool set the adapter cannot report is
:meth:`ToolInventory.unresolvable` — distinct from a task that declares no
tools, because the two decide opposite things: nothing is checkable against the
first, while every tool name is wrong against the second.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ArgumentSchema(str, Enum):
    """What a tool's resolved schema can answer about an argument name."""

    CLOSED = "closed"
    """``additionalProperties: false`` — a name outside ``properties`` is wrong."""

    OPEN = "open"
    """Properties are known but extras are permitted — an unknown name is suspect."""

    UNKNOWN = "unknown"
    """No schema resolved — nothing about this tool's arguments is checkable."""


@dataclass(frozen=True)
class ToolInventory:
    """The tool set a task gives its actors, and what each tool's schema says."""

    declared: frozenset[str]
    """Union of ``tools.agent.enabled`` and ``tools.user.enabled``."""

    parameters: Mapping[str, Mapping[str, Any]]
    """Tool name to its JSON-schema parameters object, for the tools that resolved."""

    known: bool
    """``False`` only for :meth:`unresolvable`."""

    @classmethod
    def unresolvable(cls) -> ToolInventory:
        """The inventory of an adapter that cannot report a tool set at all."""
        return cls(declared=frozenset(), parameters={}, known=False)

    def strictness(self, tool: str) -> ArgumentSchema:
        """Classify what *tool*'s schema can say about argument names.

        A resolved schema carrying no properties is ``OPEN`` over the empty set —
        the zero-argument tool — not ``UNKNOWN``.
        """
        schema = self.parameters.get(tool)
        if schema is None:
            return ArgumentSchema.UNKNOWN
        if schema.get("additionalProperties") is False:
            return ArgumentSchema.CLOSED
        return ArgumentSchema.OPEN

    def properties(self, tool: str) -> frozenset[str]:
        """The argument names *tool*'s schema declares; empty when unresolved."""
        schema = self.parameters.get(tool)
        if schema is None:
            return frozenset()
        return frozenset(schema.get("properties", {}))
