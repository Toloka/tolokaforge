"""Checking grading authoring against the tools a task gives its actors.

Substrate-neutral and pure — no adapter, no filesystem: an adapter resolves the
task's tool set into a :class:`ToolInventory`, and :func:`inspect_grading_authoring`
reads only that. A tool set the adapter cannot report is
:meth:`ToolInventory.unresolvable` — distinct from a task that declares no
tools, because the two decide opposite things: nothing is checkable against the
first, while every tool name is wrong against the second.

The defects here are the author's, and every one of them is otherwise charged to
the agent or to nobody: a misspelled tool name in a ``present`` matcher scores the
component 0.0 with the message a genuine agent failure carries, the same typo in
an ``absent`` matcher passes every trial, and an uncompilable ``regex`` raises
inside the evaluator once the tokens are already spent.

What the schema cannot answer is reported as :class:`Skip` and never raises, so
the gate has no false-reject mode. The severity of each rule is documented in
``docs/GRADING.md`` § "What is validated before a run".
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel

from tolokaforge.core.models import (
    GradingFindingSeverity,
    ToolExpectations,
    TraceChecksConfig,
    TraceConstraintExpr,
    TraceMatcher,
    TranscriptRulesConfig,
    ValuePredicate,
)


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

    def __post_init__(self) -> None:
        if not self.known and (self.declared or self.parameters):
            raise ValueError(
                "an unresolvable inventory carries tools: every rule that reads them is "
                f"skipped, so {sorted(self.declared) or sorted(self.parameters)} would be "
                "resolved and then ignored. Report the tools with known=True, or report "
                "nothing"
            )

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


@dataclass(frozen=True)
class Finding:
    """One authoring defect, addressed by where in the block it was written."""

    where: str
    message: str


@dataclass(frozen=True)
class Skip:
    """One thing the gate could not check, and why it could not."""

    where: str
    reason: str


@dataclass(frozen=True)
class AuthoringReport:
    """What checking one grading block against one tool inventory found.

    ``unchecked`` is a channel, not a third severity: nothing reads it to decide
    whether to raise, so a tool set the adapter cannot report can never fail a
    pack. It is still reported — the CLI prints it beside the task — because a
    gate that checked nothing must not read as a clean bill of health.
    """

    errors: tuple[Finding, ...] = ()
    """Fatal wherever the report is enforced."""

    advisories: tuple[Finding, ...] = ()
    """Fatal only where the caller enforces them."""

    unchecked: tuple[Skip, ...] = ()
    """Never fatal anywhere."""

    def fatal(self, fail_on: GradingFindingSeverity) -> tuple[Finding, ...]:
        """The findings a caller enforcing down to *fail_on* must refuse."""
        if fail_on is GradingFindingSeverity.ERROR:
            return self.errors
        return self.errors + self.advisories


@dataclass(frozen=True)
class _MatcherSite:
    """One matcher, and the authored address it was read from."""

    where: str
    matcher: TraceMatcher


@dataclass(frozen=True)
class _PredicateSite:
    """One value predicate, and the authored address it was read from."""

    where: str
    predicate: ValuePredicate


_UNRESOLVABLE_REASON = (
    "the tool set of this task could not be resolved, so no tool name and no argument "
    "name in this block is checkable"
)

# What naming an undeclared tool costs, per expectation. Written per field because
# the two fail in opposite directions: one charges the agent for a tool it was never
# given, the other cannot fail at all.
_TOOL_EXPECTATION_HAZARDS: Mapping[str, str] = {
    "required_tools": (
        "no actor can call it, so the transcript component is short a required tool on "
        "every trial however the agent behaves"
    ),
    "disallowed_tools": (
        "no actor can call it, so the rule passes on every trial and forbids nothing"
    ),
}


def inspect_grading_authoring(
    grading: Mapping[str, Any], inventory: ToolInventory
) -> AuthoringReport:
    """Report what a task's tools say about the grading block it is graded by.

    The block is expected to have passed its own shape validation; the typed
    sub-blocks read here are constructed, so a malformed one raises its own load
    error rather than being reported as an authoring finding.

    Every rule that needs the task's tools is skipped into ``unchecked`` when the
    inventory is unresolvable, and the rules that need nothing but the block —
    regex compilation, the hash-source declaration — still run.
    """
    sites = tuple(_trace_matcher_sites(grading))
    rules = _transcript_rules(grading)
    reports = [
        _check_regex_compiles(sites, rules.disallow_regex if rules else ()),
        _check_hash_source_declared(grading),
    ]
    if inventory.known:
        reports += [
            _check_tool_names(sites, inventory),
            _check_tool_expectation_names(rules.tool_expectations if rules else None, inventory),
            _check_argument_paths(sites, inventory),
        ]
    else:
        reports.append(AuthoringReport(unchecked=(Skip("grading", _UNRESOLVABLE_REASON),)))
    return _merged(reports)


def _check_tool_names(sites: tuple[_MatcherSite, ...], inventory: ToolInventory) -> AuthoringReport:
    """A matcher may only name a tool some actor of the task can call.

    A name is read off ``equals`` and ``in_`` alone. ``regex`` names a set by
    pattern rather than a token, and every other operator is left alone rather
    than guessed at.
    """
    errors = tuple(
        Finding(f"{site.where}.tool", _undeclared_tool_message(name, inventory))
        for site in sites
        if site.matcher.tool is not None
        for name in _tool_names_asserted_by(site.matcher.tool)
        if name not in inventory.declared
    )
    return AuthoringReport(errors=errors)


def _check_tool_expectation_names(
    expectations: ToolExpectations | None, inventory: ToolInventory
) -> AuthoringReport:
    """``tool_expectations`` may only name a tool some actor of the task can call."""
    if expectations is None:
        return AuthoringReport()
    errors = tuple(
        Finding(
            f"transcript_rules.tool_expectations.{expectation}",
            f"tool {name!r} is not declared by this task, which gives its actors "
            f"{sorted(inventory.declared)}: {hazard}",
        )
        for expectation, hazard in _TOOL_EXPECTATION_HAZARDS.items()
        for name in getattr(expectations, expectation)
        if name not in inventory.declared
    )
    return AuthoringReport(errors=errors)


def _check_argument_paths(
    sites: tuple[_MatcherSite, ...], inventory: ToolInventory
) -> AuthoringReport:
    """An ``args`` path's first segment must be an argument the tool declares.

    Only the first segment, and only against ``properties``: a nested path bottoms
    out where the schema stops declaring properties, and descending further would
    reject argument paths that grade correctly today (#765).
    """
    return _merged(
        _one_matchers_argument_paths(site, inventory)
        for site in sites
        if site.matcher.args is not None
    )


def _check_regex_compiles(
    sites: tuple[_MatcherSite, ...], disallow_regex: Iterable[str]
) -> AuthoringReport:
    """Every authored pattern compiles here, or it raises inside the evaluator.

    Neither substrate catches ``re.error`` locally: core lets it propagate out of
    the grader and the runner folds it into a failed grade response, so the trial
    is lost rather than the constraint.
    """
    authored = [
        (f"transcript_rules.disallow_regex[{index}]", pattern)
        for index, pattern in enumerate(disallow_regex)
    ]
    authored += [
        (f"{predicate_site.where}.regex", predicate_site.predicate.regex)
        for site in sites
        for predicate_site in _predicate_sites(site)
        if predicate_site.predicate.regex is not None
    ]
    findings = (_uncompilable(where, pattern) for where, pattern in authored)
    return AuthoringReport(errors=tuple(finding for finding in findings if finding is not None))


def _check_hash_source_declared(grading: Mapping[str, Any]) -> AuthoringReport:
    """An expected state hash is read only where the hash check is enabled.

    Both substrates test the flag before reading the hash, so the comparison an
    author wrote never runs and the pack grades without it in silence. The flag is
    read for truth rather than for ``True``, because that is what decides the
    grade: core branches on its truthiness and the runner coerces it, so a pack
    written ``enabled: 1`` does read the hash and rejecting it here would be
    stricter than either substrate.
    """
    state_checks = grading.get("state_checks")
    if not isinstance(state_checks, Mapping):
        return AuthoringReport()
    hash_block = state_checks.get("hash")
    if not isinstance(hash_block, Mapping):
        return AuthoringReport()
    if not hash_block.get("expected_state_hash") or hash_block.get("enabled"):
        return AuthoringReport()
    return AuthoringReport(
        errors=(
            Finding(
                "state_checks.hash.expected_state_hash",
                "an expected state hash is declared while hash.enabled is "
                f"{hash_block.get('enabled')!r}: both substrates read the flag before the "
                "hash, so the comparison never runs and the state is graded without it. "
                "Write enabled: true, or drop the hash",
            ),
        )
    )


def _one_matchers_argument_paths(site: _MatcherSite, inventory: ToolInventory) -> AuthoringReport:
    """The argument half of one matcher, once the tool it addresses is decided."""
    tool_predicate = site.matcher.tool
    named = frozenset(_tool_names_asserted_by(tool_predicate)) if tool_predicate else frozenset()
    where = f"{site.where}.args"
    if len(named) != 1:
        return AuthoringReport(
            unchecked=(
                Skip(
                    where,
                    "the matcher does not name one tool, so which schema declares these "
                    "argument names is undecided",
                ),
            )
        )
    tool = next(iter(named))
    strictness = inventory.strictness(tool)
    if strictness is ArgumentSchema.UNKNOWN:
        return AuthoringReport(
            unchecked=(
                Skip(where, f"no schema resolved for {tool!r}, so its arguments are not checkable"),
            )
        )
    properties = inventory.properties(tool)
    return _merged(
        _one_argument_path(f"{where}.{path}", path, tool, strictness, properties)
        for path in site.matcher.args
    )


def _one_argument_path(
    where: str,
    path: str,
    tool: str,
    strictness: ArgumentSchema,
    properties: frozenset[str],
) -> AuthoringReport:
    head, _, below = path.partition(".")
    if head not in properties:
        return _unknown_argument_report(where, head, tool, strictness, properties)
    if below:
        return AuthoringReport(
            unchecked=(
                Skip(
                    where,
                    f"an argument path is checked at its first segment only, so {below!r} "
                    f"under {head!r} is not (#765)",
                ),
            )
        )
    return AuthoringReport()


def _unknown_argument_report(
    where: str,
    head: str,
    tool: str,
    strictness: ArgumentSchema,
    properties: frozenset[str],
) -> AuthoringReport:
    declared = f"{tool!r} declares {sorted(properties)}"
    if strictness is ArgumentSchema.CLOSED:
        return AuthoringReport(
            errors=(
                Finding(
                    where,
                    f"argument {head!r} is not one {declared} and its schema admits no "
                    "other, so the matcher selects nothing and the default on_missing "
                    "reports that as the agent's failure",
                ),
            )
        )
    return AuthoringReport(
        advisories=(
            Finding(
                where,
                f"argument {head!r} is not one {declared}. The schema permits arguments it "
                "does not declare, so this is a probable typo rather than a certainty",
            ),
        )
    )


def _undeclared_tool_message(name: str, inventory: ToolInventory) -> str:
    if not inventory.declared:
        return (
            f"tool {name!r} is not declared by this task, which gives its actors no tools "
            "at all, so the matcher selects nothing whatever the agent does"
        )
    return (
        f"tool {name!r} is not declared by this task, which gives its actors "
        f"{sorted(inventory.declared)}. A matcher naming a tool no actor can call selects "
        "nothing, which the default on_missing reports as the agent's failure"
    )


def _uncompilable(where: str, pattern: str) -> Finding | None:
    try:
        re.compile(pattern)
    except re.error as error:
        return Finding(
            where,
            f"regex {pattern!r} does not compile: {error}. An uncompilable pattern raises "
            "out of the evaluator at grade time, once the trial is already paid for",
        )
    return None


def _tool_names_asserted_by(predicate: ValuePredicate) -> tuple[str, ...]:
    """The tool names a predicate asserts as tokens, if it asserts any."""
    named: list[str] = []
    if isinstance(predicate.equals, str):
        named.append(predicate.equals)
    if predicate.in_ is not None:
        named += [value for value in predicate.in_ if isinstance(value, str)]
    return tuple(named)


def _trace_matcher_sites(grading: Mapping[str, Any]) -> Iterator[_MatcherSite]:
    """Every matcher the block declares, shared and per-route alike.

    A route's constraints are graded exactly as the shared ones are, so a typo
    inside one is the same defect — and the route id joins the address because the
    block's one id space is what keeps the two forms apart.
    """
    block = grading.get("trace_checks")
    if not isinstance(block, Mapping):
        return
    config = TraceChecksConfig(**block)
    for constraint in config.constraints:
        yield from _matcher_sites(constraint.require, f"trace_checks.{constraint.id}")
    for path in config.alternatives or ():
        for constraint in path.constraints:
            yield from _matcher_sites(constraint.require, f"trace_checks.{path.id}.{constraint.id}")


def _transcript_rules(grading: Mapping[str, Any]) -> TranscriptRulesConfig | None:
    block = grading.get("transcript_rules")
    if not isinstance(block, Mapping):
        return None
    return TranscriptRulesConfig(**block)


def _matcher_sites(value: Any, where: str) -> Iterator[_MatcherSite]:
    """Every matcher reachable from an authored constraint, addressed by its path.

    Structural rather than per-kind, so a constraint kind added to the vocabulary
    is walked without a second table to keep in step.
    """
    if isinstance(value, TraceMatcher):
        yield _MatcherSite(where, value)
    elif isinstance(value, TraceConstraintExpr):
        kind = value.declared_kind().value
        yield from _matcher_sites(getattr(value, kind), f"{where}.{kind}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _matcher_sites(item, f"{where}[{index}]")
    elif isinstance(value, BaseModel):
        for name in type(value).model_fields:
            yield from _matcher_sites(getattr(value, name), f"{where}.{name}")


def _predicate_sites(site: _MatcherSite) -> Iterator[_PredicateSite]:
    """Every value predicate one matcher carries, including its argument paths."""
    for name in type(site.matcher).model_fields:
        value = getattr(site.matcher, name)
        if isinstance(value, ValuePredicate):
            yield _PredicateSite(f"{site.where}.{name}", value)
        elif isinstance(value, Mapping):
            for path, predicate in value.items():
                yield _PredicateSite(f"{site.where}.{name}.{path}", predicate)


def _merged(reports: Iterable[AuthoringReport]) -> AuthoringReport:
    collected = tuple(reports)
    return AuthoringReport(
        errors=tuple(finding for report in collected for finding in report.errors),
        advisories=tuple(finding for report in collected for finding in report.advisories),
        unchecked=tuple(skip for report in collected for skip in report.unchecked),
    )
