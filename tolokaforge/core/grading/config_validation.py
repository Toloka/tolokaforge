"""Checking grading authoring against what a task gives its actors, its replay and its fold.

Substrate-neutral and pure — no adapter, no filesystem: an adapter resolves the
task's tool set into a :class:`ToolInventory`, the world its golden actions replay in
into a :class:`ReplayWorld`, the tables it seeds into a :class:`SeededTablesLayer` and
the task's effective ``combine``, and :func:`inspect_grading_authoring` reads only
those. A tool set the adapter cannot report is :meth:`ToolInventory.unresolvable` —
distinct from a task that declares no tools, because the two decide opposite things:
nothing is checkable against the first, while every tool name is wrong against the
second. A replay world and a seeded-tables layer read the same way through their own
``unresolvable()``, and an effective combine no caller could resolve is ``None``.

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
substrate discarding a different one, a primary key declared over records the task
never seeds addresses no row for a write or a diff, and a component and its weight
naming each other only one way leaves the two substrates folding different maps for
the same trial.

What the schema cannot answer is reported as :class:`Skip` and never raises, so
the gate has no false-reject mode. The severity of each rule is documented in
``docs/GRADING.md`` § "What is validated before a run".
"""

from __future__ import annotations

import logging
import re
import types
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Union, get_args, get_origin, get_type_hints

from pydantic import BaseModel, ValidationError

from tolokaforge.core.grading.golden_replay import (
    InitialStateSource,
    unreplayable_golden_source,
)
from tolokaforge.core.grading.grade_components import (
    COMPONENT_BY_NAME,
    GRADE_COMPONENTS,
    component_requested,
)
from tolokaforge.core.grading.jsonpath_addressing import (
    addresses_the_database,
    block_addresses_the_database,
    unreachable_target,
)
from tolokaforge.core.grading.predicates import JSON_TYPES, ever_satisfiable
from tolokaforge.core.grading.state_composition import (
    CONFLICTING_STATE_SOURCES_MESSAGE,
    HASH_SOURCE_KEYS,
    StateHashConfig,
    hash_block_is_a_state_source,
    probes_conflict_with_another_state_source,
)
from tolokaforge.core.grading.trace_timeline import TraceEvent
from tolokaforge.core.models import (
    REQUESTOR_TO_EXECUTOR,
    BoundValue,
    GradingCombineConfig,
    GradingFindingSeverity,
    RequiredAction,
    ToolExecutorIdentity,
    ToolExpectations,
    TraceBinding,
    TraceChecksConfig,
    TraceConstraint,
    TraceConstraintExpr,
    TraceMatcher,
    TranscriptRulesConfig,
    ValuePredicate,
)
from tolokaforge.runner.id_resolution import IdFieldResolutionError, id_fields_findings
from tolokaforge.runner.models import TRACE_PREDICATE_BINDING_OPERATORS

logger = logging.getLogger(__name__)


class ArgumentSchema(str, Enum):
    """What a tool's resolved schema can answer about an argument name."""

    CLOSED = "closed"
    """``additionalProperties: false`` — a name outside ``properties`` is wrong."""

    OPEN = "open"
    """Properties are known but extras are permitted — an unknown name is suspect."""

    UNKNOWN = "unknown"
    """No schema resolved — nothing about this tool's arguments is checkable."""


class SkipKind(str, Enum):
    """Why the gate could not check something, sorted by whose absence is speaking.

    The two readings decide opposite things about enforcement: an environment that
    cannot inspect a pack it does not host must not refuse it, but an adapter that
    is loaded and answers ``unresolvable()`` for a layer is speaking on its own
    account and can be held to it by an author who names the adapter in their CI.
    """

    STRUCTURAL = "structural"
    """The environment cannot inspect this pack: the adapter is uninstalled, the
    ``adapter_type`` is misspelled, or a schema this reading cannot resolve. Never
    fatal — an environment without the adapter must not refuse a pack it cannot
    see. This is what ``Skip`` reads by default, because every skip today is
    produced when the environment could not inspect."""

    ADAPTER_DECLARED = "adapter_declared"
    """The adapter is loaded and its hook returned :meth:`unresolvable`, so the
    silence is the adapter's own. Reported never-fatal by default; promotable to
    fatal by a caller targeting the adapter."""


@dataclass(frozen=True)
class ToolInventory:
    """The tool set a task gives its actors, and what each tool's schema says."""

    declared: frozenset[str]
    """Every tool the task gives an actor: :attr:`agent_declared` ∪ :attr:`user_declared`."""

    agent_declared: frozenset[str]
    """``tools.agent.enabled`` — what the agent may call."""

    user_declared: frozenset[str]
    """``tools.user.enabled`` — what the user simulator may call."""

    actor_split_known: bool
    """Whether :attr:`agent_declared` / :attr:`user_declared` are the task's own split.

    ``False`` for a producer that can report the tool set but not whose it was — a
    recorded trial's wire tool list carries no actor. Such a producer files the whole
    set under the agent because a set has to go somewhere, and that placement is not
    a claim: :meth:`declared_by` refuses to answer, and a rule about *which* actor
    declared a tool reports unchecked instead of refusing the author over a fact the
    inventory does not hold.
    """

    parameters: Mapping[str, Mapping[str, Any]]
    """Tool name to its JSON-schema parameters object, for the tools that resolved."""

    known: bool
    """``False`` only for :meth:`unresolvable`."""

    skip_kind: SkipKind = SkipKind.STRUCTURAL
    """The kind of skip a rule reading this inventory reports where it cannot check.

    Read only by callers producing a :class:`Skip` because this layer answered
    ``known=False``; unused on a resolved inventory, defaulted to
    :attr:`SkipKind.STRUCTURAL` to keep the constructor uniform.
    """

    def __post_init__(self) -> None:
        carried = sorted(self.declared | self.agent_declared | self.user_declared)
        if not self.known and (carried or self.parameters):
            raise ValueError(
                "an unresolvable inventory carries tools: every rule that reads them is "
                f"skipped, so {carried or sorted(self.parameters)} would be "
                "resolved and then ignored. Report the tools with known=True, or report "
                "nothing"
            )
        if not self.known and self.actor_split_known:
            raise ValueError(
                "an inventory that reports no tools claims to know whose they are. "
                "Nothing is known of an unresolvable tool set, its split least of all"
            )
        if self.declared != self.agent_declared | self.user_declared:
            raise ValueError(
                "the declared tool set is not the union of the actors' sets: "
                f"{sorted(self.declared)} against agent {sorted(self.agent_declared)} and "
                f"user {sorted(self.user_declared)}. A rule reading one and a rule reading "
                "the other would decide the same name differently"
            )

    @classmethod
    def unresolvable(cls, kind: SkipKind = SkipKind.ADAPTER_DECLARED) -> ToolInventory:
        """The inventory of an adapter that cannot report a tool set at all.

        *kind* tags whose silence this is: the default,
        :attr:`SkipKind.ADAPTER_DECLARED`, is for an adapter whose hook returned
        this value; a caller reading an unresolvable inventory *because* it holds
        no adapter passes :attr:`SkipKind.STRUCTURAL` instead.
        """
        return cls(
            declared=frozenset(),
            agent_declared=frozenset(),
            user_declared=frozenset(),
            actor_split_known=False,
            parameters={},
            known=False,
            skip_kind=kind,
        )

    def declared_by(self, executor: ToolExecutorIdentity) -> frozenset[str]:
        """The tools ``tools.<executor>.enabled`` gives that one actor.

        Keyed off the recorded executor identity rather than taking a field name,
        so a rule reading what an actor may call and a record saying who called it
        speak one vocabulary.

        Raises:
            ValueError: If :attr:`actor_split_known` is ``False``. The sets exist
                either way, so answering would hand a caller the agent's whole set
                as the agent's own — the false certainty this method must not sell.
        """
        if not self.actor_split_known:
            raise ValueError(
                "this inventory does not know which actor declared what, so it cannot "
                f"answer what {executor.value!r} may call. Read `declared`, or route the "
                "question to the report's unchecked channel"
            )
        return {
            ToolExecutorIdentity.AGENT: self.agent_declared,
            ToolExecutorIdentity.USER: self.user_declared,
        }[executor]

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


