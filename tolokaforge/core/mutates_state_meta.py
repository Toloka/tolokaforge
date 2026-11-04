"""How an MCP tool's author declares a ``mutates_state`` read rule on the wire (ADR-0061).

The rule rides in the tool's ``_meta`` under :data:`MUTATES_STATE_META_KEY`, beside a
``readOnlyHint`` of ``false``, since some of the tool's calls do change state. The
task's MCP server process checks a rule's shape here before it emits it; the native
adapter validates it in full against
:class:`~tolokaforge.runner.models.MutatesStateRule` when it reads the listing.

Stdlib only, and imported relatively, so a server that loads the registry by path
(``core.tools_interface``) imports nothing of the engine's models.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .value_predicate_operators import (
    TRACE_PREDICATE_BINDING_OPERATORS,
    TRACE_PREDICATE_MODIFIERS,
    TRACE_PREDICATE_OPERATORS,
)

__all__ = [
    "MUTATES_STATE_META_KEY",
    "check_read_rule_binds_nothing",
    "check_read_rule_path",
    "check_read_rule_shape",
]

MUTATES_STATE_META_KEY = "tolokaforge/mutates_state"
"""The engine-owned ``_meta`` key of an MCP tool that carries its read rule."""

_RULE_OPERATORS = TRACE_PREDICATE_OPERATORS - TRACE_PREDICATE_BINDING_OPERATORS

_FORM = "{read_when: {args: {<argument path>: <predicate>}}}"


def check_read_rule_shape(rule: Any) -> None:
    """Refuse a read rule that is not ``{read_when: {args: {<path>: <predicate>}}}``.

    Each path is a dotted string with no empty segment; each predicate a mapping of
    one or more of the trace matcher's operators, the binding operators excluded (a
    rule binds nothing). Operator values are not checked here.

    Raises:
        ValueError: Naming what is wrong.
    """
    if not isinstance(rule, Mapping) or set(rule) != {"read_when"}:
        raise ValueError(f"a read rule is {_FORM}, not {rule!r}")
    read_when = rule["read_when"]
    if not isinstance(read_when, Mapping) or set(read_when) != {"args"}:
        raise ValueError(f"read_when holds exactly args, not {read_when!r}")
    args = read_when["args"]
    if not isinstance(args, Mapping) or not args:
        raise ValueError(f"read_when.args maps one or more argument paths, not {args!r}")
    for path, predicate in args.items():
        check_read_rule_path(path)
        _check_predicate_shape(f"read_when.args.{path}", predicate)


def check_read_rule_path(path: Any) -> None:
    """Refuse a ``read_when.args`` path that is not a dotted string with no empty segment.

    Raises:
        ValueError: Naming the path.
    """
    if not isinstance(path, str) or not all(path.split(".")):
        raise ValueError(f"read_when.args path {path!r} is not a dotted argument path")


def check_read_rule_binds_nothing(where: str, operators: Iterable[Any]) -> None:
    """Refuse a read-rule predicate at *where* that declares a binding operator.

    Raises:
        ValueError: Naming *where* and the binding operators.
    """
    bound = sorted(set(operators) & TRACE_PREDICATE_BINDING_OPERATORS)
    if bound:
        raise ValueError(
            f"{where} uses {', '.join(bound)}, which compare against a value a "
            "constraint's bind extracted; a read rule binds nothing. Write the value "
            "with equals / in_ instead"
        )


def _check_predicate_shape(where: str, predicate: Any) -> None:
    if not isinstance(predicate, Mapping):
        raise ValueError(f"{where} is a mapping of operators, not {predicate!r}")
    names = set(predicate)
    check_read_rule_binds_nothing(where, names)
    unknown = sorted(str(name) for name in names - _RULE_OPERATORS - TRACE_PREDICATE_MODIFIERS)
    if unknown:
        raise ValueError(
            f"{where} names {', '.join(unknown)}, which no predicate reads; "
            f"write one of {sorted(_RULE_OPERATORS)}"
        )
    if not names & _RULE_OPERATORS:
        raise ValueError(f"{where} declares no operator, so it asserts nothing")
