"""Runner-side wire schema — Pydantic models the runner service exchanges.

The types in this module fall into three tiers:

- **Shared wire types re-exported by :mod:`tolokaforge.core.models`** —
  ``Criterion`` / ``CriterionResult`` / ``Rubric`` / ``JudgeCustomization`` /
  ``LLMJudgeConfig`` / ``EnvironmentManifest`` (and its supporting
  ``EnvironmentPatch`` / ``StackPatch`` / ``ResetSpec`` / ``ServiceSpec`` /
  ``ServiceIsolation`` / ``ServiceNetworkAccess``) / ``ToolExpectations`` /
  ``RecordedToolCall`` / ``ToolCallRecorder`` / ``ToolExecutorIdentity``.
  Their canonical home stays here; the ``core.models`` shim re-exports them
  so callers reach one module for the whole recorded-tool-call + wire-schema
  vocabulary.
- **Runner-only wire types with a ``Runner`` prefix** —
  ``RunnerGradingConfig`` / ``RunnerStateChecksConfig`` /
  ``RunnerTranscriptRulesConfig`` / ``RunnerRequiredAction`` /
  ``RunnerInitialStateConfig`` / ``RunnerInitializationAction`` /
  ``RunnerUserSimulatorConfig`` / ``RunnerGradeComponents``. Each is the
  strict, wire-shaped Pydantic model the runner produces or consumes;
  ``tolokaforge.core.models`` carries the sibling YAML-authoring shape under
  the unprefixed name for the same concern. The two live side by side
  because the wire form (flat, ``extra="forbid"``) and the authoring form
  (nested, ``extra="ignore"``) validate different things.
- **Genuinely runner-only types (no core-side sibling)** — ``TaskDescription``
  and the tool-manifest / DB-client / grading-result nested models that
  travel only on the runner service surface.

All models use Pydantic v2 ``BaseModel`` for validation and serialization.
"""

from __future__ import annotations

import ipaddress
import math
import re
from collections import Counter
from datetime import datetime
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

import yaml
from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator

from tolokaforge.core.deprecations import (
    coerce_flat_stack_fields,
    coerce_network_policy_case,
    coerce_security_context_aliases,
)
from tolokaforge.core.grading.combine_method import CombineMethod, validate_combine_method
from tolokaforge.core.grading.state_composition import resolve_hash_weight, validate_hash_weight
from tolokaforge.core.grading.trace_event_kind import TraceEventKind
from tolokaforge.core.grading.turn_bounds import validate_turn_window
from tolokaforge.core.netpolicy_constants import HARNESS_RESERVED_NETWORKS

# ``ToolExecutionStatus`` is declared beside ``ToolResult`` in the true leaf
# ``tolokaforge.tools.registry`` (zero first-party imports), because a
# ``ToolResult`` is what produces a status. Importing it here is the only legal
# direction: declaring it in this module would make that leaf — which every
# layer imports — depend on the runner's models.
from tolokaforge.tools.registry import ToolExecutionStatus

# =============================================================================
# Enums (from TASK_DESCRIPTION_SCHEMA.md)
# =============================================================================


class AdapterType(str, Enum):
    """Well-known adapter names produced by built-in/first-party adapters.

    This enum is **not** an exhaustive, closed set: ``TaskDescription.adapter_type``
    is a free ``str`` sourced from the adapter registry, so entry-point / third-party
    adapters round-trip with their own names and need no engine edit. These members
    are the canonical constants for the built-in adapter names — first-party engine
    code should reference them (e.g. ``AdapterType.NATIVE``) instead of raw
    adapter-name string literals.
    """

    NATIVE = "native"
    TAU = "tau"
    TLK_MCP_CORE = "tlk_mcp_core"
    TERMINAL_BENCH = "terminal_bench"
    MIGRATION_BENCH = "migration_bench"


class InvocationStyle(str, Enum):
    """How the runtime invokes this tool."""

    TAU_SYNC = "tau_sync"  # Tau: Tool.invoke(data, **kwargs)
    MCP_ASYNC = "mcp_async"  # TlkMcpCore: asyncio.run(tool.run(db, kwargs))
    MCP_SERVER = "mcp_server"  # Native: MCP server subprocess
    DOCKER_COMPOSE_EXEC = "docker_compose_exec"  # Terminal-bench: docker compose exec


# =============================================================================
# Tool Definitions (from TASK_DESCRIPTION_SCHEMA.md)
# =============================================================================


