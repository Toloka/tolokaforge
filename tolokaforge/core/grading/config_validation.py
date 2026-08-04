"""Checking grading authoring against what a task gives its actors, its replay and its fold.

Substrate-neutral and pure — no adapter, no filesystem: an adapter resolves the
task's tool set into a :class:`ToolInventory`, the world its golden actions replay in
into a :class:`ReplayWorld` and the task's effective ``combine``, and
:func:`inspect_grading_authoring` reads only those. A tool set the adapter cannot
report is :meth:`ToolInventory.unresolvable` — distinct from a task that declares no
tools, because the two decide opposite things: nothing is checkable against the first,
while every tool name is wrong against the second. A replay world reads the same way
through :meth:`ReplayWorld.unresolvable`, and an effective combine no caller could
resolve is ``None``.

The defects here are the author's, and every one of them is otherwise charged to
the agent or to nobody: a misspelled tool name in a ``present`` matcher scores the
component 0.0 with the message a genuine agent failure carries, the same typo in
an ``absent`` matcher passes every trial, an uncompilable ``regex`` raises inside
the evaluator once the tokens are already spent, a binding correlated against a
field of another type is red on every trajectory whatever the agent did, a golden
action naming no callable tool leaves the replay with no world to hash and the trial
with no verdict, a golden path authored against a task that declares no initial-state
file or no MCP server module has no world to replay in at all, a golden source that is
not the list of actions to replay is iterated by one substrate and handed to the other's
replay loop and crashes both once the trial is paid for, a section declaring
nothing scores nothing while reading as configured, a probe declared beside a state
source the fold also scores leaves one component holding two verdicts and each
substrate discarding a different one, and a component and its weight naming each other
only one way leaves the two substrates folding different maps for the same trial.

What the schema cannot answer is reported as :class:`Skip` and never raises, so
the gate has no false-reject mode. The severity of each rule is documented in
``docs/GRADING.md`` § "What is validated before a run".
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel

from tolokaforge.core.grading.golden_replay import (
    InitialStateSource,
    unreplayable_golden_source,
)
from tolokaforge.core.grading.grade_components import (
    COMPONENT_BY_NAME,
    GRADE_COMPONENTS,
    component_requested,
)
from tolokaforge.core.grading.state_composition import (
    CONFLICTING_STATE_SOURCES_MESSAGE,
    HASH_SOURCE_KEYS,
    probes_conflict_with_another_state_source,
)
from tolokaforge.core.models import (
    BoundValue,
    GradingCombineConfig,
    GradingFindingSeverity,
    ToolExpectations,
    TraceBinding,
    TraceChecksConfig,
    TraceConstraint,
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

    def declared_type(self, tool: str, argument: str) -> str | None:
        """The JSON type *tool*'s schema gives *argument*, where it gives one.

        ``None`` where the property writes no ``type`` or writes it as a union of
        several: neither settles that the value is never a string.
        """
        properties = self.parameters.get(tool, {}).get("properties")
        if not isinstance(properties, Mapping):
            return None
        declared = properties.get(argument)
        kind = declared.get("type") if isinstance(declared, Mapping) else None
        return kind if isinstance(kind, str) else None


@dataclass(frozen=True)
class CombineLayer:
    """The project layer beneath a task's own ``combine``, or the fact that none resolved.

    The weight rules need the *effective* combine, and the authored file holds only
    half of it: a task declaring no ``combine`` at all still inherits the project's
    weights. A caller that cannot say what the project layer is reports
    :meth:`unresolvable` — distinct from a caller reporting *no* project layer,
    because the two decide opposite things. Reading a project's defaults as absent
    would refuse every task that inherits its weights, which is why the two states
    cannot share one value.
    """

    project_combine: dict[str, Any] | None
    known: bool = True

    def __post_init__(self) -> None:
        if not self.known and self.project_combine:
            raise ValueError(
                "an unresolvable combine layer carries project defaults: every weight "
                f"rule is skipped, so {self.project_combine} would be resolved and then "
                "ignored. Report the defaults with known=True, or report nothing"
            )

    @classmethod
    def unresolvable(cls) -> CombineLayer:
        """The layer of a caller that cannot say what a task's project supplies."""
        return cls(project_combine=None, known=False)


@dataclass(frozen=True)
class ReplayWorld:
    """What a task gives a golden-action replay to be executed against.

    Two task-level facts, neither of them readable from ``grading.yaml``: what
    ``initial_state.json_db`` gives the replay to load, and whether
    ``tools.agent.mcp_server`` names the module holding the tools the actions call. A
    caller that cannot say what a task supplies reports :meth:`unresolvable` — distinct
    from a caller reporting a task that supplies neither, because the two decide
    opposite things: nothing is checkable against the first, while no golden replay can
    be built at all against the second.
    """

    initial_state: InitialStateSource
    mcp_server: bool
    known: bool = True

    def __post_init__(self) -> None:
        if not self.known and (
            self.initial_state is not InitialStateSource.ABSENT or self.mcp_server
        ):
            raise ValueError(
                "an unresolvable replay world carries task facts: the rule that reads them "
                f"is skipped, so initial_state={self.initial_state.value} / "
                f"mcp_server={self.mcp_server} would be resolved and then ignored. Report "
                "the facts with known=True, or report nothing"
            )

    @classmethod
    def unresolvable(cls) -> ReplayWorld:
        """The world of a caller that cannot say what a task gives a golden replay."""
        return cls(initial_state=InitialStateSource.ABSENT, mcp_server=False, known=False)


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

    def with_unchecked(self, *skips: Skip) -> AuthoringReport:
        """This report plus what the *caller* could not check on its own account.

        A rule the caller could not supply the inputs for is unchecked by the same
        definition as one this module could not answer, and belongs in the same
        channel — otherwise a caller that gated nothing reads as a clean bill of health.
        """
        return AuthoringReport(
            errors=self.errors, advisories=self.advisories, unchecked=self.unchecked + skips
        )


