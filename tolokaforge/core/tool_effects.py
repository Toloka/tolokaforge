"""Which of a trial's tool calls can change state, from the tools' ``mutates_state`` (ADR-0061).

A tool declares ``false`` (every call reads), ``true`` (every call can change
state), or a :class:`~tolokaforge.runner.models.MutatesStateRule` naming the
arguments under which a call reads. A tool that declares nothing, or that the
declarations do not name at all, can change state. The rule's predicates are read
through :mod:`tolokaforge.core.grading.argument_predicates`, the trace matcher's
own resolver.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from tolokaforge.core.grading.argument_predicates import (
    PredicatePatterns,
    argument_at,
    compile_predicate_patterns,
    predicate_holds,
)
from tolokaforge.runner.models import MutatesStateRule

if TYPE_CHECKING:
    from tolokaforge.runner.models import ToolSchema

__all__ = [
    "TOOL_EFFECTS_BUNDLE_VERSION",
    "ToolEffects",
    "UnrecordedToolEffectsError",
]

TOOL_EFFECTS_BUNDLE_VERSION = 8
"""The first trial-bundle generation whose ``tools_schemas.yaml`` records ``mutates_state``."""


class UnrecordedToolEffectsError(ValueError):
    """A trial bundle that cannot say which of its tool calls can change state."""


@dataclass(frozen=True)
class _ReadRule:
    """A :class:`MutatesStateRule` with its patterns compiled once."""

    rule: MutatesStateRule
    patterns: PredicatePatterns

    @classmethod
    def compile(cls, rule: MutatesStateRule) -> _ReadRule:
        return cls(
            rule,
            compile_predicate_patterns(rule.read_when.args.items(), rule.read_when.REGEX_ENGINE),
        )

    def reads(self, arguments: Any) -> bool:
        return all(
            predicate_holds(path, predicate, argument_at(arguments, path), {}, self.patterns)
            for path, predicate in self.rule.read_when.args.items()
        )


@dataclass(frozen=True)
class ToolEffects:
    """The ``mutates_state`` each tool of a trial declares, by tool name."""

    _declared: Mapping[str, bool | _ReadRule] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def empty(cls) -> ToolEffects:
        """Declarations for no tool: every call can change state."""
        return cls()

    @classmethod
    def from_schemas(cls, schemas: Iterable[ToolSchema]) -> ToolEffects:
        """The declarations of *schemas*; the first schema of a name is the one read."""
        declared: dict[str, bool | _ReadRule] = {}
        seen: set[str] = set()
        for schema in schemas:
            if schema.name in seen:
                continue
            seen.add(schema.name)
            if schema.mutates_state is not None:
                declared[schema.name] = _compiled(schema.mutates_state)
        return cls(MappingProxyType(declared))

    @classmethod
    def from_recorded_schemas(
        cls, entries: Any, *, schema_version: Any, source: Path
    ) -> ToolEffects:
        """The declarations a trial bundle recorded on its ``tools_schemas.yaml`` entries.

        *entries* is that file's content, ``None`` where the bundle has none, and
        *schema_version* the bundle's ``metrics.yaml`` stamp; *source* is the file,
        named in every refusal. Each entry carries its tool's declaration as a
        top-level ``mutates_state``, absent where the tool declares none. The agent's
        tools come first in the file, so the first entry of a name is the one read.

        Raises:
            UnrecordedToolEffectsError: the bundle predates generation
                :data:`TOOL_EFFECTS_BUNDLE_VERSION`, records no tool list, or records
                a declaration the rule model refuses.
        """
        if not isinstance(schema_version, int) or schema_version < TOOL_EFFECTS_BUNDLE_VERSION:
            raise UnrecordedToolEffectsError(
                f"{source.parent} is a generation-{schema_version} bundle; its "
                f"{source.name} records the tools' mutates_state from generation "
                f"{TOOL_EFFECTS_BUNDLE_VERSION}, so which of the agent's calls can change "
                "state is not recorded. Re-run the trial"
            )
        if not isinstance(entries, list):
            raise UnrecordedToolEffectsError(
                f"{source} records no tool list, so which of the agent's calls can change "
                "state is not recorded"
            )
        declared: dict[str, bool | _ReadRule] = {}
        seen: set[str] = set()
        for index, entry in enumerate(entries):
            name, raw = _recorded_declaration(source, index, entry)
            if name in seen:
                continue
            seen.add(name)
            if raw is None:
                continue
            try:
                value = raw if isinstance(raw, bool) else MutatesStateRule.model_validate(raw)
            except ValidationError as exc:
                raise UnrecordedToolEffectsError(
                    f"{source} entry {index} ({name!r}) records a mutates_state no rule "
                    f"validates: {exc}"
                ) from exc
            declared[name] = _compiled(value)
        return cls(MappingProxyType(declared))

    def can_change_state(self, name: str, arguments_json: str) -> bool:
        """Whether a call of tool *name* with these arguments can change state.

        ``arguments_json`` is the call's arguments as the transcript records them;
        an empty string is no arguments.

        Raises:
            ValueError: ``arguments_json`` is not JSON.
        """
        arguments = _parsed_arguments(name, arguments_json)
        declared = self._declared.get(name, True)
        if isinstance(declared, bool):
            return declared
        return not declared.reads(arguments)


def _compiled(value: bool | MutatesStateRule) -> bool | _ReadRule:
    return value if isinstance(value, bool) else _ReadRule.compile(value)


def _parsed_arguments(name: str, arguments_json: str) -> Any:
    if not arguments_json:
        return {}
    try:
        return json.loads(arguments_json)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"a call of {name!r} carries unparseable arguments JSON ({exc}); the "
            "transcript wire never produces one"
        ) from exc


def _recorded_declaration(path: Path, index: int, entry: Any) -> tuple[str, Any]:
    function = entry.get("function") if isinstance(entry, Mapping) else None
    name = function.get("name") if isinstance(function, Mapping) else None
    if not isinstance(name, str) or not name:
        raise UnrecordedToolEffectsError(
            f"{path} entry {index} records no function.name, so whose mutates_state it "
            "carries is unknown"
        )
    return name, entry.get("mutates_state")