class ToolSource(BaseModel):
    """
    Information needed to reconstruct tool execution at runtime.

    The runtime uses this to locate and instantiate the actual tool
    implementation in the container. Tool code must be pre-installed
    or mounted in the container.
    """

    # Import-rooted Python package name. The runner imports
    # ``{toolset}.{module_path}`` as-is — the adapter must supply the full
    # package path (e.g. "my_adapter_pkg.zendesk"); the runner adds no prefix.
    toolset: str
    module_path: str  # Module within toolset: "tools.create_item"
    class_name: str  # Class/function: "CreateItem", "BookReservation"
    invocation_style: InvocationStyle = InvocationStyle.TAU_SYNC

    # For MCP server tools only
    mcp_server_script: str | None = None  # Relative path: "mcp_server.py"

    # Arbitrary metadata for invocation-style-specific config (e.g. compose paths)
    extra: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class ToolSchema(BaseModel):
    """Complete tool definition with schema and source for reconstruction."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema format (OpenAI function calling)

    # Metadata
    category: Literal["read", "write", "compute"] = "compute"
    timeout_s: float = 30.0

    # How to reconstruct this tool at runtime
    source: ToolSource | None = None

    # Per-tool init kwargs lifted from ``task.yaml`` ``tools.agent.<name>: {...}``.
    # The runner splats these into the tool class constructor; unknown keys raise
    # ``ToolConfigurationError`` at trial registration. Used by builtin tools
    # whose construction needs task-side data (e.g. ``MobileTool.apps``); MCP
    # server tools and tau/MCP-async tools leave it empty.
    tool_config: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


# =============================================================================
# State and Data (from TASK_DESCRIPTION_SCHEMA.md)
# =============================================================================


class UnstableFieldSpec(BaseModel):
    """
    A field excluded from grading hash comparison.

    These are fields with non-deterministic values: auto-generated IDs,
    timestamps, or LLM-generated content. The DB service uses this to
    filter them out when computing stable state.
    """

    table_name: str  # "zendesk_tickets", "reservations"
    field_name: str  # "id", "created_at", "subject"
    reason: Literal["auto_id", "timestamp", "llm_generated", "random"] = "auto_id"

    model_config = {"extra": "forbid"}


class TableSchema(BaseModel):
    """Schema for a database table. Used by DB Service for validation."""

    table_name: str
    fields: dict[str, str]  # field_name → type ("string", "integer", "datetime")
    primary_key: str = "id"

    model_config = {"extra": "forbid"}


class RunnerInitialStateConfig(BaseModel):
    """
    Complete initial state specification.

    Contains all data and metadata needed to initialize the DB service
    and provision the agent's filesystem.
    """

    # Data: table_name → list of records
    tables: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)

    # Schema: table definitions for validation
    schemas: list[TableSchema] = Field(default_factory=list)

    # Unstable fields: single source of truth for hash exclusion
    unstable_fields: list[UnstableFieldSpec] = Field(default_factory=list)

    # Filesystem: dest_path → file content (text)
    # Files are written to the Runner's agent-visible directory during RegisterTrial.
    filesystem: dict[str, str] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


# =============================================================================
# Pre-Trial Actions (from TASK_DESCRIPTION_SCHEMA.md)
# =============================================================================


class RunnerInitializationAction(BaseModel):
    """
    Action to execute before trial starts.

    Used by Native adapter for user device setup (toggle_airplane_mode, etc.)
    """

    env_type: Literal["assistant", "user"]
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


# =============================================================================
# User Simulator (from TASK_DESCRIPTION_SCHEMA.md)
# =============================================================================


class RunnerUserSimulatorConfig(BaseModel):
    """Configuration for the user simulator."""

    mode: Literal["scripted", "llm"] = "llm"
    persona: str = "cooperative"
    backstory: str = ""  # User instruction/context

    # First message to start conversation (TlkMcpCore)
    first_message: str | None = None

    # User context data injected into conversation (TlkMcpCore)
    user_context: dict[str, Any] | None = None

    # For scripted mode
    scripted_flow: list[dict[str, str]] | None = None

    model_config = {"extra": "forbid"}


# =============================================================================
# Search / TypeSense (from TASK_DESCRIPTION_SCHEMA.md)
# =============================================================================


class SearchConfig(BaseModel):
    """Configuration for knowledge base search (TypeSense)."""

    enabled: bool = False
    domain_name: str | None = None  # "external_retail_v3"
    documents_path: str | None = None  # Path to docindex/ directory

    # TypeSense connection details for Docker execution.
    # When set, the Runner initialises mcp_core's global TypeSense registry
    # so that search_policy tools can call get_typesense_for_domain().
    host: str | None = None  # "typesense" (Docker DNS alias)
    port: int | None = None  # 8108 (container port)
    api_key: str | None = None  # TypeSense API key

    model_config = {"extra": "forbid"}


# =============================================================================
# Grading (from TASK_DESCRIPTION_SCHEMA.md)
# =============================================================================


class GoldenAction(BaseModel):
    """
    A tool call in the expected sequence.

    Execute these on fresh state to compute the expected final state hash.
    """

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class RunnerRequiredAction(BaseModel):
    """Tool call that must appear in the trajectory."""

    action_id: str
    requestor: Literal["assistant", "user"]
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    compare_args: list[str] | None = None  # Which args to compare, None = all

    model_config = {"extra": "forbid"}


class DbProbe(BaseModel):
    """A declarative read-only SQL assertion against a task-declared postgres.

    Grading connects to ``dsn``, runs ``query`` (a single read-only ``SELECT``),
    shapes the result into ``{"rows": [{col: val, ...}, ...], "row_count": N}``,
    and applies ``expect`` — JSONPath assertions in the same vocabulary as
    ``jsonpath_checks`` (``equals``/``equals_ci``/``contains``/``contains_ci``).
    """

    name: str
    dsn: str
    query: str
    expect: list[dict[str, Any]]
    description: str = ""

    model_config = {"extra": "forbid"}


_HASH_WEIGHT_CONTEXT = "task_description grading.state_checks.hash_weight"


class RunnerStateChecksConfig(BaseModel):
    """State-based grading configuration."""

    # Hash comparison
    hash_enabled: bool = False
    expected_hash: str | None = None  # Pre-computed (if available)
    golden_actions: list[GoldenAction] = Field(default_factory=list)
    # None means the author declared no weight — never "fall back to a default".
    hash_weight: float | None = None
    # Opt-in, PER-FIELD: record field names whose numeric-looking STRING values
    # fold ("130.00" == "130.0") when hashing state. Per-field (not a global
    # switch) because a numeric-looking string can carry meaning in its exact
    # representation (versions/codes) — see core/hash.py compute_stable_hash.
    numeric_string_fields: list[str] = Field(default_factory=list)
    # Opt-in, PER-TABLE: primary-key field for tables not keyed by the literal "id"
    # (e.g. {"widgets": "widget_id"}). Absent table => "id". Consumed by
    # the DB proxy (via ToolFactory) so key resolution is data-driven instead of
    # derived from model source. See runner/db_proxy.py _resolve_id_field.
    id_fields: dict[str, str] = Field(default_factory=dict)
    # Escape hatch for legacy tasks: downgrade the id_fields cross-check
    # (id_fields keys must appear in initial_state.tables) from a raise to a warning.
    # New tasks should fix typos or add the table, not enable this.
    relaxed_validation: bool = False

    # JSONPath assertions
    jsonpath_checks: list[dict[str, Any]] = Field(default_factory=list)

    # Substrate SQL assertions against a task-declared postgres DSN
    db_probes: list[DbProbe] = Field(default_factory=list)

    @field_validator("id_fields")
    @classmethod
    def _validate_id_fields(cls, value: dict[str, str]) -> dict[str, str]:
        for table, field in value.items():
            if not (isinstance(table, str) and table.strip()):
                raise ValueError(f"state_checks.id_fields has a blank table name: {table!r}")
            if not (isinstance(field, str) and field.strip()):
                raise ValueError(
                    f"state_checks.id_fields[{table!r}] must be a non-empty key field, "
                    f"got {field!r}"
                )
        return value

    @field_validator("hash_weight", mode="before")
    @classmethod
    def _validate_hash_weight_domain(cls, value: object) -> float | None:
        """Reject what core rejects, before Pydantic coerces it into range.

        ``mode="before"`` is load-bearing: Pydantic's lax coercion turns ``true``
        into ``1.0`` and ``"0.5"`` into ``0.5``, so any validator running after it —
        or a declarative ``ge``/``le`` constraint — sees a clean float and can no
        longer tell that the wire carried a bool or a string. ``hash_weight: true``
        would then silently mean "the hash decides outright".
        """
        if value is None:
            return None
        return validate_hash_weight(value, context=_HASH_WEIGHT_CONTEXT)

    @model_validator(mode="after")
    def _validate_hash_weight_declaration(self) -> RunnerStateChecksConfig:
        """Reject the one shape whose ``state_checks`` score is undecidable.

        Calls the same predicate the core config calls, over this model's flattened
        naming, so the two substrates cannot disagree about which configs are
        gradeable. An engine that dropped ``hash.weight`` on the way to the wire is
        therefore rejected at ``RegisterTrial`` rather than having its trial graded
        by a fold rule the author never chose.
        """
        resolve_hash_weight(
            {
                "enabled": self.hash_enabled,
                "expected_state_hash": self.expected_hash,
                "golden_actions": self.golden_actions,
                "weight": self.hash_weight,
            },
            jsonpaths=self.jsonpath_checks,
            context=_HASH_WEIGHT_CONTEXT,
        )
        return self

    model_config = {"extra": "forbid"}


class ToolExpectations(BaseModel):
    """Tools the agent must use and tools it must not touch.

    ``extra="forbid"`` inside the ``extra="ignore"`` core parent so a
    ``required_toolz`` typo fails at load instead of grading as an empty list.
    """

    required_tools: list[str] = Field(default_factory=list)
    disallowed_tools: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


class RunnerTranscriptRulesConfig(BaseModel):
    """Transcript-based grading configuration."""

    must_contain: list[str] = Field(default_factory=list)
    disallow_regex: list[str] = Field(default_factory=list)
    # Both bounds are declarable from 1 up. A ceiling below 1 admits no
    # assistant-turn count at all, and a floor of 0 asserts nothing — and the
    # runtime key ledger tests a declared key by truthiness, so a floor of 0 would
    # be an unpoliced declaration.
    max_turns: int | None = Field(default=None, ge=1)
    min_assistant_turns: int | None = Field(default=None, ge=1)
    tool_expectations: ToolExpectations | None = None
    required_actions: list[RunnerRequiredAction] = Field(default_factory=list)
    communicate_info: list[dict[str, Any]] = Field(default_factory=list)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _validate_turn_window(self) -> TranscriptRulesConfig:
        """Reject the window no assistant-turn count satisfies.

        Calls the same predicate the core config calls, so an engine and a runner
        built from different releases cannot disagree about which packs are
        gradeable: a window core rejected at ``tolokaforge validate`` is rejected at
        ``RegisterTrial`` too.
        """
        validate_turn_window(
            min_assistant_turns=self.min_assistant_turns,
            max_turns=self.max_turns,
            context="task_description grading.transcript_rules",
        )
        return self


TRACE_PREDICATE_OPERATORS: frozenset[str] = frozenset(
    {
        "equals",
        "equals_ci",
        "contains",
        "contains_ci",
        "not_equals",
        "regex",
        "gt",
        "gte",
        "lt",
        "lte",
        "in_",
        "not_in",
        "len_gt",
        "len_gte",
        "exists",
    }
)
"""Every operator a :class:`ValuePredicate` may carry, written out rather than
comprehended from the model, so the per-operator answer table has a second source
to be checked against."""


class ValuePredicate(BaseModel):
    """A conjunction of operators over one field of one event.

    Every declared operator must hold, so ``{gt: 0, lt: 100}`` is a range. This is
    deliberately unlike ``evaluate_jsonpath_state_checks``, which rejects a second
    operator: there two operators had no conjunctive reading, while here the range
    is the common case and a misspelled operator is already a load error.

    An operator counts as declared when its value is not ``None``, so a predicate
    means the same thing after the gRPC round trip that dumps every unset field as
    ``null``. The one thing that reading makes inexpressible is ``equals: null`` —
    and a ``None`` field is unmatched rather than vacuously true anyway, so that
    predicate never had a reading.
    """

    equals: Any = None
    equals_ci: str | None = None
    contains: Any = None
    contains_ci: str | None = None
    not_equals: Any = None
    regex: str | None = None
    gt: float | None = None
    gte: float | None = None
    lt: float | None = None
    lte: float | None = None
    in_: list[Any] | None = None
    not_in: list[Any] | None = None
    len_gt: int | None = Field(default=None, ge=0)
    len_gte: int | None = Field(default=None, ge=0)
    exists: bool | None = None

    model_config = {"extra": "forbid"}

    def declared_operators(self) -> frozenset[str]:
        """The operators this predicate asserts, which it is the conjunction of."""
        return frozenset(
            name for name in TRACE_PREDICATE_OPERATORS if getattr(self, name) is not None
        )

    @model_validator(mode="after")
    def _reject_a_predicate_asserting_nothing(self) -> ValuePredicate:
        if not self.declared_operators():
            raise ValueError(
                "a value predicate declares no operator, so it asserts nothing and "
                f"matches every value. Write one of {sorted(TRACE_PREDICATE_OPERATORS)}, "
                "or drop the field"
            )
        return self


# Which fields a matcher may read, per event kind. A ``tool_call`` matcher reads
# ``status`` and ``result`` from the result paired with the call, which is the only
# way to write "a failed call to X with argument Y" — ``args`` lives on the call
# event and ``status`` on its result. ``latency_seconds`` is on no row: wall time is
# not compared across substrates, so grading cannot depend on it.
TRACE_MATCHABLE_FIELDS_BY_KIND: dict[TraceEventKind, frozenset[str]] = {
    TraceEventKind.TOOL_CALL: frozenset({"tool", "executor", "args", "status", "result"}),
    TraceEventKind.TOOL_RESULT: frozenset({"tool", "executor", "status", "result"}),
    TraceEventKind.ASSISTANT_MESSAGE: frozenset({"text"}),
    TraceEventKind.USER_MESSAGE: frozenset({"text"}),
}

_MATCHER_PREDICATE_FIELDS: tuple[str, ...] = (
    "tool",
    "executor",
    "args",
    "status",
    "result",
    "text",
)


class TraceMatcher(BaseModel):
    """Which timeline events a constraint is about.

    ``kind`` is required and nothing is inferred from which predicates are present,
    so what a matcher selects is readable from the YAML. ``args`` addresses nested
    argument paths by dotted segments (``body.query``).

    A predicate on a field the kind never carries is rejected here rather than
    silently selecting nothing: an unmatchable matcher reads as an agent failure
    under the default ``on_missing``, so the author's typo would be reported as the
    agent's fault.
    """

    kind: TraceEventKind
    tool: ValuePredicate | None = None
    executor: ValuePredicate | None = None
    args: dict[str, ValuePredicate] | None = Field(default=None, min_length=1)
    status: ValuePredicate | None = None
    result: ValuePredicate | None = None
    text: ValuePredicate | None = None

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _reject_fields_the_kind_never_carries(self) -> TraceMatcher:
        declared = {name for name in _MATCHER_PREDICATE_FIELDS if getattr(self, name) is not None}
        matchable = TRACE_MATCHABLE_FIELDS_BY_KIND[self.kind]
        unmatchable = sorted(declared - matchable)
        if unmatchable:
            raise ValueError(
                f"kind {self.kind.value!r} carries no {unmatchable}, so a predicate there "
                f"would select nothing. A {self.kind.value} matcher reads "
                f"{sorted(matchable)}"
            )
        return self

    @model_validator(mode="after")
    def _require_a_success_status_beside_a_result_predicate(self) -> TraceMatcher:
        """#717: a failed call's result text differs between the two substrates.

        Successful result text is byte-identical across substrates and canonically
        locked; nothing proves that for a failure, so a ``result`` predicate is
        admitted only where portability holds. The rule is syntactic — one operator,
        ``equals``, valued ``success`` — rather than "does this status admit a
        failure", which would be a satisfiability question over every operator.
        """
        if self.result is None:
            return self
        success = ToolExecutionStatus.SUCCESS.value
        declared = self.status.declared_operators() if self.status else frozenset()
        if declared != {"equals"} or self.status.equals != success:
            raise ValueError(
                "a result predicate needs a status predicate reading exactly "
                f"{{equals: {success}}} beside it. A failed call's result text is not "
                "identical on the two substrates (#717), so only a successful call's "
                "result is matchable. To assert a call failed, match on status instead"
            )
        return self


class Quantifier(str, Enum):
    """How a constraint reads a side that matched more than one event."""

    ANY = "any"
    ALL = "all"
    FIRST = "first"
    LAST = "last"


class AnchorQuantifier(str, Enum):
    """The restricted domain a window anchor is selected by."""

    FIRST = "first"
    LAST = "last"


class MatcherSide(BaseModel):
    """One quantified side of an ordering constraint."""

    quantifier: Quantifier
    match: TraceMatcher

    model_config = {"extra": "forbid"}


class AnchorSide(BaseModel):
    """The single event a window is measured from.

    ``any`` and ``all`` are rejected rather than accepted and collapsed: over a
    prefix or an interval ``any`` means ``first`` and ``all`` means ``last``, so the
    four-value domain would carry two verdicts under four spellings. One selected
    anchor is also what makes the window one interval rather than a cross-product of
    every start against every end.
    """

    quantifier: AnchorQuantifier
    match: TraceMatcher

    model_config = {"extra": "forbid"}

    @field_validator("quantifier", mode="before")
    @classmethod
    def _reject_the_quantifiers_a_window_cannot_read(cls, value: Any) -> Any:
        # Before the enum, which would answer with a bare literal error naming
        # neither the collapse nor the spelling that expresses the same intent.
        collapses_onto = {
            Quantifier.ANY.value: AnchorQuantifier.FIRST,
            Quantifier.ALL.value: AnchorQuantifier.LAST,
        }
        # Any other malformed value — including an unhashable one a dict or list
        # under the key would give — is left to the enum to reject.
        equivalent = collapses_onto.get(value) if isinstance(value, str) else None
        if equivalent is not None:
            raise ValueError(
                f"an anchor cannot be quantified {value!r}: over a window {value!r} means "
                f"{equivalent.value!r}, and admitting both would give one verdict two "
                f"spellings. Write {equivalent.value!r}"
            )
        return value


class AdjacencyView(str, Enum):
    """The event sequence adjacency is read in.

    There is no default. Events interleave inside a turn — a call's own result sits
    between it and the next call — so every candidate default is wrong for some
    common intent: ``tool_calls`` cannot express confirm-before-acting, where one
    side is a message, and ``events`` cannot express two consecutive calls.
    """

    TOOL_CALLS = "tool_calls"
    TOOL_RESULTS = "tool_results"
    MESSAGES = "messages"
    EVENTS = "events"


class PresentConstraint(BaseModel):
    """At least one event matches."""

    match: TraceMatcher

    model_config = {"extra": "forbid"}


class AbsentConstraint(BaseModel):
    """No event matches."""

    match: TraceMatcher

    model_config = {"extra": "forbid"}


class CountConstraint(BaseModel):
    """The number of matching events is within the declared bounds."""

    match: TraceMatcher
    min: int | None = Field(default=None, ge=0)
    max: int | None = Field(default=None, ge=0)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _require_a_bound_that_can_fail(self) -> CountConstraint:
        if self.min is None and self.max is None:
            raise ValueError(
                "a count constraint declares neither min nor max, so every match count "
                "satisfies it. Declare at least one bound"
            )
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(
                f"count bounds min {self.min} and max {self.max} admit no match count, so "
                "the constraint fails however the agent behaves"
            )
        return self


class BeforeConstraint(BaseModel):
    """The left side is ordered before the right side."""

    left: MatcherSide
    right: MatcherSide

    model_config = {"extra": "forbid"}


class ImmediatelyBeforeConstraint(BaseModel):
    """The left side is adjacent to the right side in the named view."""

    left: MatcherSide
    right: MatcherSide
    among: AdjacencyView

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def _require_the_view_adjacency_is_read_in(cls, data: Any) -> Any:
        # Before the required-field error, which names the field but not why no
        # default can be right.
        if isinstance(data, dict) and data.get("among") is None:
            raise ValueError(
                "immediately_before needs an explicit among: adjacency has no meaning "
                "until the sequence it is read in is named, and no default is right for "
                f"every intent. Write one of {[view.value for view in AdjacencyView]}"
            )
        return data


class AbsentBeforeConstraint(BaseModel):
    """Nothing forbidden occurs strictly before the anchor.

    LTLf ``¬A U B``, and the primitive a no-prefill check is written with.
    """

    forbidden: TraceMatcher
    anchor: AnchorSide

    model_config = {"extra": "forbid"}


class AbsentBetweenConstraint(BaseModel):
    """Nothing forbidden occurs strictly between the two anchors."""

    forbidden: TraceMatcher
    start: AnchorSide
    end: AnchorSide

    model_config = {"extra": "forbid"}


class TraceConstraintKind(str, Enum):
    """One member of the closed constraint vocabulary.

    Each member names a field of :class:`TraceConstraintExpr` holding that kind's
    payload, so a kind and the payload address that reaches it are the same token.
    """

    PRESENT = "present"
    ABSENT = "absent"
    COUNT = "count"
    BEFORE = "before"
    IMMEDIATELY_BEFORE = "immediately_before"
    ABSENT_BEFORE = "absent_before"
    ABSENT_BETWEEN = "absent_between"
    ALL_OF = "all_of"
    ANY_OF = "any_of"
    NEGATE = "negate"


TRACE_CONSTRAINT_KINDS: frozenset[TraceConstraintKind] = frozenset(
    {
        TraceConstraintKind.PRESENT,
        TraceConstraintKind.ABSENT,
        TraceConstraintKind.COUNT,
        TraceConstraintKind.BEFORE,
        TraceConstraintKind.IMMEDIATELY_BEFORE,
        TraceConstraintKind.ABSENT_BEFORE,
        TraceConstraintKind.ABSENT_BETWEEN,
        TraceConstraintKind.ALL_OF,
        TraceConstraintKind.ANY_OF,
        TraceConstraintKind.NEGATE,
    }
)
"""The closed constraint vocabulary, written out rather than read off
:class:`TraceConstraintExpr` or off :class:`TraceConstraintKind`, so the totality
lock compares sources that can disagree."""

# Kinds whose verdict is about the match itself, for which an unmatched-anchor
# policy would decide the very thing the constraint asserts. On ``present`` the
# pair is worse than redundant: unmatched would pass by the policy and matched by
# the constraint, so the check could not fail.
_KINDS_WITHOUT_AN_ANCHOR: frozenset[TraceConstraintKind] = frozenset(
    {TraceConstraintKind.PRESENT, TraceConstraintKind.ABSENT, TraceConstraintKind.COUNT}
)

# Over one matched set, ``last`` and ``all`` on the left require the event nothing
# follows to precede something, and ``first`` and ``all`` on the right require
# something to precede the event nothing precedes — false at every trajectory. Their
# complements, written out below, are the quantifiers an ordering over one matcher
# survives on.
_LEFT_QUANTIFIERS_LEAVING_A_SUCCESSOR: frozenset[Quantifier] = frozenset(
    {Quantifier.FIRST, Quantifier.ANY}
)
_RIGHT_QUANTIFIERS_LEAVING_A_PREDECESSOR: frozenset[Quantifier] = frozenset(
    {Quantifier.LAST, Quantifier.ANY}
)

# The one window over a self-referential ``absent_between`` that some trial opens
# and some trial does not: from the first match to the last, which is non-empty
# exactly when the events occur twice.
_SELF_REFERENTIAL_WINDOW_SOME_TRIAL_OPENS = (AnchorQuantifier.FIRST, AnchorQuantifier.LAST)

_ORDERING_KINDS: frozenset[TraceConstraintKind] = frozenset(
    {TraceConstraintKind.BEFORE, TraceConstraintKind.IMMEDIATELY_BEFORE}
)


class TraceConstraintExpr(BaseModel):
    """Exactly one constraint kind, holding that kind's own payload.

    One optional field per kind, rather than a ``require: <name>`` discriminator
    over a flat payload: each payload is then independently typed and
    ``extra="forbid"`` checks it on its own, so ``before`` carrying ``min`` is a
    load error by construction instead of a hand-written cross-field rule repeated
    ten times.

    The three composite kinds nest expressions, which carry no ``id`` / ``weight`` /
    ``on_missing`` — those belong to the scored constraint, never to a sub-term.
    """

    present: PresentConstraint | None = None
    absent: AbsentConstraint | None = None
    count: CountConstraint | None = None
    before: BeforeConstraint | None = None
    immediately_before: ImmediatelyBeforeConstraint | None = None
    absent_before: AbsentBeforeConstraint | None = None
    absent_between: AbsentBetweenConstraint | None = None
    all_of: list[TraceConstraintExpr] | None = Field(default=None, min_length=1)
    any_of: list[TraceConstraintExpr] | None = Field(default=None, min_length=1)
    negate: TraceConstraintExpr | None = None

    model_config = {"extra": "forbid"}

    def declared_kind(self) -> TraceConstraintKind:
        """The one constraint kind this expression is."""
        return next(iter(self.declared_kinds()))

    def declared_kinds(self) -> frozenset[TraceConstraintKind]:
        """Every constraint kind carrying a payload — exactly one after validation."""
        return frozenset(
            kind for kind in TRACE_CONSTRAINT_KINDS if getattr(self, kind.value) is not None
        )

    @model_validator(mode="after")
    def _require_exactly_one_kind(self) -> TraceConstraintExpr:
        declared = sorted(kind.value for kind in self.declared_kinds())
        if len(declared) != 1:
            raise ValueError(
                f"a constraint expression declares {declared or 'no kind'}, and exactly one "
                f"of {sorted(kind.value for kind in TRACE_CONSTRAINT_KINDS)} is required. "
                "Two conditions are an "
                "all_of over two expressions"
            )
        return self

    @model_validator(mode="after")
    def _reject_an_order_over_one_matcher_that_no_trial_decides(self) -> TraceConstraintExpr:
        """Two sides selecting one set of events, in a shape no author means to write.

        An ordering whose sides carry the same matcher is satisfiable only where the
        left selection can leave a later event and the right selection an earlier
        one; every other quantifier pair fails whatever the agent did, and so does a
        window measured between two selections of one set unless it runs from the
        first match to the last. Anchoring ``absent_before`` at its own ``first`` is
        the one rejected shape here that is not a constant: nothing precedes the
        first match, so it decides exactly what ``present`` decides.
        """
        kind = self.declared_kind()
        payload = getattr(self, kind.value)
        if kind in _ORDERING_KINDS and payload.left.match == payload.right.match:
            if not (
                payload.left.quantifier in _LEFT_QUANTIFIERS_LEAVING_A_SUCCESSOR
                and payload.right.quantifier in _RIGHT_QUANTIFIERS_LEAVING_A_PREDECESSOR
            ):
                raise ValueError(
                    f"{kind.value} orders one matcher against itself, quantified "
                    f"{payload.left.quantifier.value!r} before "
                    f"{payload.right.quantifier.value!r}, which no trajectory satisfies: "
                    "over the events one matcher selects, nothing follows the last of them "
                    "and nothing precedes the first. Quantify the left side 'first' or "
                    "'any' and the right side 'last' or 'any' for a shape a trajectory "
                    "can decide, or give the two sides different matchers"
                )
        if (
            kind is TraceConstraintKind.ABSENT_BEFORE
            and payload.forbidden == payload.anchor.match
            and payload.anchor.quantifier is AnchorQuantifier.FIRST
        ):
            raise ValueError(
                "absent_before forbids the events its own anchor is selected from, "
                "anchored 'first': nothing precedes the first of them, so the constraint "
                "reduces to 'the events occurred at all' — present, written the long way "
                "round. Write a present constraint, anchor it 'last' to assert the events "
                "occur once, or forbid a different matcher"
            )
        if kind is TraceConstraintKind.ABSENT_BETWEEN and (
            payload.forbidden == payload.start.match == payload.end.match
        ):
            self._require_a_self_referential_window_some_trial_opens(payload)
        return self

    @staticmethod
    def _require_a_self_referential_window_some_trial_opens(
        payload: AbsentBetweenConstraint,
    ) -> None:
        anchors = (payload.start.quantifier, payload.end.quantifier)
        if anchors == _SELF_REFERENTIAL_WINDOW_SOME_TRIAL_OPENS:
            return
        raise ValueError(
            "absent_between forbids the events its own window is measured between, "
            f"from the {payload.start.quantifier.value!r} of them to the "
            f"{payload.end.quantifier.value!r}, which leaves no interval any trajectory "
            "opens: on_missing then decides the verdict however the agent behaved. Measure "
            "from 'first' to 'last' to assert the events occur exactly twice, or forbid a "
            "different matcher"
        )


class OnMissing(str, Enum):
    """What an anchor that matched nothing decides."""

    FAIL = "fail"
    PASS = "pass"


class TurnWindow(BaseModel):
    """An inclusive turn range every matcher in a constraint is restricted to.

    The opening user prompt shares turn 0 with the first assistant turn, so
    ``first_turn: 0`` includes it. "Before the first user message" is therefore not
    expressible as a window — that window is always empty, and the intent is
    ``absent_before``.
    """

    first_turn: int | None = Field(default=None, ge=0)
    last_turn: int | None = Field(default=None, ge=0)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _require_a_window_some_turn_falls_in(self) -> TurnWindow:
        if self.first_turn is None and self.last_turn is None:
            raise ValueError(
                "a turn window declares neither first_turn nor last_turn, so it restricts "
                "nothing. Declare a bound, or drop within"
            )
        if (
            self.first_turn is not None
            and self.last_turn is not None
            and self.first_turn > self.last_turn
        ):
            raise ValueError(
                f"turn window first_turn {self.first_turn} is above last_turn "
                f"{self.last_turn}, so no turn falls inside it and every matcher in the "
                "constraint selects nothing"
            )
        return self


class TraceConstraint(BaseModel):
    """One scored trajectory condition.

    ``weight`` scales its share of the component score, and ``on_missing`` decides
    an anchor that matched nothing — defaulting to a named failing sub-check, so an
    unmatched anchor is never silently satisfied.
    """

    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    weight: float = 1.0
    on_missing: OnMissing | None = None
    within: TurnWindow | None = None
    require: TraceConstraintExpr

    model_config = {"extra": "forbid"}

    @field_validator("weight")
    @classmethod
    def _require_a_weight_that_scores(cls, value: float) -> float:
        """A positive weight, so the component's fold has a denominator.

        Every weight being positive makes ``Σ(weight) > 0`` an invariant of a
        populated constraint list, which is what lets the fold divide without a
        zero-denominator branch and an invented convention for what an all-zero
        weight set should score.
        """
        if not math.isfinite(value):
            raise ValueError(
                f"weight {value} is not a finite number, so the share of the score it "
                "scales has no value. Write a positive weight"
            )
        if value <= 0.0:
            raise ValueError(
                f"weight {value} scores nothing: a zero-weight constraint is evaluated and "
                "reported while contributing to neither the numerator nor the denominator "
                "of the component score, and a negative one inverts the fold. A check that "
                "must hold without being scored is severity: gate (#680)"
            )
        return value

    @model_validator(mode="after")
    def _reject_an_unmatched_anchor_policy_where_nothing_is_anchored(self) -> TraceConstraint:
        kind = self.require.declared_kind()
        if self.on_missing is not None and kind in _KINDS_WITHOUT_AN_ANCHOR:
            raise ValueError(
                f"{self.id}: on_missing has nothing to decide on a {kind.value!r} constraint, "
                "whose verdict is the match itself. Setting it would answer the very "
                "question the constraint asks"
            )
        return self


class TraceChecksConfig(BaseModel):
    """Declarative conditions on what the agent did, and in what order."""

    constraints: list[TraceConstraint] = Field(min_length=1)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _require_distinct_constraint_ids(self) -> TraceChecksConfig:
        counted = Counter(item.id for item in self.constraints)
        duplicated = sorted(name for name, total in counted.items() if total > 1)
        if duplicated:
            raise ValueError(
                f"constraint ids {duplicated} are declared more than once. Each id names "
                "one sub-check in the grade, so a repeat makes two results indistinguishable"
            )
        return self


class Criterion(BaseModel):
    """A single rubric criterion the judge scores independently.

    ``description`` is an imperative pass-condition the judge evaluates.
    ``kind`` selects binary (met / not-met → 0 or 1) or graded (0–1 gradient).
    A failed ``required`` criterion fails the whole rubric regardless of others.
    ``expected`` is an optional author-written reference shown to the judge for
    this criterion (e.g. the correct value to look for).
    """

    id: str
    description: str
    weight: float = 1.0
    kind: Literal["binary", "graded"] = "binary"
    required: bool = False
    expected: str | None = None

    model_config = {"extra": "forbid"}


class Rubric(BaseModel):
    """Structured grading rubric — the replacement for the old free-text blob.

    ``reference`` is an optional author-written reference (correct answer /
    policy summary) shown to the judge alongside the per-criterion ``expected``.

    Criterion ids are validated at construction (see ``_validate_criterion_ids``)
    so that ``validate`` / config-load fail loud before any judge runs — the
    derived ``submit_report`` tool schema (``core/grading/rubric.py``) relies on
    these guarantees (unique, identifier-safe ids that never collide with the
    reserved overall ``reasons`` key or the per-criterion ``<id>_justification``
    derived keys).
    """

    criteria: list[Criterion]
    reference: str | None = None

    model_config = {"extra": "forbid"}

    # Reserved keys in the generated submit_report tool schema. Encoded inline
    # here (not imported from core/grading/rubric.py) to avoid an import cycle;
    # build_submit_report_tool must keep these in sync.
    _RESERVED_OVERALL_KEY = "reasons"
    _JUSTIFICATION_SUFFIX = "_justification"
    _SAFE_ID_PATTERN = r"^[A-Za-z][A-Za-z0-9_]*$"

    @model_validator(mode="after")
    def _validate_criterion_ids(self) -> Rubric:
        """Fail loud on unsafe / colliding criterion ids.

        Guards the construction seam so a malformed rubric is rejected at
        config-load / ``validate`` time, never at judge runtime. Rules:
          (a) no duplicate ``criterion.id``;
          (b) no id equal to the reserved overall key ``"reasons"``;
          (c) no id ending in ``"_justification"``, and no id colliding with
              another criterion's derived ``"<id>_justification"`` key;
          (d) every id matches ``^[A-Za-z][A-Za-z0-9_]*$`` (identifier-safe).
        """

        ids = [c.id for c in self.criteria]

        seen: set[str] = set()
        duplicates = {cid for cid in ids if cid in seen or seen.add(cid)}
        if duplicates:
            raise ValueError(
                f"Rubric criterion ids must be unique; duplicates: {sorted(duplicates)}."
            )

        for cid in ids:
            if not re.match(self._SAFE_ID_PATTERN, cid):
                raise ValueError(
                    f"Rubric criterion id {cid!r} is not identifier-safe; it must "
                    f"match {self._SAFE_ID_PATTERN} (letter first, then letters / "
                    f"digits / underscores)."
                )
            if cid == self._RESERVED_OVERALL_KEY:
                raise ValueError(
                    f"Rubric criterion id {cid!r} is reserved — it collides with the "
                    f"overall '{self._RESERVED_OVERALL_KEY}' field in the submit_report "
                    f"tool schema. Rename the criterion."
                )
            if cid.endswith(self._JUSTIFICATION_SUFFIX):
                raise ValueError(
                    f"Rubric criterion id {cid!r} must not end with "
                    f"'{self._JUSTIFICATION_SUFFIX}' — that suffix is reserved for the "
                    f"per-criterion justification fields in the submit_report schema."
                )

        id_set = set(ids)
        for cid in ids:
            derived = f"{cid}{self._JUSTIFICATION_SUFFIX}"
            if derived in id_set:
                raise ValueError(
                    f"Rubric criterion id {derived!r} collides with the derived "
                    f"justification key for criterion {cid!r}. Rename one of them."
                )

        return self


class CriterionResult(BaseModel):
    """Per-criterion judge output.

    ``score`` is 0/1 for binary criteria and 0–1 for graded ones. ``met`` is the
    binary verdict (for graded criteria, whether it cleared the author's bar).
    """

    id: str
    met: bool
    score: float
    justification: str

    model_config = {"extra": "forbid"}


class JudgeCustomization(BaseModel):
    """Judge tool-surface customization, sibling of ``rubric`` under ``llm_judge``.

    Tri-state ``disable_knowledge_search``: ``None`` (unset) means the faithful
    default (the judge is offered whatever knowledge-search tools the agent had);
    ``True`` withholds every knowledge-search tool from the judge's surface;
    ``False`` explicitly keeps them (so a task can override a project default that
    disabled them). Layered project→task by :func:`resolve_effective_judge_customization`.

    ``system_prompt`` replaces the default judge system-prompt body; the harness
    always appends the marker contract, so a custom prompt can never break
    ``submit_report`` validation. ``None`` (unset) keeps the default prompt; a task
    sets ``null`` to reset a project-level custom prompt back to the default. An
    empty or whitespace-only string is rejected at load — a marker-only prompt is
    almost certainly a mistake.

    Tri-state ``include_agent_system_prompt``: ``None`` (unset) and ``True`` both
    embed the agent's policy / system prompt in the judge's opening-message
    evidence (today's behaviour); ``False`` omits that section so a self-contained
    rubric grades without the agent's framing. Evidence gating, distinct from
    ``system_prompt`` (which is the judge's own wording). A task sets ``true`` or
    ``null`` to re-include over a project ``false``.
    """

    disable_knowledge_search: bool | None = None
    system_prompt: str | None = None
    include_agent_system_prompt: bool | None = None

    model_config = {"extra": "forbid"}

    @field_validator("system_prompt")
    @classmethod
    def _reject_blank_system_prompt(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError(
                "grading.llm_judge.customization.system_prompt must not be empty or "
                "whitespace-only; omit the key (or set it to null) to use the default "
                "judge prompt."
            )
        return value


class LLMJudgeConfig(BaseModel):
    """LLM-based grading configuration.

    Canonical home for the judge config that crosses both the YAML grading block
    and the gRPC wire (serialized inside ``TrialSpec.task.grading``). The
    ``output_schema`` field was dropped — the judge's structured-output schema is
    derived from the rubric's criteria (Stage 3), not author-specified. The
    judge *model* is no longer pinned here: it lives at the run level under
    ``RunConfig.models["judge"]`` and rides ``TrialSpec.judge_model_config``.
    ``customization`` is attached only when a config layer sets it, so a task
    with no customization block serializes an identical config.
    """

    rubric: Rubric  # Structured grading rubric
    customization: JudgeCustomization | None = None

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_rubric_shape(cls, data: Any) -> Any:
        """Fail loud with a migration message on legacy llm_judge shapes.

        The free-text ``rubric: str`` blob, the removed ``output_schema`` field,
        and the relocated ``model_ref`` field are all pre-relocation contracts.
        Rubric grading never worked end-to-end, so this is an intentional,
        non-back-compatible break (see docs/RUBRIC_GRADING_DESIGN.md).
        """
        if not isinstance(data, dict):
            return data
        if "model_ref" in data:
            raise ValueError(
                "grading.llm_judge.model_ref moved to the run config under "
                "models.judge; remove it from grading.yaml."
            )
        if isinstance(data.get("rubric"), str):
            raise ValueError(
                "grading.llm_judge.rubric is now a structured Rubric, not free "
                'text. Replace `rubric: "<text>"` with:\n'
                "  rubric:\n"
                "    reference: <optional author-written reference>\n"
                "    criteria:\n"
                "      - id: <criterion_id>\n"
                "        description: <imperative pass-condition>\n"
                "        kind: binary  # or graded\n"
                "        required: false\n"
                "        weight: 1.0\n"
                "        expected: <optional per-criterion reference>"
            )
        if "output_schema" in data:
            raise ValueError(
                "grading.llm_judge.output_schema has been removed. The judge's "
                "structured-output schema is derived from the rubric's criteria; "
                "delete the `output_schema` field from grading.llm_judge."
            )
        return data


class RunnerGradingConfig(BaseModel):
    """
    Complete grading configuration.

    Supports multiple methods combined with weights.
    """

    combine_method: CombineMethod = "weighted"
    weights: dict[str, float] = Field(default_factory=lambda: {"state_checks": 1.0})
    pass_threshold: float = 0.8

    @field_validator("combine_method", mode="before")
    @classmethod
    def _validate_combine_method(cls, value: Any) -> Any:
        # Before the Literal, which would answer a retired alias with a bare
        # literal_error naming no replacement.
        return validate_combine_method(value, context="TaskDescription grading.combine_method")

    # Declarative grading dispatch — adapters tell the runner *how* to grade in data,
    # so the runner never infers it from the adapter's identity.
    #
    # Values:
    #   ``None`` (default)
    #     Standard grading: combine state checks / transcript rules / LLM judge
    #     using ``weights`` and ``pass_threshold``. Most adapters want this.
    #   ``"test_execution"``
    #     Run a reference test suite inside the trial env via an exec-capable
    #     lifecycle tool (today: ``DockerComposeExecToolWrapper``) and score by
    #     reading the reward written to ``/logs/verifier/reward.txt``. Requires
    #     such a tool to be present in ``TaskDescription.agent_tools`` — without
    #     one the runner returns a clear error at ``GradeTrial`` time.
    #   ``"hash"`` / ``"transcript"`` / ``"llm"``
    #     Reserved names for future single-method dispatch; not currently used
    #     for dispatch (today their behaviour is part of the default path).
    grading_method: Literal["hash", "test_execution", "transcript", "llm"] | None = None

    state_checks: RunnerStateChecksConfig | None = None
    transcript_rules: RunnerTranscriptRulesConfig | None = None
    trace_checks: TraceChecksConfig | None = None
    llm_judge: LLMJudgeConfig | None = None

    # Custom Python checks (``@init`` + ``@check`` in a pack's ``checks.py``).
    # Loose ``dict[str, Any]`` here — the runner validates it into
    # :class:`~tolokaforge.core.grading.checks_interface.CustomChecksConfig` at
    # both ``RegisterTrial`` (fail-loud on interface_version / module load) and
    # ``GradeTrial`` (before executor dispatch); mirrors the host-side
    # :class:`~tolokaforge.core.models.GradingConfig` so an unchanged
    # ``task.yaml`` round-trips through both.
    custom_checks: dict[str, Any] | None = None

    model_config = {"extra": "forbid"}


# =============================================================================
# EnvironmentManifest — Docker Compose as source of truth
# =============================================================================


InitialStateKind = Literal["sql", "copy", "script"]


def _check_safe_relative_path(value: str, field_label: str) -> None:
    """Raise ``ValueError`` unless ``value`` is a non-empty relative path with
    no ``..`` segments, no shell-expansion sequences, and no home-directory
    expansion. Used to keep manifest-declared paths inside the task pack root.
    """
    if not value:
        raise ValueError(f"{field_label} must be a non-empty path.")
    if "$" in value:
        raise ValueError(
            f"{field_label} contains a shell-expansion sequence ({value!r}); "
            "not allowed — expansion happens at provision time and escapes "
            "the load-time safety check."
        )
    if value.startswith("~"):
        raise ValueError(
            f"{field_label} starts with '~' ({value!r}); home-directory "
            "expansion happens at provision time and escapes the load-time "
            "safety check."
        )
    path = PurePosixPath(value)
    if path.is_absolute():
        raise ValueError(f"{field_label} must be a relative path; got absolute {value!r}.")
    if any(part == ".." for part in path.parts):
        raise ValueError(f"{field_label} must not contain '..' segments; got {value!r}.")


class InitialStateRef(BaseModel):
    """Reference to a fixture that initialises a service's state."""

    from_: str = Field(alias="from")
    """Path to the fixture, relative to the task pack root. Non-empty."""

    kind: InitialStateKind = "copy"
    """How the provisioner applies the fixture: ``"sql"`` pipes through the
    service's SQL client, ``"copy"`` writes the file inside the container,
    ``"script"`` executes it as a script in the container."""

    model_config = {"extra": "forbid", "populate_by_name": True}

    @field_validator("from_")
    @classmethod
    def _from_is_safe_relative_path(cls, v: str) -> str:
        _check_safe_relative_path(v, "InitialStateRef.from")
        return v


class SecurityContext(BaseModel):
    """Per-container security policy declarations. The provisioner applies
    these to every service that does not override them in the compose file.
    """

    run_as_user: int | str | None = None
    """UID or username the container process runs as. ``None`` defers to
    the image default. A username (e.g. ``"toloka"``) is resolved to a
    numeric UID by the substrate at materialisation time — some
    substrates (k8s ``runAsUser``) require the numeric form."""

    run_as_group: int | str | None = None
    """GID or group name the container process runs as. ``None`` defers
    to the image default. Same resolution rules as ``run_as_user``."""

    read_only_root_filesystem: bool = False
    """When ``True``, the container's root filesystem is mounted read-only.
    Writable paths must be declared as volumes."""

    no_new_privileges: bool = True
    """When ``True``, the container cannot gain new privileges via setuid
    binaries or file capabilities. Default ``True`` — safer posture."""

    capabilities_drop: list[str] = Field(default_factory=lambda: ["ALL"])
    """Linux capabilities to drop. Default drops ``ALL`` — start from no
    capabilities and add back only what's needed via ``capabilities_add``."""

    capabilities_add: list[str] = Field(default_factory=list)
    """Linux capabilities to add back after ``capabilities_drop`` runs."""

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_user_group(cls, data: Any) -> Any:
        return coerce_security_context_aliases(data)


class NetworkPolicy(str, Enum):
    """Network posture the provisioner is asked to enforce for a trial."""

    NO_INTERNET = "no_internet"
    """Services reach each other on the per-trial network only. No egress
    to the public internet, no reachability across per-trial projects."""

    LIMITED_INTERNET = "limited_internet"
    """Egress permitted only to the hostnames declared in
    :attr:`EnvironmentManifest.limited_internet_allowlist`. No cross-trial
    reachability. The allowlist is required (non-empty) under this policy and
    forbidden under the others."""

    FULL_INTERNET = "full_internet"
    """Unrestricted egress. Still no cross-trial reachability."""


class TaskIsolation(str, Enum):
    """Computed backend-selection signal derived from a manifest's
    per-service ``isolation`` map.

    ``PER_TRIAL`` means at least one service in the manifest is labelled
    ``reset`` or ``ephemeral`` (or the manifest is set with no explicit
    services), so the orchestrator selects
    :class:`~tolokaforge.core.per_trial_runtime.PerTrialRuntimeBackend`.
    ``SHARED_OK`` means every declared service is labelled ``shared``,
    so :class:`~tolokaforge.core.shared_stack_runtime.SharedStackRuntimeBackend`
    is chosen. Not authored in YAML — computed from
    :attr:`EnvironmentManifest.services` by
    :attr:`EnvironmentManifest.requires_per_trial`.
    """

    PER_TRIAL = "per_trial"
    SHARED_OK = "shared_ok"


ServiceIsolation = Literal["shared", "reset", "ephemeral"]
"""Per-service isolation vocabulary.

* ``shared`` — service persists across trials (state carries over).
* ``reset`` — service is reset between trials via a seed-backed recipe
  named by :attr:`ServiceSpec.reset`.
* ``ephemeral`` — service is torn down and recreated between trials.
"""


ServiceNetworkAccess = Literal["default", "restricted"]
"""Per-service network-access vocabulary.

* ``default`` — the harness attaches the service to the shared internal
  network it injects to enforce :class:`NetworkPolicy` (under
  ``no_internet`` this is ``tolokaforge_netpolicy_internal``; under
  ``limited_internet`` the service also receives the proxy-env
  variables that route egress through the allowlisted squid). Every
  first-party sibling service (runner, db-service, rag) reaches every
  other on that shared network.
* ``restricted`` — the service joins only the networks declared for it
  in the compose file (a task-owned network), not the harness-injected
  shared network. Under ``limited_internet`` the service also receives
  no proxy-env variables — an untrusted sibling cannot reach the
  runner, db-service, rag, or the internet-egress squid. The compose
  file must declare a non-empty ``networks:`` block for the service so
  it has somewhere to attach; the manifest validator rejects a
  ``restricted`` service that lacks one.
"""


class ResetSpec(BaseModel):
    """Seed pointer for a service labelled ``reset``. Names the entry in
    ``project.assets.seeds`` whose recipe restores the service to a
    known baseline between trials."""

    seed: str
    """Name of the seed entry in the project's ``assets.seeds`` map."""

    model_config = {"extra": "forbid"}


ReadinessKind = Literal["grpc", "http", "tcp"]
"""Per-service readiness-probe vocabulary — the endpoint kind the
provisioner probes for client-side reachability.

* ``grpc`` — probe the gRPC channel on the runner port / first published
  port until it reaches READY.
* ``http`` — probe ``GET /health`` on the first published port; 2xx is ready.
* ``tcp`` — probe a TCP connect to the first published port.
"""


class ReadinessSpec(BaseModel):
    """Per-service readiness declaration — gates provisioning on client-side
    reachability of the service's published endpoint.

    Port and path are resolved by convention (see :data:`ReadinessKind`);
    ``kind`` is the only knob in v1.
    """

    kind: ReadinessKind
    """Endpoint kind to probe — see :data:`ReadinessKind`."""

    model_config = {"extra": "forbid"}


class ServiceSpec(BaseModel):
    """Per-service manifest entry — the harness's declaration of how a
    compose service is treated between trials.

    ``reset`` is required exactly when ``isolation == "reset"`` and
    forbidden otherwise; :meth:`_check_reset_agrees_with_isolation`
    enforces the invariant so a stale ``reset`` sibling from a deep
    merge fails loud at load.
    """

    isolation: ServiceIsolation
    """Per-service isolation label — see :data:`ServiceIsolation`."""

    reset: ResetSpec | None = None
    """Reset recipe pointer. Required for ``isolation="reset"``, forbidden
    for the other labels."""

    network_access: ServiceNetworkAccess = "default"
    """Per-service network-access label — see :data:`ServiceNetworkAccess`.
    Orthogonal to :attr:`isolation` and :attr:`reset`; every combination
    is legal."""

    readiness: ReadinessSpec | None = None
    """Provision-time readiness contract — see :class:`ReadinessSpec`.
    ``None`` means the service is not gated by an explicit contract (the
    docker healthcheck is the only readiness signal). Orthogonal to
    :attr:`isolation`, :attr:`reset`, and :attr:`network_access`; every
    combination is legal."""

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _check_reset_agrees_with_isolation(self) -> ServiceSpec:
        if self.isolation == "reset" and self.reset is None:
            raise ValueError(
                "ServiceSpec: isolation='reset' requires a 'reset.seed' pointer; "
                "declare `reset: {seed: <name>}` or change isolation."
            )
        if self.isolation != "reset" and self.reset is not None:
            raise ValueError(
                f"ServiceSpec: isolation={self.isolation!r} cannot carry a 'reset' "
                "recipe. Either change isolation to 'reset' or drop the 'reset' "
                "sibling (e.g. `reset: null` on the overriding side)."
            )
        return self


def _compose_services(content: dict[str, Any]) -> dict[str, dict[str, Any]]:
    services = content.get("services")
    if not isinstance(services, dict) or not services:
        raise ValueError("compose file must declare a non-empty top-level `services:` mapping")
    for name, body in services.items():
        if not isinstance(name, str) or not name:
            raise ValueError(f"compose service names must be non-empty strings; got {name!r}")
        if not isinstance(body, dict):
            raise ValueError(
                f"compose service {name!r} must be a mapping; got {type(body).__name__}"
            )
    return services


def _check_no_host_network(services: dict[str, dict[str, Any]]) -> None:
    for name, body in services.items():
        if body.get("network_mode") == "host":
            raise ValueError(
                f"compose service {name!r} sets `network_mode: host`; not allowed "
                "under the manifest's network-isolation contract."
            )


def _check_no_privileged(services: dict[str, dict[str, Any]]) -> None:
    for name, body in services.items():
        privileged = body.get("privileged")
        if privileged is None or privileged is False:
            continue
        # Reject any truthy or non-bool value; catches `privileged: true`,
        # `privileged: "true"` (quoted string), `privileged: 1`, and stray
        # non-bool declarations that YAML would parse to a truthy value.
        raise ValueError(
            f"compose service {name!r} declares `privileged: {privileged!r}`; not "
            "allowed under the manifest's safety contract."
        )


def _check_no_cap_add(services: dict[str, dict[str, Any]]) -> None:
    for name, body in services.items():
        cap_add = body.get("cap_add")
        if cap_add:
            raise ValueError(
                f"compose service {name!r} sets `cap_add: {cap_add!r}`; not allowed "
                "under the manifest's safety contract (start from ALL-dropped, "
                "declare additions via SecurityContext.capabilities_add if needed)."
            )


def _check_safe_bind_mounts(services: dict[str, dict[str, Any]]) -> None:
    for name, body in services.items():
        if "volumes" not in body:
            continue
        volumes = body["volumes"]
        if not isinstance(volumes, list):
            raise ValueError(
                f"compose service {name!r}: `volumes:` must be a list; got {type(volumes).__name__}"
            )
        for idx, entry in enumerate(volumes):
            source = _bind_mount_source(entry)
            if source is None:
                continue
            _check_safe_relative_path(source, f"compose service {name!r} volumes[{idx}].source")


_FLOATING_IMAGE_TAGS = frozenset(
    {
        "latest",
        "main",
        "master",
        "edge",
        "stable",
        "dev",
        "develop",
        "nightly",
        "head",
    }
)

_IMAGE_DIGEST_RE = re.compile(r"^(?:sha256:[a-fA-F0-9]{64}|sha512:[a-fA-F0-9]{128})$")


def _image_tag_or_digest(image: str) -> tuple[str | None, str | None]:
    """Return ``(tag, digest)`` from a docker image reference. Either may be
    ``None``. When a digest is set, the tag is reported as ``None`` even if a
    tag is also present in the reference (digest takes precedence)."""
    if "@" in image:
        _, digest = image.rsplit("@", 1)
        return None, digest
    last_segment = image.rsplit("/", 1)[-1]
    if ":" not in last_segment:
        return None, None
    return last_segment.split(":", 1)[1], None


def _check_pinned_images(services: dict[str, dict[str, Any]]) -> None:
    """Reject floating tags (:latest, :main, :master, :edge, :stable, :dev,
    :develop, :nightly, :head) and bare image references; require an
    immutable tag or a ``sha256`` / ``sha512`` digest. Compose services that
    declare ``build:`` instead of ``image:`` are exempt (no tag to check)."""
    for name, body in services.items():
        image = body.get("image")
        if image is None:
            continue
        if not isinstance(image, str) or not image:
            raise ValueError(
                f"compose service {name!r}: `image:` must be a non-empty string; got {image!r}"
            )
        tag, digest = _image_tag_or_digest(image)
        if digest is not None:
            if not _IMAGE_DIGEST_RE.fullmatch(digest):
                raise ValueError(
                    f"compose service {name!r}: image digest must be a well-formed "
                    f"`sha256:<64-hex>` or `sha512:<128-hex>` reference; got {image!r}."
                )
            continue
        if tag is None:
            raise ValueError(
                f"compose service {name!r}: `image: {image!r}` must include an "
                "explicit tag or digest for reproducibility."
            )
        if tag == "":
            raise ValueError(
                f"compose service {name!r}: image tag must be non-empty; got {image!r}."
            )
        if tag.lower() in _FLOATING_IMAGE_TAGS:
            raise ValueError(
                f"compose service {name!r}: `image: {image!r}` uses a floating tag "
                f"({tag!r}); pin to an immutable tag or a digest for reproducibility."
            )


def _check_depends_on_resolves(services: dict[str, dict[str, Any]]) -> None:
    """Every service named in a ``depends_on`` entry must be declared in
    the compose file. Supports both short form (list of service names) and
    long form (mapping from service name to a condition dict)."""
    for name, body in services.items():
        deps = body.get("depends_on")
        if deps is None:
            continue
        if isinstance(deps, list):
            dep_names = deps
        elif isinstance(deps, dict):
            dep_names = list(deps.keys())
        else:
            raise ValueError(
                f"compose service {name!r}: `depends_on:` must be a list or a "
                f"mapping; got {type(deps).__name__}"
            )
        for dep in dep_names:
            if not isinstance(dep, str):
                raise ValueError(
                    f"compose service {name!r}: `depends_on` entries must be strings; got {dep!r}"
                )
            if dep not in services:
                raise ValueError(
                    f"compose service {name!r}: `depends_on: {dep!r}` references a "
                    f"service not declared in the compose file; declared services "
                    f"are {sorted(services)!r}."
                )


def _bind_mount_source(entry: Any) -> str | None:
    """Return the bind-mount source path from a compose volume entry, or
    ``None`` if the entry is a named volume / not a bind mount.
    """
    if isinstance(entry, str):
        # Short form: "SOURCE:TARGET[:MODE]". Bind if SOURCE contains a path separator
        # or starts with `.` / `/`; otherwise it's a named volume reference.
        source = entry.split(":", 1)[0]
        if source.startswith((".", "/")) or "/" in source:
            return source
        return None
    if isinstance(entry, dict):
        if entry.get("type") == "bind":
            source = entry.get("source")
            if isinstance(source, str):
                return source
    return None


def _check_runner_service_declared(services: dict[str, dict[str, Any]], runner: str) -> None:
    if runner not in services:
        raise ValueError(
            f"EnvironmentManifest.runner_service = {runner!r} is not declared in the "
            f"compose file; declared services are {sorted(services)!r}."
        )


def _check_initial_state_keys(
    services: dict[str, dict[str, Any]], initial_state: dict[str, InitialStateRef]
) -> None:
    for key in initial_state:
        if key not in services:
            raise ValueError(
                f"EnvironmentManifest.initial_state has key {key!r} that does not "
                f"match any declared service; compose declares {sorted(services)!r}."
            )


def _check_endpoint_services_declared(
    services: dict[str, dict[str, Any]],
    db_service: str | None,
    rag_service: str | None,
) -> None:
    for field_name, value in (("db_service", db_service), ("rag_service", rag_service)):
        if value is not None and value not in services:
            raise ValueError(
                f"EnvironmentManifest.{field_name} = {value!r} is not declared in the "
                f"compose file; declared services are {sorted(services)!r}."
            )


def _check_services_keys(
    compose_services: dict[str, dict[str, Any]],
    manifest_services: dict[str, ServiceSpec],
) -> None:
    for key in manifest_services:
        if key not in compose_services:
            raise ValueError(
                f"EnvironmentManifest.services has entry {key!r} that does not "
                f"match any declared compose service; compose declares "
                f"{sorted(compose_services)!r}."
            )


def _check_runner_not_restricted(
    manifest_services: dict[str, ServiceSpec], runner_service: str
) -> None:
    spec = manifest_services.get(runner_service)
    if spec is not None and spec.network_access == "restricted":
        raise ValueError(
            f"EnvironmentManifest.runner_service = {runner_service!r} cannot be "
            "marked network_access='restricted'; the runner needs its shared "
            "internal network attach to reach db-service / rag and its edge "
            "attach to reach control-plane. Restrict an untrusted sibling "
            "instead."
        )


def _check_runner_readiness_not_declared(
    manifest_services: dict[str, ServiceSpec], runner_service: str
) -> None:
    spec = manifest_services.get(runner_service)
    if spec is not None and spec.readiness is not None:
        raise ValueError(
            f"EnvironmentManifest.runner_service = {runner_service!r} cannot declare a "
            f"readiness contract; the runner is always gated by the built-in gRPC "
            f"readiness probe on its host port. Drop the readiness field from the runner "
            f"service, or declare it on a non-runner sibling instead."
        )


def _check_restricted_services_have_own_networks(
    compose_services: dict[str, dict[str, Any]],
    manifest_services: dict[str, ServiceSpec],
) -> None:
    for name, spec in manifest_services.items():
        if spec.network_access != "restricted":
            continue
        body = compose_services.get(name, {})
        networks = body.get("networks")
        if not isinstance(networks, (list, dict)) or not networks:
            raise ValueError(
                f"compose service {name!r} is marked "
                "network_access='restricted' but declares no compose "
                "`networks:` block; declare a task-owned network for it to "
                "attach to (e.g. `networks: [task_net]` with a top-level "
                "`networks: {task_net: {}}` entry)."
            )
        declared_names = set(networks) if isinstance(networks, dict) else set(networks)
        reserved = sorted(declared_names & HARNESS_RESERVED_NETWORKS)
        if reserved:
            raise ValueError(
                f"compose service {name!r} is marked "
                f"network_access='restricted' but declares harness-reserved "
                f"network(s) {reserved!r} in its `networks:` block; these "
                "names are owned by the network-policy transform and "
                "attaching to them would defeat the partitioning primitive. "
                "Declare a task-owned network instead (e.g. "
                "`networks: [task_net]` with a top-level "
                "`networks: {task_net: {}}` entry)."
            )


class StackPatch(BaseModel):
    """Substrate slot inside an :class:`EnvironmentPatch`.

    Groups the compose-file pointer with the runner-service name and
    the compose-input substitutions scoped to that specific file. All
    fields optional so a task patch can touch a single sub-field
    (``inputs``, ``runner_service``) without triggering the atomic
    ``stack`` replacement rule described in
    :func:`tolokaforge.core.project_loader.resolve`.
    """

    compose_file: Path | None = None
    """Path to the docker-compose file. Anchored to the file that
    declared it — the project directory for project patches, the task
    directory for task patches — by the loader before this patch is
    constructed."""

    runner_service: str | None = None
    """Name of the compose service that runs the agent. ``None`` in a
    patch means inherit; :func:`resolve` falls back to ``"default"``
    when the merged patch leaves this unset."""

    inputs: dict[str, str] = Field(default_factory=dict)
    """Compose-file variable substitutions scoped to ``compose_file``.
    Passed through to the runtime backend at compose-up time; the
    compose file's ``${var}`` slots resolve against this mapping."""

    runner_port: int | None = None
    """Runner gRPC container port. ``None`` in a patch means inherit;
    :func:`resolve` leaves the manifest's convention default in place
    when the merged patch leaves this unset."""

    db_service: str | None = None
    """Compose service backing the engine's db endpoint. ``None`` means
    inherit; :func:`resolve` leaves the manifest's convention default in
    place when the merged patch leaves this unset."""

    db_port: int | None = None
    """Container port for the db endpoint. ``None`` means inherit."""

    rag_service: str | None = None
    """Compose service backing the engine's rag endpoint. ``None`` means
    inherit the candidate-scan convention."""

    rag_port: int | None = None
    """Container port for the rag endpoint. ``None`` means inherit
    published-port auto-detect."""

    model_config = {"extra": "forbid"}


_HOSTNAME_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOSTNAME_RE = re.compile(rf"^{_HOSTNAME_LABEL}(?:\.{_HOSTNAME_LABEL})*$")


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _validate_allowlist_entry(entry: str) -> str:
    """Return the lowercased entry when it is a bare DNS hostname or a single
    leading ``*.`` wildcard domain; raise ``ValueError`` naming the entry
    otherwise.

    Rejects schemes, ports, paths, IP literals, multiple wildcards, and any
    label that is not DNS-valid — the allowlist maps onto squid ``dstdomain``
    ACLs, which match on host only.
    """
    if not entry:
        raise ValueError(
            "limited_internet_allowlist: entries must be non-empty hostnames; got an empty string"
        )
    lowered = entry.lower()
    if "://" in lowered:
        raise ValueError(
            f"limited_internet_allowlist entry {entry!r} must not carry a scheme; "
            "declare a bare hostname (e.g. 'api.openai.com' or '*.openai.com')"
        )
    if "/" in lowered:
        raise ValueError(
            f"limited_internet_allowlist entry {entry!r} must not carry a path; "
            "declare a bare hostname (e.g. 'api.openai.com' or '*.openai.com')"
        )
    if ":" in lowered:
        raise ValueError(
            f"limited_internet_allowlist entry {entry!r} must not carry a port; "
            "squid dstdomain matches on host only"
        )
    if lowered.count("*") > 1 or ("*" in lowered and not lowered.startswith("*.")):
        raise ValueError(
            f"limited_internet_allowlist entry {entry!r} may use at most one leading "
            "'*.' wildcard label (e.g. '*.openai.com')"
        )
    host = lowered[2:] if lowered.startswith("*.") else lowered
    if _is_ip_literal(host):
        raise ValueError(
            f"limited_internet_allowlist entry {entry!r} is an IP literal; "
            "the allowlist accepts DNS hostnames only"
        )
    if not _HOSTNAME_RE.match(host):
        raise ValueError(f"limited_internet_allowlist entry {entry!r} is not a valid DNS hostname")
    return lowered


class EnvironmentPatch(BaseModel):
    """Per-project or per-task environment declaration — an all-optional
    input shape that :func:`tolokaforge.core.project_loader.resolve`
    binds to an :class:`EnvironmentManifest`.

    Every field is optional so ``ProjectConfig.default_environment`` and
    ``TaskConfig.environment_manifest`` share the same type and compose
    on merge: a task that touches only ``stack.inputs`` inherits the
    rest from the project. Patches perform no filesystem I/O at
    construction time; the disk-touching validators live on the
    :class:`EnvironmentManifest` output type.
    """

    model_config = {"extra": "ignore"}

    stack: StackPatch | None = None
    """Substrate slot — see :class:`StackPatch`. A task patch that sets
    ``stack.compose_file`` replaces the project's whole ``stack``
    atomically (clean slate of ``inputs`` and ``runner_service``); a
    task patch that touches only ``stack.inputs`` /
    ``stack.runner_service`` deep-merges."""

    initial_state: dict[str, InitialStateRef] | None = None
    """Per-service fixture-copy operations. Discarded on atomic
    ``stack`` replacement (fixtures are scoped to the replaced
    substrate)."""

    network_policy: NetworkPolicy | None = None
    """Network posture the provisioner is asked to enforce. Survives
    atomic ``stack`` replacement (policy request, substrate-neutral)."""

    limited_internet_allowlist: list[str] | None = None
    """Hostnames egress is permitted to under ``network_policy:
    limited_internet``. Survives atomic ``stack`` replacement (policy
    request, substrate-neutral); the task list replaces the project list
    outright on merge. Per-entry syntax and the cross-field invariant are
    validated on :class:`EnvironmentManifest`, not here — the patch is a
    pure input shape."""

    security_context_defaults: SecurityContext | None = None
    """Security defaults applied to services that do not override them.
    Survives atomic ``stack`` replacement (policy request,
    substrate-neutral)."""

    services: dict[str, ServiceSpec] | None = None
    """Per-service isolation + reset declarations, keyed by compose
    service name. Discarded on atomic ``stack`` replacement — the
    project's per-service opt-outs reviewed the project's services,
    not the replacement stack. Deep-merges over the project side
    entry-by-entry on non-replacement paths."""

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_flat_stack_fields(cls, data: Any) -> Any:
        return coerce_flat_stack_fields(data)

    @model_validator(mode="before")
    @classmethod
    def _coerce_network_policy_case(cls, data: Any) -> Any:
        return coerce_network_policy_case(data)


class EnvironmentManifest(BaseModel):
    """Resolved per-trial environment — the output of
    :func:`tolokaforge.core.project_loader.resolve`.

    Produced by binding a project-side and task-side
    :class:`EnvironmentPatch` pair; consumers (runtime backends,
    orchestrator, docker stack materialiser) read it directly. The
    compose file is the source of truth for service topology (images,
    ports, volumes, health probes, depends_on, resources); this model
    adds the engine-specific fields the provisioner needs and runs
    safety validators against the compose contents at construction
    time.
    """

    compose_file: Path
    """Path to the docker-compose file. Always absolute — the resolver
    anchors it to the file that declared it before constructing the
    manifest."""

    runner_service: str = "default"
    """Which compose service is the agent runner. Must be declared in
    the compose file's ``services:`` mapping."""

    stack_inputs: dict[str, str] = Field(default_factory=dict)
    """Compose-file variable substitutions carried through from
    :class:`StackPatch.inputs`. Runtime backends pass these to
    ``docker compose`` as environment values so ``${var}`` slots in
    the compose file resolve at up-time."""

    initial_state: dict[str, InitialStateRef] = Field(default_factory=dict)
    """Fixture-copy operations, keyed by service name."""

    network_policy: NetworkPolicy = NetworkPolicy.NO_INTERNET
    """Network posture the provisioner is asked to enforce."""

    limited_internet_allowlist: list[str] = Field(default_factory=list)
    """Hostnames egress is permitted to under ``network_policy:
    limited_internet``. Each entry is a bare DNS hostname
    (``api.openai.com``) or a single leading ``*.`` wildcard domain
    (``*.openai.com``); entries are lowercase-normalised. Required
    (non-empty) under ``limited_internet`` and forbidden under the other
    policies — see :meth:`_check_allowlist_matches_policy`."""

    security_context_defaults: SecurityContext | None = None
    """Applied by the provisioner to every service that does not override
    the equivalent settings in the compose file."""

    services: dict[str, ServiceSpec] = Field(default_factory=dict)
    """Per-service isolation + reset declarations, keyed by compose
    service name. Populated by :func:`tolokaforge.core.project_loader.resolve`
    which merges the project-side and task-side patches and then fills
    any compose service missing from the merged map with an
    ``ephemeral`` default. Consumed by :attr:`requires_per_trial` (for
    backend selection) and by the runtime backends (for between-trial
    reset dispatch)."""

    runner_port: int = 50051
    """Runner gRPC container port. Always used — the runner always
    resolves. Overridable via ``stack.runner_port``."""

    db_service: str | None = None
    """Compose service backing the engine's db endpoint. ``None`` resolves
    by convention (``"db-service"``); a non-``None`` value names an exact
    service and is validated to exist in the compose file."""

    db_port: int | None = None
    """Container port for the db endpoint. ``None`` resolves by
    convention (8000)."""

    rag_service: str | None = None
    """Compose service backing the engine's rag endpoint. ``None`` resolves
    by candidate-scan; a non-``None`` value names an exact service and is
    validated to exist in the compose file."""

    rag_port: int | None = None
    """Container port for the rag endpoint. ``None`` auto-detects the first
    published port."""

    model_config = {"extra": "forbid"}

    @property
    def requires_per_trial(self) -> bool:
        """True iff at least one service is labelled ``reset`` or
        ``ephemeral``, or the manifest declares no services at all.

        Consumed by :meth:`Orchestrator._select_backend_from_tasks` to
        pick :class:`PerTrialRuntimeBackend` for any run whose tasks
        need per-trial substrate materialisation.
        """
        if not self.services:
            return True
        return any(spec.isolation != "shared" for spec in self.services.values())

    @property
    def restricted_services(self) -> frozenset[str]:
        """Names of services marked ``network_access="restricted"``.

        Substrate-facing single source of truth for the derivation:
        every runtime backend that enforces network topology reads this
        property and threads the set through to the compose
        materialisation transform, so restricted services skip the
        harness-injected shared internal network attach (and, under
        ``limited_internet``, the proxy-env injection).
        """
        return frozenset(
            name for name, spec in self.services.items() if spec.network_access == "restricted"
        )

    _compose_content: dict[str, Any] = PrivateAttr(default_factory=dict)
    """Parsed compose file contents cached at construction time. Populated
    by the model_validator; returned verbatim from :meth:`load_compose`."""

    @field_validator("limited_internet_allowlist")
    @classmethod
    def _validate_allowlist(cls, value: list[str]) -> list[str]:
        normalised = [_validate_allowlist_entry(entry) for entry in value]
        duplicates = sorted({entry for entry in normalised if normalised.count(entry) > 1})
        if duplicates:
            raise ValueError(f"limited_internet_allowlist contains duplicate entries: {duplicates}")
        return normalised

    @model_validator(mode="after")
    def _check_allowlist_matches_policy(self) -> EnvironmentManifest:
        if self.network_policy is NetworkPolicy.LIMITED_INTERNET:
            if not self.limited_internet_allowlist:
                raise ValueError(
                    "network_policy 'limited_internet' requires a non-empty "
                    "limited_internet_allowlist; declare the hostnames egress is "
                    "permitted to."
                )
            return self
        if self.limited_internet_allowlist:
            raise ValueError(
                f"limited_internet_allowlist is only valid under network_policy "
                f"'limited_internet'; got policy '{self.network_policy.value}' with "
                "a non-empty allowlist."
            )
        return self

    @model_validator(mode="after")
    def _load_and_validate_compose(self) -> EnvironmentManifest:
        if not self.compose_file.is_file():
            raise ValueError(
                f"EnvironmentManifest.compose_file does not exist or is not a "
                f"file: {self.compose_file}"
            )
        with self.compose_file.open() as f:
            content = yaml.safe_load(f)
        if not isinstance(content, dict):
            raise ValueError(
                f"EnvironmentManifest.compose_file must be a YAML mapping; got "
                f"{type(content).__name__}"
            )
        services = _compose_services(content)
        _check_no_host_network(services)
        _check_no_privileged(services)
        _check_no_cap_add(services)
        _check_safe_bind_mounts(services)
        _check_pinned_images(services)
        _check_depends_on_resolves(services)
        _check_runner_service_declared(services, self.runner_service)
        _check_initial_state_keys(services, self.initial_state)
        _check_services_keys(services, self.services)
        _check_runner_not_restricted(self.services, self.runner_service)
        _check_runner_readiness_not_declared(self.services, self.runner_service)
        _check_restricted_services_have_own_networks(services, self.services)
        _check_endpoint_services_declared(services, self.db_service, self.rag_service)
        self._compose_content = content
        return self

    def load_compose(self) -> dict[str, Any]:
        """Return the parsed compose file cached at construction time.

        The manifest snapshots the compose file when the validator runs;
        callers get the same content the safety validators inspected. Later
        edits to the compose file on disk are not reflected."""
        return self._compose_content


# =============================================================================
# Main TaskDescription (from TASK_DESCRIPTION_SCHEMA.md)
# =============================================================================


class TaskDescription(BaseModel):
    """
    Complete serializable task description.

    Produced by the Loader (host) from adapter-specific formats.
    Consumed by the Runtime (runner container) for execution and grading.
    """

    # --- Identity ---
    task_id: str
    name: str
    category: str  # Domain: "airline", "retail"
    description: str  # Task description / user goal
    # Open/extensible: the adapter name from the registry (entry-point or built-in).
    # Not constrained to AdapterType so third-party adapters round-trip with no engine
    # edit; see AdapterType for the well-known built-in names.
    adapter_type: str
    schema_version: str = "1.0.0"

    # --- System Prompt ---
    system_prompt: str  # Full content, not file path

    # --- Tools ---
    agent_tools: list[ToolSchema] = Field(default_factory=list)
    user_tools: list[ToolSchema] = Field(default_factory=list)  # User-side device tools

    # --- State ---
    initial_state: RunnerInitialStateConfig = Field(default_factory=RunnerInitialStateConfig)
    initialization_actions: list[RunnerInitializationAction] = Field(default_factory=list)

    # --- User Simulator ---
    user_simulator: RunnerUserSimulatorConfig = Field(default_factory=RunnerUserSimulatorConfig)

    # --- Search ---
    search: SearchConfig = Field(default_factory=SearchConfig)

    # --- Grading ---
    grading: RunnerGradingConfig = Field(default_factory=RunnerGradingConfig)

    # --- Metadata ---
    source_files: dict[str, str] = Field(default_factory=dict)  # For debugging
    generated_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)  # Adapter-specific extras

    # Bundled tool artifacts — Python files needed for tool reconstruction.
    # Keys are relative paths (e.g., "mcp_core/__init__.py"), values are
    # base64-encoded file contents. The Runner extracts these to a temp
    # directory and adds it to sys.path before reconstructing tools.
    # This enables tool execution in Docker without host filesystem mounts.
    tool_artifacts: dict[str, str] = Field(
        default_factory=dict,
        description="Base64-encoded Python files for tool reconstruction. "
        "Keys are relative paths, values are base64 content.",
    )

    # --- Environment ---
    environment_manifest: EnvironmentManifest | None = None
    """Typed declaration of the task's multi-service environment. ``None``
    means the task does not declare one."""

    model_config = {"extra": "forbid"}


# =============================================================================
# Recorded Tool Call (for transcript grading)
# =============================================================================


class ToolExecutorIdentity(str, Enum):
    """Which side of the dialogue made a tool call.

    ``USER`` is unreachable in every run today because no code path constructs
    a user-side tool executor (#688) — equally on both substrates.
    """

    AGENT = "agent"
    USER = "user"


class RecordedToolCall(BaseModel):
    """One tool call as the trial recorded it.

    The single recorded-tool-call type on both grading substrates. Produced
    once per call, in true execution order across every executor, by the
    trial's :class:`ToolCallRecorder`.
    """

    # The provider's tool-call id, carried on ExecuteToolRequest. Two calls to
    # the same tool with identical arguments differ only here and in ``sequence``.
    call_id: str = Field(min_length=1)
    # Trial-wide, 0-based, stamped by the recorder at append time.
    sequence: int
    tool_name: str
    arguments: dict[str, Any]
    executor: ToolExecutorIdentity
    status: ToolExecutionStatus
    # Untruncated. On a failed call this is the failure text the executing layer
    # produced, which is not the text the ``role: tool`` message carries.
    output: str
    # Wall time measured by the recording caller around the call.
    latency_seconds: float
    timestamp: datetime

    model_config = {"extra": "forbid"}


class ToolCallRecorder(Protocol):
    """The trial's ordered tool-call record.

    One recorder per trial, shared by every executor, so ``sequence`` is
    execution order across executors rather than position within one of them.
    Implementations stamp ``sequence`` themselves — a caller cannot supply a
    wrong index.
    """

    def record(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        executor: ToolExecutorIdentity,
        status: ToolExecutionStatus,
        output: str,
        latency_seconds: float,
    ) -> None: ...

    @property
    def recorded(self) -> tuple[RecordedToolCall, ...]: ...


# =============================================================================
# Reconstructed Tools Container
# =============================================================================


class ReconstructedTools(BaseModel):
    """Container for reconstructed tools (non-serializable callables stored separately)."""

    agent_tool_names: list[str] = Field(default_factory=list)
    user_tool_names: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


# =============================================================================
# DB Client Response Models
# =============================================================================


class InitTrialResponse(BaseModel):
    """Response from DB Service init_trial endpoint."""

    status: str
    trial_id: str
    tables_initialized: list[str]
    schemas_registered: int
    unstable_fields_registered: int
    initial_hash: str

    model_config = {"extra": "allow"}


class StateResponse(BaseModel):
    """Response from DB Service get_state endpoint."""

    data: dict[str, list[dict[str, Any]]]
    version: int
    full_hash: str
    stable_hash: str

    model_config = {"extra": "allow"}


class StableStateResponse(BaseModel):
    """Response from DB Service get_stable_state endpoint."""

    data: dict[str, list[dict[str, Any]]]
    version: int
    stable_hash: str
    filtered_fields: list[dict[str, str]]

    model_config = {"extra": "allow"}


class HashResponse(BaseModel):
    """Response from DB Service get_state_hash endpoint."""

    stable_hash: str
    full_hash: str
    version: int

    model_config = {"extra": "allow"}


class MutateResponse(BaseModel):
    """Response from DB Service mutate endpoint."""

    status: str
    version: int
    affected_rows: int
    new_hash: str

    model_config = {"extra": "allow"}


class SnapshotResponse(BaseModel):
    """Response from DB Service create_snapshot endpoint."""

    status: str
    snapshot_name: str
    version: int
    hash: str

    model_config = {"extra": "allow"}


class RestoreSnapshotResponse(BaseModel):
    """Response from DB Service restore_snapshot endpoint."""

    status: str
    restored_from: str
    version: int
    hash: str

    model_config = {"extra": "allow"}


class ResetTrialResponse(BaseModel):
    """Response from DB Service reset_trial endpoint."""

    status: str
    version: int
    hash: str

    model_config = {"extra": "allow"}


class DeleteTrialResponse(BaseModel):
    """Response from DB Service delete_trial endpoint."""

    status: str
    deleted: dict[str, Any]

    model_config = {"extra": "allow"}


class QueryResponse(BaseModel):
    """Response from DB Service query endpoint."""

    results: list[Any]
    count: int

    model_config = {"extra": "allow"}


class SchemaResponse(BaseModel):
    """Response from DB Service get_schema endpoint."""

    schemas: dict[str, dict[str, Any]]
    unstable_fields: list[dict[str, Any]]

    model_config = {"extra": "allow"}


class HealthCheckResponse(BaseModel):
    """Response from DB Service health_check endpoint."""

    status: str
    version: str
    active_trials: int

    model_config = {"extra": "allow"}


# =============================================================================
# Grading Result Models
# =============================================================================


class TableDiff(BaseModel):
    """Diff for a single table."""

    missing: list[dict[str, Any]] = Field(default_factory=list)
    extra: list[dict[str, Any]] = Field(default_factory=list)
    different: list[dict[str, Any]] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


class StateDiff(BaseModel):
    """Human-readable diff between two stable states."""

    tables: dict[str, TableDiff] = Field(default_factory=dict)
    summary: str = ""

    model_config = {"extra": "forbid"}

    @property
    def identical(self) -> bool:
        """Check if states are identical (no differences)."""
        for table_diff in self.tables.values():
            if table_diff.missing or table_diff.extra or table_diff.different:
                return False
        return True


class TranscriptRuleResult(BaseModel):
    """Result of evaluating a single transcript rule."""

    rule_type: str
    rule: dict[str, Any]
    passed: bool
    message: str

    model_config = {"extra": "forbid"}


class KeyAccounting(str, Enum):
    """What a recording site in the grading path did with its author key."""

    EVALUATED = "evaluated"
    SKIPPED = "skipped"


class KeyAccountingRecord(BaseModel):
    """One recording site's outcome for one author-facing ``grading.yaml`` key.

    A skip renders into ``Grade.reasons`` as ``<author_key> skipped: <detail>``,
    so ``detail`` is what a task author reads to learn why a key they populated
    contributed nothing.
    """

    outcome: KeyAccounting
    detail: str = ""

    model_config = {"extra": "forbid", "frozen": True}

    @model_validator(mode="after")
    def _skip_states_a_reason(self) -> KeyAccountingRecord:
        if self.outcome is KeyAccounting.SKIPPED and not self.detail.strip():
            raise ValueError("a skipped key must carry the detail rendered into Grade.reasons")
        return self


class TranscriptEvaluationResult(BaseModel):
    """Result of evaluating all transcript rules.

    ``accounted_keys`` names the author-facing ``transcript_rules.*`` keys this
    evaluation decomposed. The runtime ledger (``runner/grading_ledger.py``) reads
    it rather than re-deriving which fields the evaluator "should have" branched
    on, so a populated key the evaluator never decomposes stays unaccounted.
    """

    # Use 'passed' as the field name (not 'pass' which is a Python keyword)
    passed: bool = False
    score: float = 0.0
    details: list[TranscriptRuleResult] = Field(default_factory=list)
    accounted_keys: dict[str, KeyAccountingRecord] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class TraceConstraintResult(BaseModel):
    """The verdict one declared trace constraint reached.

    ``message`` is empty on a pass and names what went wrong otherwise — an
    unmatched anchor, a failed condition, or the evidence the trial does not carry.
    ``matched_positions`` holds timeline positions rather than events, so a grade
    stays readable and serialisable; an event is looked up by position against
    ``trajectory.yaml``.
    """

    id: str = Field(min_length=1)
    kind: TraceConstraintKind
    passed: bool
    weight: float
    message: str = ""
    matched_positions: list[int] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


class TraceChecksResult(BaseModel):
    """What evaluating a pack's ``trace_checks`` block produced.

    ``accounted_keys`` names the author-facing ``trace_checks.*`` keys the
    evaluation decomposed, so the runtime ledger reads what was evaluated rather
    than re-deriving it — the ``TranscriptEvaluationResult`` contract.

    A result with no ``constraints`` is the trial that left no trace of itself: the
    component is not scored there and the caller records the skip.
    """

    passed: bool = False
    score: float = 0.0
    constraints: list[TraceConstraintResult] = Field(default_factory=list)
    accounted_keys: dict[str, KeyAccountingRecord] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class RunnerGradeComponents(BaseModel):
    """Component scores for grading."""

    hash_match: bool | None = None
    hash_score: float = -1.0  # -1.0 means not evaluated
    jsonpath_score: float = -1.0  # -1.0 means not evaluated
    jsonpath_reasons: str = ""
    db_probe_score: float = -1.0  # -1.0 means not evaluated
    db_probe_reasons: str = ""
    transcript_pass: bool | None = None
    transcript_score: float = -1.0
    trace_checks_score: float = -1.0  # -1.0 means not evaluated
    llm_judge_score: float = -1.0  # -1.0 means not evaluated
    llm_judge_reasons: str = ""
    custom_checks_score: float = -1.0  # -1.0 means not evaluated

    model_config = {"extra": "forbid"}


class HashGradingResult(BaseModel):
    """Result of hash-based grading."""

    hash_match: bool
    hash_score: float
    state_diff: StateDiff | None = None
    golden_action_errors: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}