@dataclass(frozen=True)
class _MatcherSite:
    """One matcher, and the authored address it was read from."""

    where: str
    matcher: TraceMatcher


@dataclass(frozen=True)
class _PredicateSite:
    """One value predicate, the matcher field it reads, and its authored address."""

    where: str
    field: str
    predicate: ValuePredicate


@dataclass(frozen=True)
class _BindingSite:
    """One constraint's binder, and every predicate in its ``require`` that reads it.

    The references travel with the binder because a binding reaches exactly as far
    as the constraint declaring it, so the two are never paired across constraints.
    """

    where: str
    binding: TraceBinding
    references: tuple[_PredicateSite, ...]


@dataclass(frozen=True)
class _ResolvedTool:
    """The one tool an authored address is checked against, and what it declares."""

    name: str
    strictness: ArgumentSchema
    properties: frozenset[str]


_UNRESOLVABLE_REASON = (
    "the tool set of this task could not be resolved, so no tool name and no argument "
    "name in this block is checkable"
)

UNRESOLVED_COMBINE_REASON = (
    "no caller resolved this task's effective combine, so which configured components "
    "carry a weight — and which weights name a component the pack configures — is not "
    "checkable"
)
"""What a pack gate reports when it could not resolve the fold it is gating.

Reported by the caller through :meth:`AuthoringReport.with_unchecked`, because only a
caller gating a whole pack owes the answer: a caller checking a block *fragment*
against a recorded tool set performs no fold and is asked nothing about weights.
"""


_TRANSCRIPT_RULE_KEYS: tuple[str, ...] = (
    "must_contain",
    "disallow_regex",
    "max_turns",
    "min_assistant_turns",
    "required_actions",
    "communicate_info",
)
"""The flat ``transcript_rules`` keys that declare a rule over the transcript.

``tool_expectations`` is not among them because it declares its rules one level down,
in ``required_tools`` and ``disallowed_tools`` — a block carrying it empty asserts as
little as one omitting it. Held against ``TranscriptRulesConfig`` by the gate's unit
tests, so a rule key added to the model joins this tuple rather than reading as
nothing and refusing a pack that grades.
"""


# What each section must declare to assert anything, addressed to the author. Three
# entries, because the other two components answer this question at model
# construction: an empty ``trace_checks`` and an empty ``llm_judge`` are both
# unrepresentable, so no gate rule can reach one.
_WHAT_EACH_SECTION_MUST_DECLARE: Mapping[str, str] = {
    "state_checks": (
        "a non-empty jsonpaths list, a db_probes list, or hash.enabled beside an "
        f"{' or '.join(HASH_SOURCE_KEYS)}"
    ),
    "transcript_rules": (
        "a rule over the transcript — must_contain, disallow_regex, max_turns, "
        "min_assistant_turns, required_actions, communicate_info, or a tool_expectations "
        "carrying a required_tools or disallowed_tools entry"
    ),
    "custom_checks": (
        "enabled: true beside the file holding the checks, or enabled: false to record "
        "the opt-out explicitly"
    ),
}

_ASSERTS_NOTHING = (
    "the {section} block {because}, so it asserts nothing and scores nothing. "
    "Declare {what}, or drop the block"
)

_AN_EMPTY_BLOCK = "declares nothing at all"

_NO_STATE_SOURCE = "declares no source any substrate can read"

_NO_TRANSCRIPT_RULE = "declares no rule any substrate can evaluate"

_NO_OPT_IN_DECISION = "leaves enabled unwritten, which the component's own default reads as off"

# The half of the refusal only a report has room for: the shared message names the shape
# and the fix, and this says which substrate would have discarded which verdict.
_WHICH_SUBSTRATE_DISCARDS_WHICH = (
    "The two substrates would not even discard the same verdict: only the runner "
    "evaluates a probe, so runner-side the probe's score fills the component and the hash "
    "and jsonpath verdicts are dropped, while core has no probe evaluator and folds those "
    "two without it — one trial, two state_checks components."
)

# The JSON types whose values are never strings. A schema declaring one of them for
# an argument settles that a reference comparing the bound value against text cannot
# hold; ``string``, and a property writing no type at all, settle nothing.
_UNCORRELATABLE_JSON_TYPES: frozenset[str] = frozenset(
    {"integer", "number", "boolean", "array", "object"}
)

# The event fields ``TraceEvent`` declares as ``str | None``, so a predicate on one of
# them compares text whatever the value it is handed was typed as.
_TEXTUAL_MATCHER_FIELDS: frozenset[str] = frozenset({"tool", "text", "result"})