class SuppliedSourceState(str, Enum):
    """Whether a source an adapter supplies beneath the authored block grades anything."""

    USABLE = "usable"
    """Present and non-empty: the hash has something to compare the trial against."""

    MISSING = "missing"
    """Nothing is at the place the adapter reads its source from."""

    EMPTY = "empty"
    """The source is there and holds nothing, which compares against as little as none."""


@dataclass(frozen=True)
class AdapterHashSource:
    """The source an adapter supplies beneath the authored block, in the adapter's words.

    *where* is what a refusal names — a path in the adapter's own vocabulary, relative
    to the task directory, because that is what an author has to go and fix.
    """

    where: str
    state: SuppliedSourceState


@dataclass(frozen=True)
class HashSourceLayer:
    """What supplies a hash source beneath a task's authored ``state_checks.hash`` block.

    Three answers, because they decide three different things. A caller that cannot say
    what the grading adapter supplies reports :meth:`unresolvable`, which makes the
    sourceless shape uncheckable. A caller resolving *nothing* beneath the block — the
    native reading, where the authored keys are the only place a source can come from —
    reports the default construction, which makes that same shape the authoring defect
    :func:`_check_hash_source_declared` refuses. An adapter that computes the source it
    compares against from its own fixtures — the frozen-core family replays a
    golden-actions fixture the authored block never names — reports it as *supplied*,
    and then the block grades exactly as well as that source does: usable passes,
    missing or empty is refused before the trial is paid for.
    """

    known: bool = True
    supplied: AdapterHashSource | None = None
    skip_kind: SkipKind = SkipKind.STRUCTURAL
    """The kind of skip a rule reading this layer reports where it cannot check.

    Read only by callers producing a :class:`Skip` because this layer answered
    ``known=False``; unused on a resolved layer, defaulted to
    :attr:`SkipKind.STRUCTURAL` to keep the constructor uniform.
    """

    def __post_init__(self) -> None:
        if not self.known and self.supplied is not None:
            raise ValueError(
                "an unresolvable hash-source layer carries facts: the rule that reads "
                f"them is skipped, so {self.supplied.where} / {self.supplied.state.value} "
                "would be resolved and then ignored. Report the facts with known=True, "
                "or report nothing"
            )

    @classmethod
    def unresolvable(cls, kind: SkipKind = SkipKind.ADAPTER_DECLARED) -> HashSourceLayer:
        """The layer of a caller that cannot say what the grading adapter supplies.

        *kind* tags whose silence this is: the default,
        :attr:`SkipKind.ADAPTER_DECLARED`, is for an adapter whose hook returned
        this value; a caller reading an unresolvable layer *because* it holds no
        adapter passes :attr:`SkipKind.STRUCTURAL` instead.
        """
        return cls(known=False, skip_kind=kind)


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
    skip_kind: SkipKind = SkipKind.STRUCTURAL
    """The kind of skip a rule reading this world reports where it cannot check.

    Read only by callers producing a :class:`Skip` because this world answered
    ``known=False``; unused on a resolved world, defaulted to
    :attr:`SkipKind.STRUCTURAL` to keep the constructor uniform.
    """

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
    def unresolvable(cls, kind: SkipKind = SkipKind.ADAPTER_DECLARED) -> ReplayWorld:
        """The world of a caller that cannot say what a task gives a golden replay.

        *kind* tags whose silence this is: the default,
        :attr:`SkipKind.ADAPTER_DECLARED`, is for an adapter whose hook returned
        this value; a caller reading an unresolvable world *because* it holds no
        adapter passes :attr:`SkipKind.STRUCTURAL` instead.
        """
        return cls(
            initial_state=InitialStateSource.ABSENT,
            mcp_server=False,
            known=False,
            skip_kind=kind,
        )


@dataclass(frozen=True)
class SeededTablesLayer:
    """The tables a task seeds, which its ``state_checks.id_fields`` declaration keys.

    A caller that cannot read what a task seeds reports :meth:`unresolvable`, which
    leaves the declaration unchecked — distinct from a caller reporting a task that
    seeds nothing, which is ``tables={}`` and a real answer with real consequences: a
    declared table is then the unknown-table finding, exactly as on the run path.
    """

    tables: Mapping[str, list[dict[str, Any]]] | None
    known: bool = True
    skip_kind: SkipKind = SkipKind.STRUCTURAL
    """The kind of skip a rule reading this layer reports where it cannot check.

    Read only by callers producing a :class:`Skip` because this layer answered
    ``known=False``; unused on a resolved layer, defaulted to
    :attr:`SkipKind.STRUCTURAL` to keep the constructor uniform.
    """

    def __post_init__(self) -> None:
        if self.known and self.tables is None:
            raise ValueError(
                "a resolved seeded-tables layer carries no view, so every declaration "
                "would be held against nothing. Report the tables the task seeds — {} "
                "where it seeds none — or report unresolvable()"
            )
        if not self.known and self.tables is not None:
            raise ValueError(
                "an unresolvable seeded-tables layer carries facts: the rule that reads "
                f"them is skipped, so tables {sorted(self.tables)} would be resolved and "
                "then ignored. Report the tables with known=True, or report nothing"
            )

    @classmethod
    def unresolvable(cls, kind: SkipKind = SkipKind.ADAPTER_DECLARED) -> SeededTablesLayer:
        """The layer of a caller that cannot read what a task seeds.

        *kind* tags whose silence this is: the default,
        :attr:`SkipKind.ADAPTER_DECLARED`, is for an adapter whose hook returned
        this value; a caller reading an unresolvable layer *because* it holds no
        adapter passes :attr:`SkipKind.STRUCTURAL` instead.
        """
        return cls(tables=None, known=False, skip_kind=kind)


@dataclass(frozen=True)
class Finding:
    """One authoring defect, addressed by where in the block it was written."""

    where: str
    message: str


@dataclass(frozen=True)
class Skip:
    """One thing the gate could not check, and why it could not.

    *kind* tags whose silence the skip is: :attr:`SkipKind.STRUCTURAL` for the
    environment that could not inspect (an uninstalled adapter, a resolved
    inventory whose schema does not type an argument), :attr:`SkipKind.ADAPTER_DECLARED`
    for an adapter that is loaded and answered :meth:`unresolvable`. The default
    matches every producer today, which reports on the environment's own account;
    a rule reading a layer that carries a kind propagates ``layer.skip_kind``
    verbatim.
    """

    where: str
    reason: str
    kind: SkipKind = SkipKind.STRUCTURAL


@dataclass(frozen=True)
class AuthoringReport:
    """What checking one grading block against one tool inventory found.

    ``unchecked`` is a channel, not a third severity — the gate itself never
    raises on a ``Skip``. Each ``Skip`` carries a :class:`SkipKind`
    (``STRUCTURAL`` when the environment couldn't inspect the pack, e.g. the
    declared adapter is not installed; ``ADAPTER_DECLARED`` when the adapter's
    hook returned ``unresolvable()``). A caller may read ``Skip.kind`` after
    :meth:`fatal` returns to promote ``ADAPTER_DECLARED`` rows to fatal —
    ``tolokaforge validate --strict-authoring`` does exactly that. ``STRUCTURAL``
    rows stay never-fatal so an environment missing an adapter cannot fail a
    pack the environment cannot judge. See ADR-0042.
    """

    errors: tuple[Finding, ...] = ()
    """Fatal wherever the report is enforced."""

    advisories: tuple[Finding, ...] = ()
    """Fatal only where the caller enforces them."""

    unchecked: tuple[Skip, ...] = ()
    """Never fatal in the gate; promotable to fatal by a caller reading
    :attr:`Skip.kind` (``ADAPTER_DECLARED`` promotes under
    ``--strict-authoring``; ``STRUCTURAL`` stays never-fatal). See ADR-0042."""

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

    matcher: TraceMatcher
    """The matcher this predicate was read from, whose ``tool`` names the schema
    that types the value it reads."""

    argument_path: str | None
    """The key this predicate was filed under in an ``args`` mapping, ``None`` for
    a predicate on a field of the event. Carried rather than recovered from
    :attr:`where`, which cannot be split back: ``args: {"a.args.b": …}`` addresses
    one path whose last ``.args.`` separator is inside it."""


