"""Which of a trial's tool calls can change state, from the tools' ``mutates_state``.

Locks :class:`~tolokaforge.core.tool_effects.ToolEffects`: a read rule is the trace
matcher's ``args`` vocabulary read through the matcher's own resolver, an undeclared
tool can change state, and a bundle predating the recorded declarations is refused.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tolokaforge.core.tool_effects import ToolEffects, UnrecordedToolEffectsError
from tolokaforge.runner.models import MutatesStateRule, ToolSchema

pytestmark = pytest.mark.unit


def _rule(args: dict[str, Any]) -> MutatesStateRule:
    return MutatesStateRule.model_validate({"read_when": {"args": args}})


def _effects(**declared: bool | MutatesStateRule | None) -> ToolEffects:
    return ToolEffects.from_schemas(
        ToolSchema(name=name, description=name, parameters={}, mutates_state=value)
        for name, value in declared.items()
    )


def _changes(effects: ToolEffects, name: str, arguments: dict[str, Any]) -> bool:
    return effects.can_change_state(name, json.dumps(arguments))


@pytest.mark.parametrize(
    ("predicate", "method", "reads"),
    [
        ({"in_": ["GET", "HEAD"]}, "GET", True),
        ({"in_": ["GET", "HEAD"]}, "POST", False),
        ({"equals": "GET"}, "GET", True),
        ({"equals": "GET"}, "get", False),
        ({"regex": "^(GET|HEAD)$"}, "HEAD", True),
        ({"regex": "^(GET|HEAD)$"}, "PATCH", False),
        ({"equals_ci": "get"}, "GET", True),
    ],
)
def test_a_read_rule_reads_the_matchers_operators(
    predicate: dict[str, Any], method: str, reads: bool
) -> None:
    effects = _effects(rest_call=_rule({"request.method": predicate}))

    assert _changes(effects, "rest_call", {"request": {"method": method}}) is not reads


def test_every_path_of_a_rule_must_hold() -> None:
    effects = _effects(rest_call=_rule({"method": {"equals": "GET"}, "url": {"regex": "^/read"}}))

    assert _changes(effects, "rest_call", {"method": "GET", "url": "/read/7"}) is False
    assert _changes(effects, "rest_call", {"method": "GET", "url": "/write/7"}) is True
    assert _changes(effects, "rest_call", {"method": "POST", "url": "/read/7"}) is True


@pytest.mark.parametrize(
    "arguments",
    [{}, {"request": {}}, {"request": "GET"}, {"request": {"method": None}}],
    ids=["no-argument", "no-key", "not-a-mapping", "null"],
)
def test_a_path_the_call_does_not_carry_does_not_match(arguments: dict[str, Any]) -> None:
    effects = _effects(rest_call=_rule({"request.method": {"in_": ["GET"]}}))

    assert _changes(effects, "rest_call", arguments) is True


def test_an_undeclared_or_unknown_tool_can_change_state() -> None:
    effects = _effects(lookup=False, close=True, rest_call=None)

    assert _changes(effects, "lookup", {}) is False
    assert _changes(effects, "close", {}) is True
    assert _changes(effects, "rest_call", {}) is True
    assert _changes(effects, "never_declared", {}) is True
    assert _changes(ToolEffects.empty(), "lookup", {}) is True


@pytest.mark.parametrize(
    ("first", "second", "changes"),
    [(False, True, False), (None, False, True)],
    ids=["declared-then-declared", "undeclared-then-declared"],
)
def test_the_first_schema_of_a_name_is_the_one_read(
    first: bool | None, second: bool, changes: bool
) -> None:
    """An agent tool shadows a same-named user tool even where it declares nothing."""
    effects = ToolEffects.from_schemas(
        [
            ToolSchema(name="lookup", description="", parameters={}, mutates_state=first),
            ToolSchema(name="lookup", description="", parameters={}, mutates_state=second),
        ]
    )

    assert _changes(effects, "lookup", {}) is changes


def test_unparseable_arguments_fail_loud() -> None:
    with pytest.raises(ValueError, match="unparseable arguments JSON"):
        _effects(lookup=False).can_change_state("lookup", "{")


def test_empty_arguments_are_no_arguments() -> None:
    effects = _effects(rest_call=_rule({"method": {"omitted": True}}))

    assert effects.can_change_state("rest_call", "") is False


@pytest.mark.parametrize("operator", ["equals_binding", "contains_binding"])
def test_a_rule_refuses_the_binding_operators(operator: str) -> None:
    with pytest.raises(ValidationError, match="binds nothing"):
        _rule({"method": {operator: "verb"}})


@pytest.mark.parametrize(
    ("args", "error"),
    [({}, "at least 1"), ({"request..method": {"equals": "GET"}}, "empty segment")],
)
def test_a_malformed_rule_is_refused(args: dict[str, Any], error: str) -> None:
    with pytest.raises(ValidationError, match=error):
        _rule(args)


@pytest.mark.parametrize(
    ("pattern", "call"), [("^(?=GET)", "GET"), (r"^(\w)\1$", "GG")], ids=["lookahead", "backref"]
)
def test_a_rule_pattern_runs_on_the_linear_engine_unless_it_names_another(
    pattern: str, call: str
) -> None:
    """A pattern its engine cannot compile is refused where the rule is declared,
    not when a grade first builds the declarations."""
    with pytest.raises(ValidationError, match="the linear regex engine cannot compile"):
        _rule({"method": {"regex": pattern}})
    effects = _effects(
        rest_call=_rule({"method": {"regex": pattern, "regex_engine": "backtracking"}})
    )
    assert _changes(effects, "rest_call", {"method": call}) is False


# ---------------------------------------------------------------------------
# From a trial bundle's record
# ---------------------------------------------------------------------------

_SOURCE = Path("/runs/r/trials/t/0/tools_schemas.yaml")


def _recorded(entries: Any, *, schema_version: Any = 8) -> ToolEffects:
    return ToolEffects.from_recorded_schemas(entries, schema_version=schema_version, source=_SOURCE)


def _entry(name: str, **extra: Any) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "parameters": {}}, **extra}


def test_a_generation_8_record_gives_back_its_declarations() -> None:
    rule = {"read_when": {"args": {"method": {"in_": ["GET"]}}}}

    effects = _recorded(
        [
            _entry("rest_call", mutates_state=rule),
            _entry("lookup", mutates_state=False),
            _entry("search"),
            _entry("lookup", mutates_state=True),
            _entry("search", mutates_state=False),
        ]
    )

    assert _changes(effects, "rest_call", {"method": "GET"}) is False
    assert _changes(effects, "rest_call", {"method": "DELETE"}) is True
    assert _changes(effects, "lookup", {}) is False
    assert _changes(effects, "search", {}) is True


@pytest.mark.parametrize(
    "schema_version", [7, None, "8"], ids=["generation-7", "unstamped", "text"]
)
def test_a_record_predating_the_declarations_is_refused(schema_version: Any) -> None:
    with pytest.raises(UnrecordedToolEffectsError, match=f"generation-{schema_version} bundle"):
        _recorded([_entry("lookup", mutates_state=False)], schema_version=schema_version)


@pytest.mark.parametrize(
    ("entries", "error"),
    [
        (None, "records no tool list"),
        ([_entry("rest_call", mutates_state={"read_when": {}})], "no rule validates"),
        (
            [
                _entry(
                    "rest_call",
                    mutates_state={"read_when": {"args": {"method": {"regex": r"(\w)\1"}}}},
                )
            ],
            "linear regex engine cannot compile",
        ),
        ([{"type": "function", "function": {}}], "records no function.name"),
    ],
    ids=["no-file", "bad-rule", "uncompilable-pattern", "no-name"],
)
def test_a_record_that_cannot_say_is_refused(entries: Any, error: str) -> None:
    with pytest.raises(UnrecordedToolEffectsError, match=error):
        _recorded(entries)