# What an argument name outside a closed schema costs, per site. One rule, two
# policies: an unreadable predicate leaves the matcher unmatched and an unreadable
# extraction leaves the binder unbound, and the author is pointed at the key that
# decides each.
_UNMATCHABLE_PREDICATE_HAZARD = (
    "the matcher selects nothing and the default on_missing reports that as the agent's failure"
)
_UNBINDABLE_EXTRACTION_HAZARD = (
    "the binding yields no assignment and the default on_unbound reports it as the agent's failure"
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

_A_HASH_SOURCE_NOTHING_READS = (
    "{key} is declared while hash.enabled is {enabled!r}: both substrates read the flag "
    "before any source, so the comparison never runs and the state is graded without it. "
    "Write enabled: true, or drop the source"
)

_GOLDEN_ACTION_NAME_ADDRESS = "state_checks.hash.golden_actions[{index}].name"

# What a golden action the replay cannot resolve costs. One tail for both shapes, since
# a name outside the declared set and no name at all are equally unreplayable and take
# the same fix; each shape supplies its own reason for that tail.
_UNREPLAYABLE_GOLDEN_ACTION_HAZARD = (
    "the replay refuses to build the golden world and the trial takes no state-hash "
    "verdict at all, once it is already paid for. Name a tool this task declares, or "
    "drop the action"
)

_UNDECLARED_GOLDEN_ACTION = (
    "golden action {name!r} is not declared by this task, which gives its actors "
    "{declared}: no actor can call it, so {hazard}"
)

_NAMELESS_GOLDEN_ACTION = (
    "this golden action names no tool to replay, and the task gives its actors "
    "{declared}: there is nothing for either substrate to resolve, so {hazard}"
)

_GOLDEN_ACTIONS_ADDRESS = "state_checks.hash.golden_actions"

# How a task withholding the initial state reads to the pack's author, per shape it
# withheld it in, and ``None`` for the shape that withholds nothing. Two shapes, one key:
# a replay loads a JSON file under the task directory, so an inline mapping supplies it as
# little as an unwritten key does, and an author fixes either by writing the same key with
# the other value. Total over :class:`InitialStateSource`, so a fourth shape cannot join
# the enum without an answer here.
_NO_INITIAL_STATE_FILE: Mapping[InitialStateSource, str | None] = {
    InitialStateSource.JSON_FILE: None,
    InitialStateSource.ABSENT: "this task declares no initial_state.json_db",
    InitialStateSource.INLINE: (
        "this task declares initial_state.json_db inline, where the replay loads a JSON "
        "file under the task directory"
    ),
}

_NO_MCP_SERVER_MODULE = (
    "this task declares no tools.agent.mcp_server, whose tools the golden actions call"
)

_UNBUILDABLE_GOLDEN_WORLD = (
    "{because}, so the golden actions have no world to be replayed in: core refuses to "
    "grade the trial at all rather than collecting the state_checks score the block's "
    "other sources earned beside a hash nothing computed, once the trial is already paid "
    "for. Write {key} in task.yaml, or drop the hash block"
)

_UNRESOLVED_REPLAY_WORLD_REASON = (
    "no caller resolved what this task gives a golden replay, so whether the replay has a "
    "world to be built in is not checkable"
)

# Bound once so the signature's default is the value, not a call in the annotation.
_UNRESOLVED_REPLAY_WORLD = ReplayWorld.unresolvable()


def inspect_grading_authoring(
    grading: Mapping[str, Any],
    inventory: ToolInventory,
    *,
    effective_combine: GradingCombineConfig | None = None,
    replay_world: ReplayWorld = _UNRESOLVED_REPLAY_WORLD,
) -> AuthoringReport:
    """Report what a task's tools, its replay world and its fold say about its grading block.

    The block is expected to have passed its own shape validation; the typed
    sub-blocks read here are constructed, so a malformed one raises its own load
    error rather than being reported as an authoring finding.

    Every rule that needs the task's tools is skipped into ``unchecked`` when the
    inventory is unresolvable. The rules outside that set still run: regex compilation,
    the hash-source declaration, the golden source's shape and the state-source
    exclusivity, which read nothing but the block, and the replay-world rule, which reads
    the world and skips on its own account.

    Args:
        grading: The authored block, as written.
        inventory: The task's tool set.
        effective_combine: The combine the task grades under, project defaults
            layered beneath its own. The weight rules read the *effective* map
            because a task declaring no ``combine`` at all still inherits one, so a
            rule reading the authored block would refuse a pack that grades fine.
            ``None`` leaves those two rules out entirely — the answer for a caller
            checking a block fragment against a recorded tool set, which performs no
            fold. A caller gating a whole pack that could not resolve one owes
            :data:`UNRESOLVED_COMBINE_REASON` through
            :meth:`AuthoringReport.with_unchecked`, so its gate does not read as a
            clean bill of health.
        replay_world: What the task gives its golden actions to be replayed against.
            The default, :meth:`ReplayWorld.unresolvable`, is the answer for a caller
            holding no ``task.yaml`` — it skips the one rule that reads the world where
            that rule would have run, and fails nothing.
    """
    constraints = tuple(_trace_constraints(grading))
    sites = tuple(_trace_matcher_sites(constraints))
    binders = tuple(_trace_binding_sites(constraints))
    rules = _transcript_rules(grading)
    reports = [
        _check_sections_declare_something(grading),
        _check_regex_compiles(sites, binders, rules.disallow_regex if rules else ()),
        _check_hash_source_declared(grading),
        _check_golden_actions_are_a_list(grading),
        _check_probes_are_the_only_state_source(grading),
        _check_golden_replay_world(grading, replay_world),
    ]
    if inventory.known:
        reports += [
            _check_tool_names(sites, inventory),
            _check_tool_expectation_names(rules.tool_expectations if rules else None, inventory),
            _check_golden_action_names(grading, inventory),
            _check_argument_paths(sites, inventory),
            _check_bound_extractions(binders, inventory),
        ]
    else:
        reports.append(AuthoringReport(unchecked=(Skip("grading", _UNRESOLVABLE_REASON),)))
    if effective_combine is not None:
        reports += [
            _check_requested_components_are_weighted(grading, effective_combine),
            _check_weights_name_requested_components(grading, effective_combine),
        ]
    return _merged(reports)


def _check_sections_declare_something(grading: Mapping[str, Any]) -> AuthoringReport:
    """A component section the author wrote declares something to evaluate.

    An empty block cannot survive translation: the wire erases an authored empty
    ``state_checks`` or ``transcript_rules`` to an absent section, so while the shape
    is representable no predicate can answer "did the author write this?" the same way
    on both substrates. Refusing it is what makes one predicate serve all three
    artifacts, and it finishes a call the project has already made twice — an empty
    ``trace_checks`` and an empty ``llm_judge`` are refused at model construction.

    All three sections carry the rule one level further, because each has keys that
    configure how a component runs rather than declaring what it checks: a
    ``state_checks`` holding only ``id_fields``, a ``transcript_rules`` whose every
    rule list is empty, and a ``custom_checks`` naming a file under no ``enabled``
    flag all assert exactly as little as an empty block, and each took a free pass for
    it — the first two scored a vacuous ``1.0``, and the third escapes the weight
    rules too, so a pack configuring nothing else folds as one asking for nothing.
    :data:`_A_NON_EMPTY_SECTION_STILL_DECLARES` holds the predicate and the sentence
    for each.

    The ``state_checks`` rule and :func:`_check_hash_source_declared` partition the
    unevaluable state blocks between them rather than overlapping — see
    :func:`_state_checks_has_a_source` for where the line falls — so each shape is
    refused once, at the key its own fix belongs to.
    """
    errors = tuple(
        Finding(
            section,
            _ASSERTS_NOTHING.format(section=section, because=because, what=what),
        )
        for section, what in _WHAT_EACH_SECTION_MUST_DECLARE.items()
        if (because := _why_a_section_asserts_nothing(section, grading.get(section))) is not None
    )
    return AuthoringReport(errors=errors)


def _why_a_section_asserts_nothing(section: str, written: Any) -> str | None:
    """Which of the two shapes *written* is, or ``None`` where it declares something.

    A section that is not a mapping is left alone: the block is expected to have
    passed its own shape validation, so a scalar there is a load error rather than an
    authoring finding.
    """
    if not isinstance(written, Mapping):
        return None
    if not written:
        return _AN_EMPTY_BLOCK
    declares_something, because = _A_NON_EMPTY_SECTION_STILL_DECLARES[section]
    return None if declares_something(written) else because


def _state_checks_has_a_source(state_checks: Mapping[str, Any]) -> bool:
    """Whether the block declares a state source at all.

    ``db_probes`` counts although only the runner can read one: that a probe's DSN
    resolves only inside the task's docker network is the documented substrate
    asymmetry, not something the author wrote wrong.

    A ``hash`` block counts as soon as it declares *either* the flag or something to
    compare against, which is what divides this rule from
    :func:`_check_hash_source_declared`: that rule owns every hash block whose two
    halves disagree, and this one owns a block that declares no source at all —
    including a hash block carrying neither. So each shape draws exactly one finding,
    and no shape draws none.

    Two sources are :func:`_check_probes_are_the_only_state_source`'s, which completes
    the partition from the other end: this rule owns a block with no source, that one a
    block with two, one of which is a probe.
    """
    hash_block = state_checks.get("hash")
    if not isinstance(hash_block, Mapping):
        hash_block = {}
    return bool(
        state_checks.get("jsonpaths")
        or state_checks.get("db_probes")
        or hash_block.get("enabled")
        or any(hash_block.get(key) for key in HASH_SOURCE_KEYS)
    )


def _transcript_rules_asserts_something(transcript_rules: Mapping[str, Any]) -> bool:
    """Whether the block declares a rule over the transcript at all.

    Every key is read for truth rather than for presence, because that is what both
    substrates do: an empty ``required_actions`` list requires no action and an empty
    ``must_contain`` list demands no phrase, so a block holding only empty lists is
    scored against nothing and takes the vacuous ``1.0`` that averaging an empty
    sub-check set produces. ``max_turns`` and ``min_assistant_turns`` are bounds
    rather than lists, and a bound of ``0`` admits every turn count from either side,
    so truthiness is the right reading for them too.

    ``tool_expectations`` is read one level down, through the same two keys the
    tool-name rule addresses, because the block itself declares nothing — its two
    lists do.
    """
    expectations = transcript_rules.get("tool_expectations") or {}
    return bool(
        any(transcript_rules.get(key) for key in _TRANSCRIPT_RULE_KEYS)
        or any(expectations.get(key) for key in _TOOL_EXPECTATION_HAZARDS)
    )


def _custom_checks_decides_its_opt_in(custom_checks: Mapping[str, Any]) -> bool:
    """Whether the block says, either way, that its suite runs.

    Presence rather than truth, because ``enabled: false`` *is* a decision — it
    survives the wire intact and both substrates read it as an opt-out. What asserts
    nothing is the block that leaves the key out: ``CustomChecksConfig.enabled``
    defaults to ``False``, so the suite never runs, no weight is owed for it, and a
    pack naming a ``checks.py`` grades as though it had named none.
    """
    return "enabled" in custom_checks


# What each section must still declare once it is non-empty, and the sentence for the
# block that does not. Total over :data:`_WHAT_EACH_SECTION_MUST_DECLARE`, so a
# component whose empty block stops being refused at model construction cannot join
# that table without an answer here.
_A_NON_EMPTY_SECTION_STILL_DECLARES: Mapping[
    str, tuple[Callable[[Mapping[str, Any]], bool], str]
] = {
    "state_checks": (_state_checks_has_a_source, _NO_STATE_SOURCE),
    "transcript_rules": (_transcript_rules_asserts_something, _NO_TRANSCRIPT_RULE),
    "custom_checks": (_custom_checks_decides_its_opt_in, _NO_OPT_IN_DECISION),
}


def _check_requested_components_are_weighted(
    grading: Mapping[str, Any], combine: GradingCombineConfig
) -> AuthoringReport:
    """A component the pack configures carries a weight in the effective combine.

    Configuring a section asks for the component to be scored; declaring no weight
    for it leaves nothing in the config saying what share of the trial's score that
    verdict carries, and the two substrates do not answer that the same way.
    """
    errors = tuple(
        Finding(
            f"combine.weights.{spec.name}",
            f"the {spec.config_section} section is configured, so the {spec.name} component "
            f"is scored, but the effective combine.weights declares {sorted(combine.weights)} "
            f"and no weight for it: nothing in the config says what share of the score "
            f"{spec.name} carries, and the two substrates do not answer that the same way. "
            f"Declare combine.weights.{spec.name}, or drop the {spec.config_section} section",
        )
        for spec in GRADE_COMPONENTS
        if component_requested(spec, grading.get(spec.config_section))
        and spec.name not in combine.weights
    )
    return AuthoringReport(errors=errors)


def _check_weights_name_requested_components(
    grading: Mapping[str, Any], combine: GradingCombineConfig
) -> AuthoringReport:
    """A weight in the effective combine names a component the pack configures.

    The converse of :func:`_check_requested_components_are_weighted`, and refused for
    the same reason in the other direction: no substrate produces a component the
    pack never configured, so the weight weighs nothing an author can see.
    """
    requested = frozenset(
        spec.name
        for spec in GRADE_COMPONENTS
        if component_requested(spec, grading.get(spec.config_section))
    )
    errors = tuple(
        Finding(f"combine.weights.{name}", _unrequested_weight_message(name, requested))
        for name in sorted(combine.weights)
        if name not in requested
    )
    return AuthoringReport(errors=errors)


def _unrequested_weight_message(name: str, requested: frozenset[str]) -> str:
    """Why one weight key weighs nothing, and which of the two fixes applies.

    A key naming no component at all is the same defect one step further out — the
    weight is unread either way — but it takes the other fix, because there is no
    section to configure.
    """
    if name not in COMPONENT_BY_NAME:
        return (
            f"combine.weights declares a weight for {name!r}, which names no grading "
            f"component: the components are {sorted(COMPONENT_BY_NAME)}. A weight no "
            "substrate reads folds nothing. Correct the name, or drop the weight"
        )
    section = COMPONENT_BY_NAME[name].config_section
    return (
        f"combine.weights declares a weight for {name}, which this pack does not configure: "
        f"its {section} section is absent or opted out and the pack configures "
        f"{sorted(requested)}, so no substrate produces a {name} component and the weight "
        f"weighs nothing. Configure the {section} section, or drop the weight"
    )


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
    sites: tuple[_MatcherSite, ...],
    binders: tuple[_BindingSite, ...],
    disallow_regex: Iterable[str],
) -> AuthoringReport:
    """Every authored pattern compiles here, or it raises inside the evaluator.

    Neither substrate catches ``re.error`` locally: core lets it propagate out of
    the grader and the runner folds it into a failed grade response, so the trial
    is lost rather than the constraint. A binder's capture pattern is compiled by
    the same evaluator on the same trial, so it is read here for the same reason.
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
    authored += [
        (f"{site.where}.values.{name}.pattern", value.pattern)
        for site in binders
        for name, value in site.binding.values.items()
        if value.pattern is not None
    ]
    findings = (_uncompilable(where, pattern) for where, pattern in authored)
    return AuthoringReport(errors=tuple(finding for finding in findings if finding is not None))


def _check_hash_source_declared(grading: Mapping[str, Any]) -> AuthoringReport:
    """The hash check and something to compare against are declared together.

    Either half alone grades the state without the comparison the author wrote. A
    source under a disabled flag is never read, whichever of
    :data:`~tolokaforge.core.grading.state_composition.HASH_SOURCE_KEYS` carries it:
    both substrates test the flag first, so the pack grades in silence without it. An
    enabled flag with no source is the same defect from the other side, and it also
    splits the two substrates — core produces no hash verdict at all while the runner
    compares the trial against the initial state, so the same trial takes two different
    ``state_checks`` components.

    A block declaring two inert sources is one defect taking one edit, so it draws one
    finding, addressed at the first source the tuple names. That order is **core's** read
    order and no more: ``_check_state_hash`` compares a truthy ``expected_state_hash`` in
    process and returns before ``golden_actions``, where no runner path reads the
    translated ``expected_hash`` at all —
    :data:`~tolokaforge.core.grading.key_manifest._HASH_SOURCE_SHAPE_REASON` records that
    asymmetry under #693. What the message claims of both substrates is only what holds
    of both: the flag is read before any source.

    Both halves read the flag for truth rather than for ``True``, because that is
    what decides the grade: core branches on its truthiness and the runner coerces
    it, so a pack written ``enabled: 1`` does read the hash and rejecting it here
    would be stricter than either substrate. A source is read the same way — an
    empty ``golden_actions`` list replays nothing, which is why both substrates
    treat it as no source at all and why such a block is
    :func:`_check_sections_declare_something`'s rather than this rule's.
    """
    hash_block = _hash_block(grading)
    if hash_block is None:
        return AuthoringReport()
    enabled = hash_block.get("enabled")
    declared = next((key for key in HASH_SOURCE_KEYS if hash_block.get(key)), None)
    if enabled and declared is None:
        sources = " or ".join(HASH_SOURCE_KEYS)
        return AuthoringReport(
            errors=(
                Finding(
                    "state_checks.hash.enabled",
                    f"hash grading is enabled but the block declares no {sources} to "
                    "compare against: core produces no hash verdict at all while the "
                    "runner compares the trial against its initial state, so the same "
                    "trial takes two different state_checks components. Declare "
                    f"{sources}, or drop the hash block",
                ),
            )
        )
    if enabled or declared is None:
        return AuthoringReport()
    return AuthoringReport(
        errors=(
            Finding(
                f"state_checks.hash.{declared}",
                _A_HASH_SOURCE_NOTHING_READS.format(key=declared, enabled=enabled),
            ),
        )
    )


def _hash_block(grading: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The authored ``state_checks.hash`` block, or ``None`` where the pack wrote none.

    Every hash rule reads it through here, so a scalar written at either level is left
    to the block's own shape validation rather than read as an authoring defect.
    """
    state_checks = grading.get("state_checks")
    if not isinstance(state_checks, Mapping):
        return None
    hash_block = state_checks.get("hash")
    return hash_block if isinstance(hash_block, Mapping) else None