class _BoundTypeSource(str, Enum):
    """What settles the type a binding holds, which decides how it is repaired."""

    SCHEMA = "schema"
    """A tool's schema types the argument the extraction addresses."""

    EVENT = "event"
    """``TraceEvent`` types the field, including ``args`` as the argument mapping."""

    CAPTURE = "capture"
    """A ``pattern`` narrows the extraction, so what binds is the capture."""


@dataclass(frozen=True)
class _BoundValueType:
    """The JSON type a binding holds, and what settles it."""

    declared: str | None
    """``None`` where nothing types it, which is no evidence rather than a mismatch."""

    source: _BoundTypeSource
    """Which of the three settles it, which the finding must not collapse: the repair
    an author is owed differs, and telling one who already wrote a capture to write a
    capture names no repair at all."""

    tool: str | None
    """The tool whose schema types it, for :attr:`_BoundTypeSource.SCHEMA` alone."""

    strictness: ArgumentSchema | None
    """The schema claim the type rests on, or ``None`` where no schema was read."""


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

# The JSON types whose values are never strings, read off the shared table: a type
# no binding operator can ever satisfy against a held string. A schema declaring one
# of them for an argument settles that a reference comparing the bound value against
# text cannot hold; ``string``, and a property writing no type at all, settle nothing.
_UNCORRELATABLE_JSON_TYPES: frozenset[str] = frozenset(
    declared
    for declared in JSON_TYPES
    if not ever_satisfiable("equals_binding", "string", declared)
    and not ever_satisfiable("contains_binding", "string", declared)
)

# Both spellings of a union: ``X | None`` resolves to one and ``Optional[X]`` to the
# other, and only these two make an annotation's arguments its alternatives.
_UNION_ORIGINS: frozenset[Any] = frozenset({Union, types.UnionType})

# Which ``TraceEvent`` attribute each matchable field reads. A matcher names the
# field; the type of the value a predicate on it is handed is the attribute's.
_MATCHER_FIELD_ATTRIBUTES: Mapping[str, str] = {
    "tool": "tool_name",
    "text": "text",
    "result": "result",
    "executor": "executor",
    "status": "status",
    "args": "arguments",
}


def _is_a_string_at_runtime(annotation: Any) -> bool:
    """Whether a ``TraceEvent`` annotation types its value as text.

    The declared type is the annotation's single non-``None`` member, and the value
    is text when that member is a ``str`` subclass — which is what makes the closed
    vocabularies behind ``status`` and ``executor`` text and ``args``' mapping not.

    Only a union is split. ``get_args`` would otherwise read a parameterised generic
    as its own union of members, so ``list[str]`` would answer for ``str``.
    """
    members = get_args(annotation) if get_origin(annotation) in _UNION_ORIGINS else (annotation,)
    declared = [member for member in members if member is not type(None)]
    if len(declared) != 1:
        return False
    return isinstance(declared[0], type) and issubclass(declared[0], str)


def _matcher_fields_whose_value_is_a_string() -> frozenset[str]:
    """The matchable fields a predicate reads text off, whatever it is handed."""
    annotations = get_type_hints(TraceEvent)
    return frozenset(
        field
        for field, attribute in _MATCHER_FIELD_ATTRIBUTES.items()
        if _is_a_string_at_runtime(annotations[attribute])
    )


# Computed off the event rather than listed, so the set and the claim behind it
# cannot disagree.
_TEXTUAL_MATCHER_FIELDS: frozenset[str] = _matcher_fields_whose_value_is_a_string()

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

# What a required action no trial can satisfy costs, and the two shapes that reach it.
# The hazard is one sentence because both cost the same thing — the component is short
# an action however the agent behaves — while the fix differs, so each message names its
# own.
_A_REQUIRED_ACTION_NOTHING_SATISFIES = (
    "the transcript component is short a required action on every trial however the agent behaves"
)

_A_REQUIRED_ACTION_NO_ACTOR_CAN_MAKE = (
    "tool {name!r} is not declared by this task, which gives its actors {declared}: no actor "
    "can call it, so {hazard}"
)

_A_REQUIRED_ACTION_ITS_REQUESTOR_CANNOT_MAKE = (
    "required action {action_id!r} names tool {name!r} under requestor {requestor!r}, which is "
    "matched against the calls the {actor} executed alone, and tools.{actor}.enabled declares "
    "{here}: {declaring} declares the tool instead, so the executor filter never matches and "
    "{hazard}. Declare {name!r} under tools.{actor}.enabled, or write the requestor whose actor "
    "already has it"
)

_A_HASH_SOURCE_NOTHING_READS = (
    "{key} is declared while hash.enabled is {enabled!r}: both substrates read the flag "
    "before any source, so the comparison never runs and the state is graded without it. "
    "Write enabled: true, or drop the source"
)