def _check_golden_actions_are_a_list(grading: Mapping[str, Any]) -> AuthoringReport:
    """A declared golden replay is the list of actions to replay.

    The one hash rule that reads a source for its *shape* rather than for truth, and it
    reads only the block: whether a value is a list needs no tool set and no replay world,
    so a shape defect is refused for a pack whose adapter can report neither rather than
    skipped with the rules that do need them.

    Read under a truthy ``hash.enabled`` and then **whatever else the block declares**,
    unlike :func:`_check_golden_replay_world` beside it. A truthy ``expected_state_hash``
    is the source core reads, so core never reaches the actions — but
    ``NativeAdapter.to_task_description`` iterates the authored value with no such
    short-circuit, so a pack declaring both cannot be registered at all. Skipping the
    literal here would accept it.

    Independent of the replay-world rule for the reason that rule reports every withheld
    fact at once: a truthy non-list under an incomplete world draws two findings at this
    one address, because both statements are true and each names a different fix. At grade
    time core answers only the first, the shape being refused above the world it would
    otherwise need — an author holding the whole list is what the gate is for, and the
    first unbuildable precondition is the whole answer once a trial is paid for.

    A falsy value is no replay rather than a malformed one — see
    :func:`~tolokaforge.core.grading.golden_replay.unreplayable_golden_source` for the
    reading — so such a block is :func:`_check_sections_declare_something`'s where it
    declares no other source, and nothing at all where it declares one.
    """
    hash_block = _hash_block(grading)
    if hash_block is None or not hash_block.get("enabled"):
        return AuthoringReport()
    reason = unreplayable_golden_source(hash_block.get("golden_actions"))
    if reason is None:
        return AuthoringReport()
    return AuthoringReport(errors=(Finding(_GOLDEN_ACTIONS_ADDRESS, reason),))


def _check_probes_are_the_only_state_source(grading: Mapping[str, Any]) -> AuthoringReport:
    """``db_probes`` is the whole of what a block declaring it may declare.

    A probe beside a source the fold also scores hands one ``state_checks`` component two
    candidate scores with no share to fold them by, and both config models refuse the
    block at load — so an advisory here would tell the author their pack is fine while
    ``tolokaforge validate`` and the run's pre-flight reject it.

    Which means this finding reaches only a caller reading a block *fragment* without
    constructing it — :mod:`tolokaforge.core.grading.trace_replay` and
    :mod:`tolokaforge.core.grading.rubric_migration`, over a recorded bundle.
    :func:`~tolokaforge.adapters._task_loader.validate_grading_yaml` constructs
    ``StateChecksConfig`` on every declared block, so it hears the model's load error
    first and this rule never runs for it. The duplication is deliberate: it is what
    lets a corpus tool report the shape rather than abort on it.

    The mirror of the no-source rule rather than an extension of it: that one owns a block
    declaring no source at all, this one a block declaring two, one of which is a probe —
    see :func:`_state_checks_has_a_source` for the whole partition. Reads the block alone,
    like :func:`_check_hash_source_declared` beside it: no tool name, no replay world, no
    fold.
    """
    state_checks = grading.get("state_checks")
    if not isinstance(state_checks, Mapping):
        return AuthoringReport()
    if not probes_conflict_with_another_state_source(
        db_probes=state_checks.get("db_probes") or (),
        jsonpaths=state_checks.get("jsonpaths") or (),
        hash_config=_hash_block(grading),
    ):
        return AuthoringReport()
    return AuthoringReport(
        errors=(
            Finding(
                "state_checks.db_probes",
                f"{CONFLICTING_STATE_SOURCES_MESSAGE} {_WHICH_SUBSTRATE_DISCARDS_WHICH}",
            ),
        )
    )