_A_SUPPLIED_HASH_SOURCE_THAT_GRADES_NOTHING = (
    "hash grading is enabled and the adapter grading this task supplies its source from "
    "{where}, which is {state}: there is nothing to compare the trial against, so the hash "
    "verdict is lost at grade time, once the trial is already paid for. Restore or populate "
    "{where}, or drop the hash block"
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

_UNRESOLVED_HASH_SOURCE_REASON = (
    "no caller resolved what the adapter grading this task supplies beneath the authored "
    "hash block — an external adapter may compute the source it compares against from its "
    "own fixtures — so whether this block grades as written is not checkable here"
)

_UNRESOLVED_SEEDED_TABLES_REASON = (
    "no caller resolved the tables this task seeds — reading initial_state.json_db is the "
    "native reading, and a task an adapter maintained outside this repository owns may seed "
    "its state some other way — so whether the declared keys address the seeded records is "
    "not checkable here"
)

_ID_FIELDS_ADDRESS = "state_checks.id_fields"

_JSONPATHS_ADDRESS = "state_checks.jsonpaths"
_HASH_ENABLED_ADDRESS = "state_checks.hash.enabled"

_UNRESOLVED_SEEDED_TABLES_FOR_A_STATE_READ = (
    "no caller resolved the tables this task seeds — reading initial_state.json_db is the "
    "native reading, and a task an adapter maintained outside this repository owns may seed "
    "its state some other way — so whether this block reads a database the trial will have "
    "is not checkable here"
)

_READS_A_DATABASE_THE_TASK_SEEDS_NONE_OF = (
    "{declares}, which reads the trial's database, but this task's initial_state seeds no "
    "tables — so no DB service is registered for the trial and GradeTrial refuses it rather "
    "than scoring it. Seed the state the block reads under initial_state.json_db, or drop "
    "{where} from the pack."
)

_A_PATH_BEYOND_THE_RUNNERS_STATE = (
    "path {path!r} addresses state the runner's JSONPath grading does not carry: it "
    "composes db and tables from the trial's database and nothing else, while the core "
    "engine also composes agent, user and filesystem — so this assertion scores on one "
    "substrate and can never match on the other. {remedy}"
)

_ADDRESS_A_FILE_BY_GLOB = (
    "Address a file with path_glob: and contains_ci:, the pairing both substrates read."
)

_ADDRESS_THE_DATABASE = "Address the trial's database, which is rooted at db or tables."

_A_PATH_GLOB_OPERATOR_THE_RUNNER_CANNOT_READ = (
    "path_glob {glob!r} is compared with {operator}, which the runner's file-content "
    "evaluator does not read — it reads contains_ci alone, and an absent contains_ci is the "
    "empty string, which every file contains. The assertion therefore passes on the runner "
    "whatever the file says, while core scores it for real. Compare with contains_ci."
)

_NO_OPERATOR_AT_ALL = "no comparison operator"

# The comparison vocabulary a ``jsonpaths`` assertion may write. Only ``contains_ci``
# survives the runner's file-content evaluator, which is what the ``path_glob`` rule
# below is about; the other three are legitimate beneath a ``path:``.
_JSONPATH_COMPARISONS: tuple[str, ...] = ("equals", "equals_ci", "contains", "contains_ci")

# Bound once so the signature's default is the value, not a call in the annotation.
_UNRESOLVED_REPLAY_WORLD = ReplayWorld.unresolvable()
_UNRESOLVED_HASH_SOURCE_LAYER = HashSourceLayer.unresolvable()
_UNRESOLVED_SEEDED_TABLES = SeededTablesLayer.unresolvable()


def inspect_grading_authoring(
    grading: Mapping[str, Any],
    inventory: ToolInventory,
    *,
    effective_combine: GradingCombineConfig | None = None,
    replay_world: ReplayWorld = _UNRESOLVED_REPLAY_WORLD,
    hash_sources: HashSourceLayer = _UNRESOLVED_HASH_SOURCE_LAYER,
    seeded_tables: SeededTablesLayer = _UNRESOLVED_SEEDED_TABLES,
) -> AuthoringReport:
    """Report what a task's tools, its replay world and its fold say about its grading block.

    The block is expected to have passed its own shape validation; the typed
    sub-blocks read here are constructed, so a malformed one raises its own load
    error rather than being reported as an authoring finding.

    Every rule that needs the task's tools is skipped into ``unchecked`` when the
    inventory is unresolvable. The rules outside that set still run: regex compilation,
    the golden source's shape and the state-source exclusivity, which read nothing but
    the block, the replay-world rule, which reads the world and skips on its own
    account, and the hash-source declaration, which skips on the hash layer the same
    way.

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
        hash_sources: What supplies a hash source beneath the authored block. The
            default, :meth:`HashSourceLayer.unresolvable`, is the answer for a caller
            that cannot say which adapter grades the task — it moves the hash-source
            rule's finding to ``unchecked`` where that rule would have refused, and
            fails nothing.
        seeded_tables: The tables the task seeds, which its ``state_checks.id_fields``
            declaration keys. The default, :meth:`SeededTablesLayer.unresolvable`, is
            the answer for a caller holding no ``task.yaml`` — it skips the rule reading
            them wherever a declaration would have been checked, and fails nothing.
    """
    constraints = tuple(_trace_constraints(grading))
    sites = tuple(_trace_matcher_sites(constraints))
    binders = tuple(_trace_binding_sites(constraints))
    rules = _transcript_rules(grading)
    reports = [
        _check_sections_declare_something(grading),
        _check_regex_compiles(sites, binders, rules.disallow_regex if rules else ()),
        _check_hash_source_declared(grading, hash_sources),
        _check_golden_actions_are_a_list(grading),
        _check_probes_are_the_only_state_source(grading),
        _check_golden_replay_world(grading, replay_world),
        _check_id_fields_against_seeded_tables(grading, seeded_tables),
        _check_state_reads_a_database_the_task_seeds(grading, seeded_tables),
        _check_jsonpaths_address_a_reachable_state(grading),
        _check_path_glob_is_compared_the_way_the_runner_reads_it(grading),
    ]
    if inventory.known:
        reports += [
            _check_tool_names(sites, inventory),
            _check_tool_expectation_names(rules.tool_expectations if rules else None, inventory),
            _check_required_action_names(rules.required_actions if rules else (), inventory),
            _check_golden_action_names(grading, inventory),
            _check_argument_paths(sites, inventory),
            _check_bound_extractions(binders, inventory),
            _check_bound_comparisons(binders, inventory),
        ]
    else:
        reports.append(
            AuthoringReport(
                unchecked=(Skip("grading", _UNRESOLVABLE_REASON, kind=inventory.skip_kind),)
            )
        )
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
        or _hash_is_enabled(hash_block)
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
    """``tool_expectations`` may only name a tool the agent of the task can call.

    ``transcript.py`` filters ``call.executor is ToolExecutorIdentity.AGENT`` in
    the evaluator (renamed ``_executed`` -> ``_executed_by_agent``), so a name
    in the user's declared set is invisible to the check at runtime: a
    ``required_tools`` name reaching only ``user_declared`` passes the gate and
    fails on every trial, indistinguishable from the misspelling this gate
    exists to catch; a ``disallowed_tools`` name reaching only ``user_declared``
    passes on every trial even when the simulator calls it. Narrow the
    membership check to the agent's set.
    """
    if expectations is None:
        return AuthoringReport()
    # ``declared_by`` refuses to answer on a recorded-trial inventory whose
    # actor split is unknown; fall back to the union set there.
    if inventory.actor_split_known:
        declared_set = inventory.declared_by(ToolExecutorIdentity.AGENT)
        actor_label = "for the agent, which the transcript-rules evaluator reads"
    else:
        declared_set = inventory.declared
        actor_label = "for any actor of this task"
    errors = tuple(
        Finding(
            f"transcript_rules.tool_expectations.{expectation}",
            f"tool {name!r} is not declared by this task {actor_label}. The declared "
            f"set is {sorted(declared_set)}: {hazard}",
        )
        for expectation, hazard in _TOOL_EXPECTATION_HAZARDS.items()
        for name in getattr(expectations, expectation)
        if name not in declared_set
    )
    return AuthoringReport(errors=errors)


def _check_required_action_names(
    actions: Sequence[RequiredAction], inventory: ToolInventory
) -> AuthoringReport:
    """A required action may only name a tool the actor its ``requestor`` names can call.

    The rule its two siblings :func:`_check_tool_names` and
    :func:`_check_tool_expectation_names` already carry, plus the half only this key
    has: ``requestor`` is matched against the recorded executor, so an action naming
    the other actor's tool selects nothing exactly as a misspelling does, and costs
    the same.

    It is written here and not over ``trace_checks`` because a required action is a
    *positive* existence claim. A matcher may carry ``executor: user`` inside an
    ``absent`` constraint on a pack declaring no user tools — an assertion that no
    user-side call happened, which such a pack satisfies — so refusing that shape
    would reject packs that grade correctly.
    """
    return _merged(
        _one_required_action(index, action, inventory) for index, action in enumerate(actions)
    )


_A_REQUESTOR_AN_ACTOR_BLIND_INVENTORY_CANNOT_JUDGE = (
    "the tool set was read off a recorded trial, whose wire tool list says which tools "
    "an actor was offered and not which actor. Whether {requestor!r} is the actor that "
    "declared {name!r} is not a fact this inventory holds, and refusing the action on it "
    "would fail an authoring that may well be right. The name itself is still checked."
)


def _one_required_action(
    index: int, action: RequiredAction, inventory: ToolInventory
) -> AuthoringReport:
    """The one finding *action* draws, or none: a name is wrong once, not twice."""
    where = f"transcript_rules.required_actions[{index}]"
    if action.name not in inventory.declared:
        return AuthoringReport(
            errors=(
                Finding(
                    f"{where}.name",
                    _A_REQUIRED_ACTION_NO_ACTOR_CAN_MAKE.format(
                        name=action.name,
                        declared=sorted(inventory.declared),
                        hazard=_A_REQUIRED_ACTION_NOTHING_SATISFIES,
                    ),
                ),
            )
        )
    if not inventory.actor_split_known:
        return AuthoringReport(
            unchecked=(
                Skip(
                    f"{where}.requestor",
                    _A_REQUESTOR_AN_ACTOR_BLIND_INVENTORY_CANNOT_JUDGE.format(
                        requestor=action.requestor, name=action.name
                    ),
                ),
            )
        )
    executor = REQUESTOR_TO_EXECUTOR[action.requestor]
    if action.name in inventory.declared_by(executor):
        return AuthoringReport()
    return AuthoringReport(
        errors=(
            Finding(
                f"{where}.requestor",
                _A_REQUIRED_ACTION_ITS_REQUESTOR_CANNOT_MAKE.format(
                    action_id=action.action_id,
                    name=action.name,
                    requestor=action.requestor,
                    actor=executor.value,
                    here=sorted(inventory.declared_by(executor)),
                    declaring=sorted(_blocks_declaring(action.name, inventory)),
                    hazard=_A_REQUIRED_ACTION_NOTHING_SATISFIES,
                ),
            ),
        )
    )


def _blocks_declaring(name: str, inventory: ToolInventory) -> Iterator[str]:
    """The ``tools.<actor>.enabled`` keys that give some actor *name*."""
    return (
        f"tools.{executor.value}.enabled"
        for executor in ToolExecutorIdentity
        if name in inventory.declared_by(executor)
    )


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
        (f"{predicate_site.where}.not_regex", predicate_site.predicate.not_regex)
        for site in sites
        for predicate_site in _predicate_sites(site)
        if predicate_site.predicate.not_regex is not None
    ]
    authored += [
        (f"{site.where}.values.{name}.pattern", value.pattern)
        for site in binders
        for name, value in site.binding.values.items()
        if value.pattern is not None
    ]
    findings = (_uncompilable(where, pattern) for where, pattern in authored)
    return AuthoringReport(errors=tuple(finding for finding in findings if finding is not None))


def _check_hash_source_declared(
    grading: Mapping[str, Any], hash_sources: HashSourceLayer
) -> AuthoringReport:
    """The hash check and something to compare against are declared together.

    Either half alone grades the state without the comparison the author wrote. A
    source under a disabled flag is never read, whichever of
    :data:`~tolokaforge.core.grading.state_composition.HASH_SOURCE_KEYS` carries it:
    both substrates test the flag first, so the pack grades in silence without it. An
    enabled flag with no source is the same defect from the other side, and it also
    splits the two substrates — core produces no hash verdict at all while the runner
    compares the trial against the initial state, so the same trial takes two different
    ``state_checks`` components.

    A block declaring an inert source draws one finding, addressed at the source the
    tuple names first. What the message claims of both substrates is only what holds of
    both: the flag is read before any source.

    Both halves read the flag through :func:`_hash_is_enabled`, which is the
    value that decides the grade: both substrates build the block into a
    ``StateHashConfig`` before either evaluator branches, so ``enabled: 1`` does read
    the hash and ``enabled: "false"`` does not, and reading either off the key would
    speak for a substrate neither of them is. A source is read for truth the same way —
    an empty ``golden_actions`` list replays nothing, which is why both substrates treat
    it as no source at all and why such a block is
    :func:`_check_sections_declare_something`'s rather than this rule's.

    Both halves also hold only where the authored block is the whole layer, which is
    *hash_sources*'s to say: an external adapter may compute the source it compares
    against from its own fixtures, so under :meth:`HashSourceLayer.unresolvable` the
    finding either half would have drawn moves to ``unchecked``, addressed where the
    finding would have been — for the reason the tool rules skip an unresolvable
    inventory, that refusing a reading the adapter does not use rejects packs that
    grade fine. Where the layer names the source an adapter supplies, the enabled half
    is decided against that source instead, by
    :func:`_an_enabled_block_declaring_no_source`. The disabled half ignores it: a source
    the block declares and nothing reads is the author's defect whatever an adapter
    supplies beside it. A block this rule finds
    nothing wrong with reports nothing on any layer, so the skip names only the shape a
    native pack would have been refused for.
    """
    hash_block = authored_hash_block(grading)
    if hash_block is None:
        return AuthoringReport()
    enabled = _hash_is_enabled(hash_block)
    declared = next((key for key in HASH_SOURCE_KEYS if hash_block.get(key)), None)
    if enabled and declared is None:
        return _an_enabled_block_declaring_no_source(hash_sources)
    if enabled or declared is None:
        return AuthoringReport()
    if not hash_sources.known:
        return AuthoringReport(
            unchecked=(
                Skip(
                    f"state_checks.hash.{declared}",
                    _UNRESOLVED_HASH_SOURCE_REASON,
                    kind=hash_sources.skip_kind,
                ),
            )
        )
    return AuthoringReport(
        errors=(
            Finding(
                f"state_checks.hash.{declared}",
                _A_HASH_SOURCE_NOTHING_READS.format(
                    key=declared, enabled=hash_block.get("enabled")
                ),
            ),
        )
    )


def _an_enabled_block_declaring_no_source(hash_sources: HashSourceLayer) -> AuthoringReport:
    """What an enabled hash block naming no source is, under each answer about the layer.

    The shape means something different beneath each answer, and only the layer knows
    which: a native pack has written a check that compares against nothing, an adapter
    supplying a usable source has written that family's own convention, and an adapter
    whose source has gone missing or empty has written a pack that costs a trial and
    grades nothing — the one reading that is worth refusing here rather than at grade
    time. A caller that cannot say leaves all three unchecked.
    """
    if not hash_sources.known:
        return AuthoringReport(
            unchecked=(
                Skip(
                    "state_checks.hash.enabled",
                    _UNRESOLVED_HASH_SOURCE_REASON,
                    kind=hash_sources.skip_kind,
                ),
            )
        )
    supplied = hash_sources.supplied
    if supplied is None:
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
    if supplied.state is SuppliedSourceState.USABLE:
        return AuthoringReport()
    return AuthoringReport(
        errors=(
            Finding(
                "state_checks.hash.enabled",
                _A_SUPPLIED_HASH_SOURCE_THAT_GRADES_NOTHING.format(
                    where=supplied.where, state=supplied.state.value
                ),
            ),
        )
    )