def _check_golden_replay_world(grading: Mapping[str, Any], world: ReplayWorld) -> AuthoringReport:
    """A pack replaying golden actions is authored against a task that gives them a world.

    The block is read in the order core reads it — the flag, then ``expected_state_hash``,
    then ``golden_actions`` — the runner having no literal-first order to share, since no
    path there reads the translated ``expected_hash`` (#693). A falsy ``hash.enabled`` is
    a source nobody resolves, for the reason :func:`_check_hash_source_declared` gives at
    length. A truthy ``expected_state_hash`` is the *effective* source whatever else the
    block declares — core compares the trial against the author's literal and returns
    before ``golden_actions`` is read — so such a pack needs no world, and refusing it
    would send its author to declare facts nothing consults. Only then are the golden
    actions read, for truthiness and never for shape: a truthy non-list value is refused
    here for the world it lacks and by :func:`_check_golden_actions_are_a_list` for being
    no list of actions, so under an incomplete world it draws both findings at this one
    address, while :func:`_check_golden_action_names` reports nothing about it at all,
    having no element to address.

    Each fact the task withholds is its own finding, addressed to the ``task.yaml`` key
    that supplies it, because an author fixing a pack one exception at a time pays a
    whole grading pass per omission — the cost
    :func:`resolve_golden_action_names` already refuses to charge for action names.

    Resolving the world is the caller's: one that cannot say what a task gives a replay
    skips this rule where it would have run and fails nothing, exactly like an
    unresolvable tool set — and only there, so a pack that replays nothing reports no
    skip for a rule that had nothing to check.
    """
    hash_block = _hash_block(grading)
    if hash_block is None or not hash_block.get("enabled"):
        return AuthoringReport()
    if hash_block.get("expected_state_hash") or not hash_block.get("golden_actions"):
        return AuthoringReport()
    if not world.known:
        return AuthoringReport(
            unchecked=(Skip(_GOLDEN_ACTIONS_ADDRESS, _UNRESOLVED_REPLAY_WORLD_REASON),)
        )
    return AuthoringReport(
        errors=tuple(
            Finding(
                _GOLDEN_ACTIONS_ADDRESS,
                _UNBUILDABLE_GOLDEN_WORLD.format(because=because, key=key),
            )
            for because, key in _withheld_replay_facts(world)
        )
    )