def authored_hash_block(grading: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The authored ``state_checks.hash`` block, or ``None`` where the pack wrote none.

    Every hash rule reads it through here, as does every pack read reaching for
    :func:`~tolokaforge.core.grading.state_composition.refuse_retired_hash_keys`, so a
    scalar written at either level is left to the block's own shape validation rather
    than read as an authoring defect.
    """
    state_checks = grading.get("state_checks")
    if not isinstance(state_checks, Mapping):
        return None
    hash_block = state_checks.get("hash")
    return hash_block if isinstance(hash_block, Mapping) else None


def _hash_is_enabled(hash_block: object) -> bool:
    """Whether an authored ``state_checks.hash`` block switches hash grading on.

    Every gate rule reading the flag reads it here, and the answer is
    :class:`StateHashConfig`'s rather than the key's, because the coercion is part of
    what the flag means: ``enabled: "false"`` is the ``False`` both substrates grade on,
    and a rule reading the truthy string speaks for neither. Anything that is no mapping
    declares no flag — the block's own shape validation owns that.

    A block the model refuses is read off the key instead. That pack does not load, and
    the one answer these rules may not give is the lenient one — a rule that blessed it
    would pass an authoring defect through on the strength of a second one.
    """
    if not isinstance(hash_block, Mapping):
        return False
    try:
        return StateHashConfig.model_validate(hash_block).enabled
    except ValidationError:
        return bool(hash_block.get("enabled"))


def state_sources_as_a_run_reads_them(state_checks: Mapping[str, Any]) -> dict[str, Any]:
    """This block's state sources, with ``hash.enabled`` read the way a run reads it.

    The authored counterpart of
    :meth:`~tolokaforge.runner.models.RunnerStateChecksConfig.authored_state_sources`,
    for a caller holding YAML rather than a config it has already built. Both hand
    :func:`~tolokaforge.core.grading.jsonpath_addressing.block_addresses_the_database`
    the same flag, so the gate cannot refuse a block the runtime grades cleanly.

    Only the flag is rewritten: every other key is the author's, which is the vocabulary
    that predicate reads.
    """
    return {**state_checks, "hash": {"enabled": _hash_is_enabled(state_checks.get("hash"))}}


def _authored_hash_is_a_state_source(grading: Mapping[str, Any]) -> bool:
    """Whether the authored block is enabled with something to compare against.

    Answered by building the block here and asking
    :func:`~tolokaforge.core.grading.state_composition.hash_block_is_a_state_source`,
    rather than by reading the raw keys a second time: the rule below reaches only a
    caller holding a fragment it never built a ``StateHashConfig`` from, and a second
    reading is a second rule. Pydantic's coercion is part of what the question means —
    ``enabled: "false"`` is the ``False`` a run grades on, not the truthy string — so
    re-reading the key would refuse a pack that loads and grades cleanly.

    A block the model refuses is a load error every surface constructing it already
    reports, so it declares no source here rather than drawing a second finding over a
    pack that cannot load at all.
    """
    hash_block = authored_hash_block(grading)
    if hash_block is None:
        return False
    try:
        hash_config = StateHashConfig.model_validate(hash_block)
    except ValidationError:
        return False
    return hash_block_is_a_state_source(hash_config)


def _check_golden_actions_are_a_list(grading: Mapping[str, Any]) -> AuthoringReport:
    """A declared golden replay is the list of actions to replay.

    The one hash rule that reads a source for its *shape* rather than for truth, and it
    reads only the block: whether a value is a list needs no tool set and no replay world,
    so a shape defect is refused for a pack whose adapter can report neither rather than
    skipped with the rules that do need them.

    Read under a ``hash.enabled`` a run switches on and then **whatever else the block
    declares**,
    unlike :func:`_check_golden_replay_world` beside it, which reads the source the
    replay needs a world for: ``NativeAdapter.to_task_description`` iterates the authored
    value whatever else the block says, so a shape no replay can iterate leaves the pack
    unregisterable however it would have graded.

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
    hash_block = authored_hash_block(grading)
    if hash_block is None or not _hash_is_enabled(hash_block):
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
        hash_is_a_state_source=_authored_hash_is_a_state_source(grading),
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


def _check_id_fields_against_seeded_tables(
    grading: Mapping[str, Any], seeded_tables: SeededTablesLayer
) -> AuthoringReport:
    """A declared primary key addresses the records its table is seeded with.

    The same three findings the run path's gates report, from the same computation
    (:func:`~tolokaforge.runner.id_resolution.id_fields_findings`) so a pack cannot be
    refused at one gate and passed at another: a declared table absent from the seeded
    state, a declared key component absent from every seeded record of its table, and a
    declared key that does not uniquely identify them. Each finding is addressed at
    ``state_checks.id_fields`` and carries its own remediation; the caller's raiser
    already names the grading file, so no context prefix is re-added.

    ``relaxed_validation`` downgrades exactly as it does on the run path: a logged
    warning is the whole observable, on no report channel at all. An advisory would
    read as the gentler answer and be the harsher one — the default ``fail_on`` is
    :attr:`~tolokaforge.core.models.GradingFindingSeverity.ADVISORY`, so it would fail
    precisely the packs the escape hatch exists to pass.
    """
    state_checks = grading.get("state_checks")
    if not isinstance(state_checks, Mapping):
        return AuthoringReport()
    id_fields = state_checks.get("id_fields")
    if not isinstance(id_fields, Mapping) or not id_fields:
        return AuthoringReport()
    if not seeded_tables.known:
        return AuthoringReport(
            unchecked=(
                Skip(
                    _ID_FIELDS_ADDRESS,
                    _UNRESOLVED_SEEDED_TABLES_REASON,
                    kind=seeded_tables.skip_kind,
                ),
            )
        )
    # __post_init__ makes a known layer with no view unconstructable; the assert
    # narrows ``tables`` for static analysis.
    assert seeded_tables.tables is not None
    # ``id_fields_findings`` calls ``table_key`` which raises
    # ``IdFieldResolutionError`` for an invalid key component. That exception
    # would propagate past ``report.fatal(fail_on)`` and become an
    # unconditional per-task refusal at ``orchestrator.py`` — bypassing the
    # ``fail_on`` severity, bypassing the ``relaxed_validation`` branch below,
    # and turning the false-reject mode this gate's docstring says it does
    # not have into the default. Catch the resolution error and route it
    # through the same channels a regular finding takes.
    try:
        findings = id_fields_findings(id_fields, seeded_tables.tables)
    except IdFieldResolutionError as exc:
        findings = (str(exc),)
    if not findings:
        return AuthoringReport()
    if state_checks.get("relaxed_validation"):
        logger.warning(
            "state_checks.relaxed_validation downgrades this task's id_fields findings: %s",
            " ".join(findings),
        )
        return AuthoringReport()
    return AuthoringReport(errors=tuple(Finding(_ID_FIELDS_ADDRESS, f) for f in findings))


def _jsonpath_assertions(grading: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """The ``state_checks.jsonpaths`` entries written as mappings, in authored order."""
    state_checks = grading.get("state_checks")
    if not isinstance(state_checks, Mapping):
        return ()
    return tuple(
        entry for entry in state_checks.get("jsonpaths") or () if isinstance(entry, Mapping)
    )


def _check_state_reads_a_database_the_task_seeds(
    grading: Mapping[str, Any], seeded_tables: SeededTablesLayer
) -> AuthoringReport:
    """A block reading the trial's database is authored on a task that provisions one.

    ``RegisterTrial`` provisions the DB service from ``initial_state``, so a block
    reading it on a task that seeds nothing reaches a service that was never started:
    ``GradeTrial`` refuses the trial, and the whole trial is paid for first. That
    refusal is the runtime half of this rule, and this is the half that costs nothing.

    An enabled ``hash`` block counts with or without a declared source, because the
    stable hash is fetched before any source is consulted — see
    :func:`~tolokaforge.core.grading.jsonpath_addressing.block_addresses_the_database`,
    the one predicate this rule and its runtime half both read. The flag reaching it is
    :func:`_hash_is_enabled`'s, not the authored key's, so that the two halves
    read one value as well as one predicate.

    ``relaxed_validation`` does not downgrade this, unlike
    :func:`_check_id_fields_against_seeded_tables`. That escape hatch exists for a
    declaration whose keys no longer resolve against seeded records, which still grades;
    a pack refused here does not grade at all on either substrate, so passing it would
    hand the author a green gate and a failed run.
    """
    state_checks = grading.get("state_checks")
    if not isinstance(state_checks, Mapping):
        return AuthoringReport()
    state_sources = state_sources_as_a_run_reads_them(state_checks)
    if not block_addresses_the_database(state_sources):
        return AuthoringReport()
    if not seeded_tables.known:
        return AuthoringReport(
            unchecked=(
                Skip(
                    "state_checks",
                    _UNRESOLVED_SEEDED_TABLES_FOR_A_STATE_READ,
                    kind=seeded_tables.skip_kind,
                ),
            )
        )
    if seeded_tables.tables:
        return AuthoringReport()

    if state_sources["hash"]["enabled"]:
        where = _HASH_ENABLED_ADDRESS
        declares = "state_checks.hash is enabled"
    else:
        assertion = next(a for a in _jsonpath_assertions(grading) if addresses_the_database(a))
        where = _JSONPATHS_ADDRESS
        described = assertion.get("description")
        declares = f"state_checks.jsonpaths declares path {assertion.get('path')!r}" + (
            f" ({described})" if described else ""
        )
    return AuthoringReport(
        errors=(
            Finding(
                where,
                _READS_A_DATABASE_THE_TASK_SEEDS_NONE_OF.format(declares=declares, where=where),
            ),
        )
    )


def _check_jsonpaths_address_a_reachable_state(grading: Mapping[str, Any]) -> AuthoringReport:
    """A ``path:`` addresses the trial's database, which is the state both substrates read.

    Reads nothing but the block, so it answers for every pack — including one whose
    seeded tables no caller could resolve, where the sibling rule above can only skip.

    A pack refused here is not merely graded differently on the two substrates: with a
    database provisioned the core engine resolves such a path against its own composed
    filesystem and the runner cannot, and without one ``GradeTrial`` refuses the trial
    outright. Neither outcome is a score its author would recognise.
    """
    findings: list[Finding] = []
    for assertion in _jsonpath_assertions(grading):
        target = unreachable_target(assertion)
        if target is None:
            continue
        # Only ``BEYOND_THE_RUNNERS_STATE`` reaches here now — ``FILESYSTEM``
        # grades on the runner via ``_read_agent_visible_filesystem``, so the
        # authoring gate no longer refuses ``$.filesystem[…]``-rooted paths.
        findings.append(
            Finding(
                _JSONPATHS_ADDRESS,
                _A_PATH_BEYOND_THE_RUNNERS_STATE.format(
                    path=assertion.get("path"),
                    remedy=_ADDRESS_THE_DATABASE,
                ),
            )
        )
    return AuthoringReport(errors=tuple(findings))


def _check_path_glob_is_compared_the_way_the_runner_reads_it(
    grading: Mapping[str, Any],
) -> AuthoringReport:
    """A ``path_glob:`` assertion is compared with ``contains_ci``, the only one both read.

    The runner's file evaluator reads ``check.get("contains_ci", "")`` and tests
    membership in the file's text, so an assertion writing any other operator — or none
    — compares the empty string against every matched file and passes whatever they
    contain, while core applies the operator the author wrote. A vacuous pass and a
    substrate divergence in one assertion (#466).

    This rule guards the road the ``filesystem``-rooted refusal above sends authors
    down, which is why it lands with them rather than later.
    """
    findings: list[Finding] = []
    for assertion in _jsonpath_assertions(grading):
        glob = assertion.get("path_glob")
        if glob is None:
            continue
        written = [name for name in _JSONPATH_COMPARISONS if assertion.get(name) is not None]
        if written == ["contains_ci"]:
            continue
        findings.append(
            Finding(
                _JSONPATHS_ADDRESS,
                _A_PATH_GLOB_OPERATOR_THE_RUNNER_CANNOT_READ.format(
                    glob=glob,
                    operator=" and ".join(written) if written else _NO_OPERATOR_AT_ALL,
                ),
            )
        )
    return AuthoringReport(errors=tuple(findings))


def _check_golden_replay_world(grading: Mapping[str, Any], world: ReplayWorld) -> AuthoringReport:
    """A pack replaying golden actions is authored against a task that gives them a world.

    The block is read the way core reads it — the flag, then ``golden_actions``, the one
    source needing a world at all. A ``hash.enabled`` a run reads as off is a source
    nobody resolves,
    for the reason :func:`_check_hash_source_declared` gives at length. The actions are
    then read for truthiness and never for shape: a truthy non-list value is refused
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
    hash_block = authored_hash_block(grading)
    if hash_block is None or not _hash_is_enabled(hash_block):
        return AuthoringReport()
    if not hash_block.get("golden_actions"):
        return AuthoringReport()
    if not world.known:
        return AuthoringReport(
            unchecked=(
                Skip(
                    _GOLDEN_ACTIONS_ADDRESS,
                    _UNRESOLVED_REPLAY_WORLD_REASON,
                    kind=world.skip_kind,
                ),
            )
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

    Read only under a ``hash.enabled`` a run switches on, the flag both substrates test
    before
    they read any source, for the reason :func:`_check_hash_source_declared` gives at
    length: a name under a disabled flag is never resolved, so refusing it would be
    stricter than the grade. That block is refused there instead, at the source key the
    flag stops anything from reading rather than at any name it carries.

    Names resolve against the tools the task *declares*, which is stricter than either
    replay substrate — core resolves against the pack's ``TOOLS`` map and the runner
    against the tools it registered for the trial, and neither is readable here without
    importing the pack's server module. #815 owns unifying the three.

    A name that is not a string at all — ``golden_actions`` claims nothing about its
    elements — is refused as one resolving to nothing rather than tested for membership,
    which an unhashable value answers with a ``TypeError``.
    """
    # The runner resolves ``golden_actions`` against the *agent* registry alone
    # (``service.py`` ``_execute_hash_grading`` step 0 iterates
    # ``trial_context.agent_tools.keys()``); a name reaching only ``user_declared``
    # passes the gate here and blows up on the runner with
    # ``UnresolvableGoldenAction``. Narrow the check to the agent's set when
    # the inventory knows the split; a recorded-trial inventory that does not
    # answers only the union ``inventory.declared``, so keep the old behaviour
    # there (the report's ``unchecked`` channel handles the residual case).
    declared_set = (
        inventory.declared_by(ToolExecutorIdentity.AGENT)
        if inventory.actor_split_known
        else inventory.declared
    )
    errors = tuple(
        Finding(
            _GOLDEN_ACTION_NAME_ADDRESS.format(index=index),
            _unreplayable_golden_action_message(name, inventory),
        )
        for index, name in enumerate(_authored_golden_action_names(grading))
        if not name or not isinstance(name, str) or name not in declared_set
    )
    return AuthoringReport(errors=errors)


def _authored_golden_action_names(grading: Mapping[str, Any]) -> Iterator[Any]:
    """Each golden action's name as written, in the order the replay would run them.

    Nothing at all where a run reads the flag as off, so the caller reads only the source a
    substrate would read. An action that is not a mapping, and one carrying no ``name``,
    both yield ``None``: ``golden_actions`` leaves its elements unclaimed (#907), so there
    is no load error to defer to, and the index of the offending action is what an author
    acts on.
    """
    hash_block = authored_hash_block(grading)
    if hash_block is None or not _hash_is_enabled(hash_block):
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

    Three answers off the one resolved schema: the extraction addresses an argument
    the tool declares, a capture can be taken off the value declared there, and that
    value can be compared against the fields the constraint references the name from.
    """
    return _merged(
        _one_extraction(f"{site.where}.values.{name}", name, value, site, inventory)
        for site in binders
        for name, value in site.binding.values.items()
    )


def _one_extraction(
    where: str, name: str, value: BoundValue, site: _BindingSite, inventory: ToolInventory
) -> AuthoringReport:
    """One extraction against the schema of the tool its binder selects.

    *where* addresses the extraction, and each answer is reported at the key that
    carries it — the value's own type under ``.field``, a capture that cannot be
    taken off it under ``.pattern``, which is the key the author deletes.

    Only an ``args`` extraction has a declared type at all: the other extractable
    fields are the event's own, which no tool schema describes and which every
    reference compares correctly.
    """
    if value.head_segment() != "args":
        return AuthoringReport()
    field = f"{where}.field"
    resolved = _resolved_tool(site.binding.match, inventory, field)
    if isinstance(resolved, Skip):
        return AuthoringReport(unchecked=(resolved,))
    _, _, path = value.field.partition(".")
    if not path:
        return _by_declared_type(where, name, value, "object", resolved, site.references)
    addressed = _one_argument_path(field, path, resolved, _UNBINDABLE_EXTRACTION_HAZARD)
    if addressed != AuthoringReport():
        return addressed
    declared = inventory.declared_type(resolved.name, path)
    return _by_declared_type(where, name, value, declared, resolved, site.references)


def _by_declared_type(
    where: str,
    name: str,
    value: BoundValue,
    declared: str | None,
    resolved: _ResolvedTool,
    references: tuple[_PredicateSite, ...],
) -> AuthoringReport:
    """Both answers the declared type gives about one extraction, at their own keys."""
    return _merged(
        (
            _uncapturable_extraction(where, name, value, declared, resolved),
            _uncorrelatable_extraction(
                f"{where}.field", name, value, declared, resolved, references
            ),
        )
    )


def _uncapturable_extraction(
    where: str,
    name: str,
    value: BoundValue,
    declared: str | None,
    resolved: _ResolvedTool,
) -> AuthoringReport:
    """A capture pattern over a value the schema types as something other than text.

    A pattern narrows a value only where the value is a string and yields nothing
    otherwise, so the name binds on no event whatever the agent did and the default
    ``on_unbound`` reports that as the agent's failure. Which predicate reads the
    name — or whether any does — does not enter into it, and neither does whether
    the pattern compiles: fixing the pattern does not make an integer capturable.
    """
    if value.pattern is None or declared not in JSON_TYPES or declared == "string":
        return AuthoringReport()
    finding = Finding(
        f"{where}.pattern",
        f"binding {name!r} narrows {value.field!r} by a capture pattern, and {resolved.name!r} "
        f"declares that as type {declared!r}. A capture is taken off text alone, so the "
        "binding yields no assignment on any trajectory and the default on_unbound reports "
        "that as the agent's failure. Drop the pattern to bind the value as the tool typed "
        "it, or take the capture off a field that holds text",
    )
    if resolved.strictness is ArgumentSchema.CLOSED:
        return AuthoringReport(errors=(finding,))
    return AuthoringReport(advisories=(finding,))


# What settled the bound type, and the repair that leaves. Written per source
# because the repair an author already took is no repair at all: one who wrote a
# capture cannot be told to write a capture, and one whose binding is typed by the
# event has no schema to align.
_WHAT_TYPED_THE_BINDING: Mapping[_BoundTypeSource, str] = {
    _BoundTypeSource.SCHEMA: "{tool!r} declares it",
    _BoundTypeSource.EVENT: "the event types it",
    _BoundTypeSource.CAPTURE: "the capture pattern makes it text",
}

# Only the last clause of each is always an edit to this file: aligning two declared
# types means editing a tool's schema, which the author may not own.
_HOW_TO_CORRELATE: Mapping[_BoundTypeSource, str] = {
    _BoundTypeSource.SCHEMA: (
        "Correlate two arguments the tools type the same way, extract a regex capture off "
        "a field that holds text — tool, text or result — or assert the two calls separately "
        "instead of correlating them"
    ),
    _BoundTypeSource.EVENT: (
        "Correlate an argument the tool types the same way, extract a regex capture off a "
        "field that holds text, or assert the two calls separately instead of correlating "
        "them"
    ),
    _BoundTypeSource.CAPTURE: (
        "A capture is text, so compare it against a field holding text — drop the pattern "
        "and correlate the value as the tool typed it, or assert the two calls separately "
        "instead of correlating them"
    ),
}


def _check_bound_comparisons(
    binders: tuple[_BindingSite, ...], inventory: ToolInventory
) -> AuthoringReport:
    """Whether a reference on an ``args`` predicate can ever hold against what it reads.

    Two arguments correlate natively where the tools type them the same way, which
    is what the feature exists for — and are false on every trajectory where they
    do not. Both types come off schemas, so this is the one rule resting on two
    tools' claims, and the weaker of the two decides its severity.
    """
    return _merged(
        _one_bound_comparison(reference, operator, name, site, inventory)
        for site in binders
        for reference in _references_this_rule_answers_for(site)
        for operator, name in _binding_operands(reference.predicate)
        if name in site.binding.values
    )


def _references_this_rule_answers_for(site: _BindingSite) -> tuple[_PredicateSite, ...]:
    """The references read off an ``args`` mapping and reported by nothing else.

    A ``regex`` beside the reference is deferred by :func:`_one_bound_comparison`
    where the extraction rule can reach it, which is not everywhere.
    """
    return tuple(reference for reference in site.references if reference.argument_path is not None)


def _the_extraction_rule_answers_for(value: BoundValue) -> bool:
    """Whether :func:`_uncorrelatable_extraction` reports on this extraction at all.

    A ``regex`` beside a reference makes it textual to :func:`_textual_references`,
    which reports the mistake at the extraction's ``.field`` address — but only for
    the extractions :func:`_one_extraction` reaches, and it exits on a non-``args``
    field and on a ``pattern`` before it resolves anything. Deferring wherever a
    ``regex`` appears would silence those two shapes at both tiers instead.
    """
    return value.pattern is None and value.head_segment() == "args"


def _binding_operands(predicate: ValuePredicate) -> tuple[tuple[str, str], ...]:
    """Each binding operator this predicate declares, with the name it reads.

    A predicate is a conjunction, so two operators are two comparisons and an
    author who wrote two mistakes is owed two findings.
    """
    return tuple(
        (operator, getattr(predicate, operator))
        for operator in sorted(TRACE_PREDICATE_BINDING_OPERATORS)
        if getattr(predicate, operator) is not None
    )


def _one_bound_comparison(
    reference: _PredicateSite,
    operator: str,
    name: str,
    site: _BindingSite,
    inventory: ToolInventory,
) -> AuthoringReport:
    """One reference, against the declared types of the two values it compares.

    Every gap another rule already reports is left to it: a matcher naming no one
    tool and a path below its first segment are both answered by
    :func:`_one_matchers_argument_paths`, at the same addresses this would use.
    """
    resolved = _resolved_tool(reference.matcher, inventory, reference.where)
    if isinstance(resolved, Skip):
        return AuthoringReport()
    head, _, below = reference.argument_path.partition(".")
    if below or head not in resolved.properties:
        return AuthoringReport()
    held = inventory.declared_type(resolved.name, head)
    if held not in JSON_TYPES:
        return AuthoringReport(
            unchecked=(
                Skip(
                    reference.where,
                    f"{resolved.name!r} declares no single type for {head!r} — no type at "
                    f"all, or a union of several — so whether {operator} can ever hold "
                    f"against binding {name!r} is not checkable",
                ),
            )
        )
    value = site.binding.values[name]
    if reference.predicate.regex is not None and _the_extraction_rule_answers_for(value):
        return AuthoringReport()
    bound = _what_the_binding_holds(site, value, inventory)
    if bound.declared not in JSON_TYPES or ever_satisfiable(operator, held, bound.declared):
        return AuthoringReport()
    return _never_true_correlation(reference, operator, name, held, bound, resolved)


def _what_the_binding_holds(
    site: _BindingSite, value: BoundValue, inventory: ToolInventory
) -> _BoundValueType:
    """The JSON type a binding holds, and which of the three sources settles it.

    A capture that binds is a string, and so is a ``tool`` / ``text`` / ``result``
    extraction, which ``TraceEvent`` types as text and no tool schema describes; a
    bare ``field: args`` binds the argument mapping. Only an ``args`` path is
    answered by a schema, so only that reading carries a claim severity can rest on.

    The extraction is typed here and again in :func:`_one_extraction`, which reads
    the same schema to answer a different question about the same key. The two are
    deliberately separate: this one must answer for a capture and for a non-``args``
    field, which that one exits on before it resolves anything.
    """
    if value.pattern is not None:
        return _BoundValueType("string", _BoundTypeSource.CAPTURE, None, None)
    if value.head_segment() != "args":
        return _BoundValueType("string", _BoundTypeSource.EVENT, None, None)
    _, _, path = value.field.partition(".")
    if not path:
        return _BoundValueType("object", _BoundTypeSource.EVENT, None, None)
    resolved = _resolved_tool(site.binding.match, inventory, site.where)
    if isinstance(resolved, Skip):
        return _BoundValueType(None, _BoundTypeSource.SCHEMA, None, None)
    head, _, below = path.partition(".")
    if below or head not in resolved.properties:
        return _BoundValueType(None, _BoundTypeSource.SCHEMA, None, None)
    return _BoundValueType(
        inventory.declared_type(resolved.name, head),
        _BoundTypeSource.SCHEMA,
        resolved.name,
        resolved.strictness,
    )


def _never_true_correlation(
    reference: _PredicateSite,
    operator: str,
    name: str,
    held: str,
    bound: _BoundValueType,
    resolved: _ResolvedTool,
) -> AuthoringReport:
    """The finding, at the severity the weaker of the two schemas permits."""
    finding = Finding(
        reference.where,
        f"{operator} reads {reference.argument_path!r}, which {resolved.name!r} declares as "
        f"type {held!r}, against binding {name!r}, which holds type {bound.declared!r} — "
        f"{_WHAT_TYPED_THE_BINDING[bound.source].format(tool=bound.tool)}. No pair of values "
        f"of those two types satisfies {operator}, so the comparison is false on every "
        f"trajectory and reads as the agent's failure. {_HOW_TO_CORRELATE[bound.source]}",
    )
    claims = tuple(claim for claim in (resolved.strictness, bound.strictness) if claim is not None)
    if all(claim is ArgumentSchema.CLOSED for claim in claims):
        return AuthoringReport(errors=(finding,))
    return AuthoringReport(advisories=(finding,))


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
    failing. An extraction carrying a ``pattern`` is exempt because what the
    reference compares is the capture rather than the value the schema typed;
    whether that capture can be taken at all is answered at ``.pattern``.
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
        "the check is false on every trajectory and reads as the agent's failure. "
        + _HOW_TO_CORRELATE[_BoundTypeSource.SCHEMA],
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
            yield _PredicateSite(f"{site.where}.{name}", name, value, site.matcher, None)
        elif isinstance(value, Mapping):
            for path, predicate in value.items():
                yield _PredicateSite(
                    f"{site.where}.{name}.{path}", name, predicate, site.matcher, path
                )


def _merged(reports: Iterable[AuthoringReport]) -> AuthoringReport:
    collected = tuple(reports)
    return AuthoringReport(
        errors=tuple(finding for report in collected for finding in report.errors),
        advisories=tuple(finding for report in collected for finding in report.advisories),
        unchecked=tuple(skip for report in collected for skip in report.unchecked),
    )