def _withheld_replay_facts(world: ReplayWorld) -> Iterator[tuple[str, str]]:
    """Each fact the task does not give the replay, and the ``task.yaml`` key that would."""
    withheld_state = _NO_INITIAL_STATE_FILE[world.initial_state]
    if withheld_state is not None:
        yield withheld_state, "initial_state.json_db"
    if not world.mcp_server:
        yield _NO_MCP_SERVER_MODULE, "tools.agent.mcp_server"


def _check_golden_action_names(
    grading: Mapping[str, Any], inventory: ToolInventory
) -> AuthoringReport:
    """A golden action may only name a tool some actor of the task can call.

    A name that resolves to nothing is refused by both substrates before the first
    action runs, so the whole trial is paid for and left with no state-hash verdict —
    core raises out of the grading engine, the runner answers ``GradeTrial`` with
    ``success=false``. The gate is where that costs nothing.

    Read only under a truthy ``hash.enabled``, the flag both substrates test before
    they read any source, for the reason :func:`_check_hash_source_declared` gives at
    length: a name under a disabled flag is never resolved, so refusing it would be
    stricter than the grade. That block is refused there instead, at the source key the
    flag stops anything from reading rather than at any name it carries.

    Names resolve against the tools the task *declares*, which is stricter than either
    replay substrate — core resolves against the pack's ``TOOLS`` map and the runner
    against the tools it registered for the trial, and neither is readable here without
    importing the pack's server module. #815 owns unifying the three.

    A name that is not a string at all — the block being untyped — is refused as one
    resolving to nothing rather than tested for membership, which an unhashable value
    answers with a ``TypeError``.
    """
    errors = tuple(
        Finding(
            _GOLDEN_ACTION_NAME_ADDRESS.format(index=index),
            _unreplayable_golden_action_message(name, inventory),
        )
        for index, name in enumerate(_authored_golden_action_names(grading))
        if not name or not isinstance(name, str) or name not in inventory.declared
    )
    return AuthoringReport(errors=errors)


def _authored_golden_action_names(grading: Mapping[str, Any]) -> Iterator[Any]:
    """Each golden action's name as written, in the order the replay would run them.

    Nothing at all where the flag is falsy, so the caller reads only the source a
    substrate would read. An action that is not a mapping, and one carrying no ``name``,
    both yield ``None``: the ``hash`` block is untyped (#730), so there is no load error
    to defer to, and the index of the offending action is what an author acts on.
    """
    hash_block = _hash_block(grading)
    if hash_block is None or not hash_block.get("enabled"):
        return
    actions = hash_block.get("golden_actions")
    if not isinstance(actions, list):
        return
    for action in actions:
        yield action.get("name") if isinstance(action, Mapping) else None


def _unreplayable_golden_action_message(name: Any, inventory: ToolInventory) -> str:
    """Which of the two unreplayable shapes one action is, and its fix."""
    declared = sorted(inventory.declared)
    if not name:
        return _NAMELESS_GOLDEN_ACTION.format(
            declared=declared, hazard=_UNREPLAYABLE_GOLDEN_ACTION_HAZARD
        )
    return _UNDECLARED_GOLDEN_ACTION.format(
        name=name, declared=declared, hazard=_UNREPLAYABLE_GOLDEN_ACTION_HAZARD
    )


def _check_bound_extractions(
    binders: tuple[_BindingSite, ...], inventory: ToolInventory
) -> AuthoringReport:
    """What a tool's schema says about the values a binder draws out of its calls.

    Two answers off the one resolved schema: the extraction addresses an argument
    the tool declares, and the type declared there can be compared against the
    fields the constraint references the name from.
    """
    return _merged(
        _one_extraction(f"{site.where}.values.{name}.field", name, value, site, inventory)
        for site in binders
        for name, value in site.binding.values.items()
    )


def _one_extraction(
    where: str, name: str, value: BoundValue, site: _BindingSite, inventory: ToolInventory
) -> AuthoringReport:
    """One extraction against the schema of the tool its binder selects.

    Only an ``args`` extraction has a declared type at all: ``tool``, ``text`` and
    ``result`` are the event's own string fields, which no tool schema describes
    and which every reference compares correctly.
    """
    if value.head_segment() != "args":
        return AuthoringReport()
    resolved = _resolved_tool(site.binding.match, inventory, where)
    if isinstance(resolved, Skip):
        return AuthoringReport(unchecked=(resolved,))
    _, _, path = value.field.partition(".")
    if not path:
        return _uncorrelatable_extraction(where, name, value, "object", resolved, site.references)
    addressed = _one_argument_path(where, path, resolved, _UNBINDABLE_EXTRACTION_HAZARD)
    if addressed != AuthoringReport():
        return addressed
    declared = inventory.declared_type(resolved.name, path)
    return _uncorrelatable_extraction(where, name, value, declared, resolved, site.references)


def _uncorrelatable_extraction(
    where: str,
    name: str,
    value: BoundValue,
    declared: str | None,
    resolved: _ResolvedTool,
    references: tuple[_PredicateSite, ...],
) -> AuthoringReport:
    """A value the schema types as non-text, read by a reference that compares text.

    ``contains`` reads two strings as a substring pair and falls back to equality
    for every other pairing, and ``equals_binding`` is that same equality, so both
    are false on every trajectory — a check indistinguishable from the agent
    failing. A ``pattern`` on the extraction binds a capture, which is a string.
    """
    if value.pattern is not None:
        return AuthoringReport()
    textual = _textual_references(name, references)
    if not textual:
        return AuthoringReport()
    read_from = sorted(site.where for site in textual)
    if declared is None:
        return AuthoringReport(
            unchecked=(
                Skip(
                    where,
                    f"{resolved.name!r} declares no single type for {value.field!r} — no "
                    f"type at all, or a union of several — so whether {read_from} can ever "
                    "hold is not checkable",
                ),
            )
        )
    if declared not in _UNCORRELATABLE_JSON_TYPES:
        return AuthoringReport()
    finding = Finding(
        where,
        f"binding {name!r} extracts {value.field!r}, which {resolved.name!r} declares as "
        f"type {declared!r}, and {read_from} compares it against a field holding text. A "
        "non-string value is never a substring and equals nothing a text field holds, so "
        "the check is false on every trajectory and reads as the agent's failure. Reference "
        "the binding from an args predicate, which compares two arguments as they were "
        "written, or bind a regex capture, which is always text",
    )
    if resolved.strictness is ArgumentSchema.CLOSED:
        return AuthoringReport(errors=(finding,))
    return AuthoringReport(advisories=(finding,))


def _textual_references(
    name: str, references: tuple[_PredicateSite, ...]
) -> tuple[_PredicateSite, ...]:
    """Every predicate reading *name* against a value that is text.

    A ``regex`` beside the reference says the same of an argument the schema types
    loosely: a pattern only ever holds against a string.
    """
    return tuple(
        site
        for site in references
        if name in site.predicate.referenced_bindings()
        and (site.field in _TEXTUAL_MATCHER_FIELDS or site.predicate.regex is not None)
    )


def _resolved_tool(
    matcher: TraceMatcher, inventory: ToolInventory, where: str
) -> _ResolvedTool | Skip:
    """The tool whose schema answers for *matcher*'s arguments, or why none does."""
    named = frozenset(_tool_names_asserted_by(matcher.tool)) if matcher.tool else frozenset()
    if len(named) != 1:
        return Skip(
            where,
            "the matcher does not name one tool, so which schema declares its arguments "
            "is undecided",
        )
    name = next(iter(named))
    strictness = inventory.strictness(name)
    if strictness is ArgumentSchema.UNKNOWN:
        return Skip(where, f"no schema resolved for {name!r}, so its arguments are not checkable")
    return _ResolvedTool(name, strictness, inventory.properties(name))


def _one_matchers_argument_paths(site: _MatcherSite, inventory: ToolInventory) -> AuthoringReport:
    """The argument half of one matcher, once the tool it addresses is decided."""
    where = f"{site.where}.args"
    resolved = _resolved_tool(site.matcher, inventory, where)
    if isinstance(resolved, Skip):
        return AuthoringReport(unchecked=(resolved,))
    return _merged(
        _one_argument_path(f"{where}.{path}", path, resolved, _UNMATCHABLE_PREDICATE_HAZARD)
        for path in site.matcher.args or ()
    )


def _one_argument_path(
    where: str, path: str, resolved: _ResolvedTool, hazard: str
) -> AuthoringReport:
    head, _, below = path.partition(".")
    if head not in resolved.properties:
        return _unknown_argument_report(where, head, resolved, hazard)
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
    where: str, head: str, resolved: _ResolvedTool, hazard: str
) -> AuthoringReport:
    declared = f"{resolved.name!r} declares {sorted(resolved.properties)}"
    if resolved.strictness is ArgumentSchema.CLOSED:
        return AuthoringReport(
            errors=(
                Finding(
                    where,
                    f"argument {head!r} is not one {declared} and its schema admits no "
                    f"other, so {hazard}",
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


def _trace_constraints(grading: Mapping[str, Any]) -> Iterator[tuple[str, TraceConstraint]]:
    """Every constraint the block declares, shared and per-route, with its address.

    A route's constraints are graded exactly as the shared ones are, so a typo
    inside one is the same defect — and the route id joins the address because the
    block's one id space is what keeps the two forms apart.
    """
    block = grading.get("trace_checks")
    if not isinstance(block, Mapping):
        return
    config = TraceChecksConfig(**block)
    for constraint in config.constraints:
        yield f"trace_checks.{constraint.id}", constraint
    for path in config.alternatives or ():
        for constraint in path.constraints:
            yield f"trace_checks.{path.id}.{constraint.id}", constraint


def _trace_matcher_sites(
    constraints: Iterable[tuple[str, TraceConstraint]],
) -> Iterator[_MatcherSite]:
    """Every matcher a constraint declares, wherever on the constraint it lives.

    Structural over the constraint's own fields, so a matcher-bearing field added
    to the vocabulary is walked without a second table to keep in step. ``require``
    alone is elided from the address, because the kind it declares is the segment
    an author reads a finding by.
    """
    for where, constraint in constraints:
        for name in type(constraint).model_fields:
            below = where if name == "require" else f"{where}.{name}"
            yield from _matcher_sites(getattr(constraint, name), below)


def _trace_binding_sites(
    constraints: Iterable[tuple[str, TraceConstraint]],
) -> Iterator[_BindingSite]:
    """Every binder a constraint declares, with the predicates reading its names."""
    for where, constraint in constraints:
        if constraint.bind is None:
            continue
        yield _BindingSite(
            where=f"{where}.bind",
            binding=constraint.bind,
            references=tuple(
                predicate_site
                for matcher_site in _matcher_sites(constraint.require, where)
                for predicate_site in _predicate_sites(matcher_site)
                if predicate_site.predicate.referenced_bindings()
            ),
        )


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
            yield _PredicateSite(f"{site.where}.{name}", name, value)
        elif isinstance(value, Mapping):
            for path, predicate in value.items():
                yield _PredicateSite(f"{site.where}.{name}.{path}", name, predicate)


def _merged(reports: Iterable[AuthoringReport]) -> AuthoringReport:
    collected = tuple(reports)
    return AuthoringReport(
        errors=tuple(finding for report in collected for finding in report.errors),
        advisories=tuple(finding for report in collected for finding in report.advisories),
        unchecked=tuple(skip for report in collected for skip in report.unchecked),
    )
