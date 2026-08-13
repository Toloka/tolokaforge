"""TaskConfig + task-scoped grading config + project umbrella.

Everything the ``task.yaml`` / ``grading.yaml`` / ``project.yaml`` layer
declares. Grouped here because the four layers form one authoring
surface — ``TaskConfig`` composes ``GradingConfig`` via a sibling file,
and both inherit :class:`TaskDefaults` from ``project.yaml``.
"""

from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator

from tolokaforge.core.deprecations import canonicalize_actor_config, drop_retired_max_idle_turns
from tolokaforge.core.grading.combine_method import CombineMethod, validate_combine_method
from tolokaforge.core.grading.id_fields_declaration import validate_id_fields_declaration
from tolokaforge.core.grading.state_composition import (
    AUTHORED_HASH_WEIGHT_CONTEXT,
    StateHashConfig,
    refuse_probes_beside_another_state_source,
    resolve_hash_weight,
)
from tolokaforge.core.models.run_config import RunDefaults
from tolokaforge.runner.models import (
    EnvironmentPatch,
    JudgeCustomization,
    LLMJudgeConfig,
    TraceChecksConfig,
    TranscriptRulesConfig,
)

__all__ = [
    "ActorSpec",
    "AssetsConfig",
    "GradingCombineConfig",
    "GradingConfig",
    "GradingDefaults",
    "InitializationAction",
    "InitialStateConfig",
    "InteractionMode",
    "LLMJudgeDefaults",
    "ProjectConfig",
    "RETIRED_STATE_CHECK_KEYS",
    "SEED_KIND_BY_EXTENSION",
    "SeedKind",
    "SeedRef",
    "StateChecksConfig",
    "StuckHeuristicsDefaults",
    "TaskConfig",
    "TaskDefaults",
    "TaskDiscoveryConfig",
    "TaskInventoryConfig",
    "TaskMetadata",
    "TimeoutDefaults",
    "ToolsConfig",
    "UserSimulatorConfig",
]


InteractionMode = Literal["conversational", "agent_only"]
"""Shape of the trial's turn loop — whether a user party participates.

- ``conversational`` (default): user simulator dispatched every turn.
  Matches τ-bench-style benchmarks where the user is a genuine
  information source.
- ``agent_only``: no user turn dispatched after the first message.
  The agent runs until it takes a turn without calling a tool (routed
  to :attr:`TerminationReason.AGENT_DONE`), or to ``max_turns`` or
  ``episode_timeout_s``. Matches code-migration / agent-driven eval
  shape where the task lives entirely in the system prompt and the
  agent decides when it's done. The user simulator is never
  constructed on this route.

Future values (e.g. ``multi_actor``) will land alongside dedicated
:class:`TurnPolicy` implementations registered in the
``tolokaforge.turn_policies`` entry-point group.
"""


class InitializationAction(BaseModel):
    """One-time environment mutation executed before a trial starts."""

    model_config = {"extra": "ignore"}

    env_type: Literal["assistant", "user"]
    func_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class InitialStateConfig(BaseModel):
    """Initial environment state configuration"""

    model_config = {"extra": "ignore"}

    json_db: str | dict[str, Any] | None = None  # JSON DB initial state
    device_overrides: dict[str, Any] | None = None  # Per-task device state overrides
    filesystem: dict[str, Any] | None = None
    mock_web: dict[str, Any] | None = None
    rag: dict[str, Any] | None = None
    system_prompt: str | None = None  # Path to system prompt file (e.g., wiki.md)
    initialization_actions: list[InitializationAction] | None = None


class ToolsConfig(BaseModel):
    """Tools configuration for task"""

    model_config = {"extra": "ignore"}

    agent: dict[str, Any] = Field(default_factory=lambda: {"enabled": []})
    user: dict[str, Any] = Field(default_factory=lambda: {"enabled": []})


def _refuse_first_message(data: Any) -> Any:
    """Reject an opener declared on the user actor instead of on the task.

    ``extra="ignore"`` would drop the key, and the nested position keeps it
    out of :func:`construct_config`'s unknown-key warning — so an author would
    see neither their opener delivered nor any complaint. Both declared mirrors
    of the user actor run this, so every spelling reaches it: ``actors.user``,
    the legacy top-level ``user_simulator`` block, a project's
    ``task_defaults`` actors map, and a direct-Python
    ``UserSimulatorConfig(...)``.
    """
    if isinstance(data, dict) and "first_message" in data:
        raise ValueError(
            "first_message is not a field on the user actor. A task's opening turn is declared "
            "task-level as initial_user_message, whose text is delivered verbatim as "
            "the first user message — move the value there."
        )
    return data


class UserSimulatorConfig(BaseModel):
    """User simulator configuration"""

    model_config = {"extra": "ignore"}

    mode: Literal["scripted", "llm"] = "llm"
    persona: str = "cooperative"
    backstory: str | None = None  # User instruction for tau-bench parity
    scripted_flow: list[dict[str, str]] | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_first_message(cls, data: Any) -> Any:
        return _refuse_first_message(data)


_RESERVED_ACTOR_NAMES = frozenset({"agent", "judge"})
"""Actor names reserved for future actors. ``actors.user`` is the
counterpart actor today; ``actors.agent`` / ``actors.judge`` are
reserved so a pack cannot use the names for something else in the
meantime."""


class ActorSpec(BaseModel):
    """Single entry inside the ``actors`` map.

    ``actors.user`` configures the user simulator; its fields mirror
    :class:`UserSimulatorConfig`. Any field left unset resolves to the
    simulator default (``mode=llm``, ``persona=cooperative``) via
    :meth:`TaskConfig.resolve_user_simulator`. Sub-keys ``tools`` and
    ``service`` are reserved by the design for future actor types; a
    future strict flip will reject them, but today they surface as a
    ``DeprecationWarning`` via :func:`construct_config` (matching the
    broader Project-layer warn-on-unknown policy).
    """

    mode: Literal["scripted", "llm"] | None = None
    persona: str | None = None
    backstory: str | None = None
    scripted_flow: list[dict[str, str]] | None = None

    model_config = {"extra": "ignore"}

    @model_validator(mode="before")
    @classmethod
    def _reject_first_message(cls, data: Any) -> Any:
        return _refuse_first_message(data)


def _validate_actors_map(
    value: dict[str, ActorSpec] | None,
) -> dict[str, ActorSpec] | None:
    """Reject reserved actor names on any ``actors`` map."""
    if value is None:
        return value
    reserved = _RESERVED_ACTOR_NAMES & set(value)
    if reserved:
        raise ValueError(
            f"actors: name(s) {sorted(reserved)!r} are reserved for future "
            "actors and cannot be declared today."
        )
    return value


_A_USER_TOOL_NOTHING_CAN_CALL = (
    "tools.user.enabled declares {tools}, and {because}, so the declared tools are "
    "registered for every trial and no turn can ever call one. {remedy}, or drop "
    "tools.user.enabled."
)

_AGENT_ONLY_DISPATCHES_NO_USER_TURN = (
    "interaction_mode is agent_only, which dispatches no user turn at all"
)
_TO_DISPATCH_A_USER_TURN = "Write interaction_mode: conversational"

_A_SCRIPTED_SIMULATOR_EMITS_NO_TOOL_CALL = (
    "the user simulator resolves to mode scripted, whose reply is text and never a tool call"
)
_TO_LET_THE_SIMULATOR_CALL = "Write actors.user.mode: llm"

_A_SECOND_MCP_SERVER = (
    "tools.user.mcp_server is {user_server} and tools.agent.mcp_server is {agent_server}. "
    "A task ships one MCP server: every block's schemas are read from the one "
    "fixtures/tools.json beside the task, so the second server's tools would be "
    "resolved from the first server's fixture — the simulator would be offered tools "
    "that do not exist, and grading rules naming them would be checked against "
    "arguments that are not theirs. Point both blocks at one server, or declare the "
    "user's tools as builtins."
)


def _why_no_user_turn_can_call_a_tool(task: "TaskConfig") -> tuple[str, str] | None:
    """Why no turn of *task* can make a user-side call, and the fix, or ``None``.

    Ordered outward-in: the interaction mode decides whether a user turn is
    dispatched at all, and only then does the simulator's own mode decide what a
    dispatched turn can emit.
    """
    if task.interaction_mode == "agent_only":
        return _AGENT_ONLY_DISPATCHES_NO_USER_TURN, _TO_DISPATCH_A_USER_TURN
    if task.resolve_user_simulator().mode == "scripted":
        return _A_SCRIPTED_SIMULATOR_EMITS_NO_TOOL_CALL, _TO_LET_THE_SIMULATOR_CALL
    return None


class TaskMetadata(BaseModel):
    """Optional metadata used for analytics slicing."""

    model_config = {"extra": "ignore"}

    complexity: str | None = None
    expected_failure_modes: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class TimeoutDefaults(BaseModel):
    """Task-shape timeouts applied to every task via ``task_defaults``.
    Consumed at task scope; the run-side ``OrchestratorConfig.timeouts``
    is a run-level cap that clamps these via the min rule at read
    time."""

    model_config = {"extra": "ignore"}

    trial_seconds: int = Field(default=600, ge=1)
    tool_call_seconds: int = Field(default=60, ge=1)


class StuckHeuristicsDefaults(BaseModel):
    """Task-shape stuck-detection knobs applied to every task via
    ``task_defaults``. The canonical home for stuck-heuristic config;
    the deprecated ``OrchestratorConfig.stuck_heuristics`` is what the
    conductor falls back to for a task declaring no block of its own."""

    model_config = {"extra": "ignore"}

    enabled: bool = True
    max_repeated_tool_calls: int = Field(default=5, ge=1)

    @model_validator(mode="before")
    @classmethod
    def _drop_retired_keys(cls, data: Any) -> Any:
        return drop_retired_max_idle_turns(data)


def _lift_user_simulator_kwarg(data: Any) -> Any:
    """Direct-Python shim: accept a legacy ``user_simulator=...`` kwarg and
    lift it into ``actors["user"]`` with a ``DeprecationWarning``.

    The YAML load path already runs :func:`canonicalize_actor_config` per
    layer pre-merge; this covers the case where a caller constructs
    ``TaskConfig(user_simulator=UserSimulatorConfig(...))`` directly in
    Python. A ``UserSimulatorConfig`` instance is model-dumped so Pydantic
    can re-parse the fields as :class:`ActorSpec`.
    """
    if not isinstance(data, dict) or "user_simulator" not in data:
        return data
    value = data["user_simulator"]
    if value is None:
        data.pop("user_simulator")
        return data
    if isinstance(value, BaseModel):
        value = value.model_dump(exclude_unset=True)
    data_for_coerce = {**data, "user_simulator": value}
    return canonicalize_actor_config(data_for_coerce)


class TaskConfig(BaseModel):
    """Task specification"""

    model_config = {"extra": "ignore"}

    task_id: str
    name: str | None = None
    """Display name. Optional — falls back to ``task_id`` in the adapter."""

    category: str | None = None
    """Legacy grouping label. Optional — project-level association
    replaces the informational role this served for reporting."""

    description: str
    adapter_type: str = "native"  # Adapter runtime type (native, tlk_mcp_core, tau, …)
    max_turns: int | None = None  # Optional per-task turn cap override
    initial_user_message: str | None = None
    """The task's pinned opener. When set, this exact text — whitespace
    included — is message index 0, and no simulator dispatch produces the
    opening turn. Unset, the conversational shape has the user simulator
    generate turn 1, while the agent-only shape fails at bootstrap: it has
    no simulator to synthesise a seed from."""

    interaction_mode: InteractionMode = "conversational"
    """Turn-loop shape. ``conversational`` (default) dispatches the user
    simulator every turn — backward-compatible with every existing pack.
    ``agent_only`` skips user-turn dispatch entirely; the agent runs to its
    first tool-call-free turn, ``max_turns`` or ``episode_timeout_s``. Selects a
    concrete :class:`TurnPolicy` via the ``tolokaforge.turn_policies``
    entry-point registry (see ADR-0027)."""
    initial_state: InitialStateConfig = Field(default_factory=InitialStateConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    actors: dict[str, ActorSpec] | None = None
    """Named actor map. ``actors.user`` configures the user simulator
    (:meth:`resolve_user_simulator`); the loader lifts a legacy top-level
    ``user_simulator`` block into it. Reserved actor names (``agent``,
    ``judge``) are rejected at load time — same rule as
    :class:`TaskDefaults`."""
    metadata: TaskMetadata = Field(default_factory=TaskMetadata)
    policies: dict[str, Any] = Field(
        default_factory=dict
    )  # Can contain guidance list or agent_system_prompt string
    grading: str | None = None  # Path to grading.yaml; sibling grading.yaml auto-picked when unset
    system_prompt: str | None = None  # Path to system prompt file (e.g., wiki.md)
    adapter_settings: dict[str, Any] | None = None  # Opaque dict parsed by each adapter type

    stuck_heuristics: StuckHeuristicsDefaults | None = None
    """Task-scope stuck-detection knobs. Populated by the M2 loader
    merge chain when ``project.task_defaults.stuck_heuristics`` is set;
    the task's own ``task.yaml`` overrides win on conflict. The
    conductor reads from here when it is set and from the deprecated
    ``OrchestratorConfig.stuck_heuristics`` when it is not."""

    timeouts: TimeoutDefaults | None = None
    """Task-scope timeouts. Populated by the M2 loader merge chain;
    the runtime clamps effective timeouts to
    ``min(task, orchestrator)`` — the orchestrator side is a run-level
    cap, this side is authoritative."""

    environment_manifest: EnvironmentPatch | None = None
    """Per-trial substrate declaration (ADR-0009), authored as a patch.

    The task's patch composes with the project's ``default_environment``
    at load time via :func:`tolokaforge.core.project_loader.resolve`,
    which produces the concrete :class:`EnvironmentManifest` the adapter
    forwards onto ``TaskDescription``. Left ``None`` for tasks that
    inherit the project default (or run on the shared stack when the
    project sets no default either). ``stack.compose_file`` is
    task-relative and anchored by the loader before construction."""

    _source_dir: Path | None = PrivateAttr(default=None)
    """On-disk pack directory this config was loaded from, or ``None`` for a
    hand-built in-memory config. An in-process locator, not a schema field:
    absent from ``model_dump()`` / ``model_json_schema()`` / the YAML
    round-trip. Stamped by :func:`tolokaforge.adapters._task_loader.load_task_yaml`
    and read via :attr:`source_dir` so a pre-loaded task resolves its assets
    without re-globbing the filesystem."""

    @property
    def source_dir(self) -> Path | None:
        """Directory this task was loaded from (see :attr:`_source_dir`)."""
        return self._source_dir

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_user_simulator_kwarg(cls, data: Any) -> Any:
        """Direct-Python compat shim: lift a legacy ``user_simulator=...``
        constructor kwarg into ``actors["user"]`` with a
        ``DeprecationWarning``. YAML loads already handle this via
        :func:`canonicalize_actor_config` per layer pre-merge; this covers
        the ``TaskConfig(user_simulator=...)`` direct-construction case."""
        return _lift_user_simulator_kwarg(data)

    @field_validator("actors")
    @classmethod
    def _reject_reserved_actor_names(
        cls, value: dict[str, ActorSpec] | None
    ) -> dict[str, ActorSpec] | None:
        return _validate_actors_map(value)

    @model_validator(mode="after")
    def _refuse_blank_initial_user_message(self) -> Self:
        """Reject a declared opener that carries no text.

        The one state the turn loop cannot honour: there is nothing to deliver
        verbatim, and the only alternative — generating turn 1 anyway — is the
        opposite of what declaring the key asks for.
        """
        if self.initial_user_message is None or self.initial_user_message.strip():
            return self
        raise ValueError(
            f"Task '{self.task_id}' declares initial_user_message with no text "
            f"({self.initial_user_message!r}). The value is delivered verbatim as the "
            "first user message, so a blank one has nothing to send. Either give it "
            "text, or leave it unset — omit the key in task.yaml, or pass None from an "
            "adapter's get_task() — to have the user simulator open the conversation."
        )

    @model_validator(mode="after")
    def _refuse_user_tools_no_turn_can_call(self) -> Self:
        """Refuse a ``tools.user.enabled`` no user turn of this task can ever call.

        Nothing downstream fails on such a pack: the tools are registered for the
        trial like any other, so a ``requestor: user`` action or an ``executor: user``
        matcher grades against a call that could not have happened, on every trial.
        The refusal is here because the three keys that decide it — the tool block,
        the interaction mode and the simulator's mode — are all in ``task.yaml``.
        """
        declared = self.tools.user.get("enabled")
        if not declared:
            return self
        reason = _why_no_user_turn_can_call_a_tool(self)
        if reason is None:
            return self
        because, remedy = reason
        raise ValueError(
            _A_USER_TOOL_NOTHING_CAN_CALL.format(
                tools=sorted(declared), because=because, remedy=remedy
            )
        )

    @model_validator(mode="after")
    def _refuse_a_second_mcp_server(self) -> Self:
        """One task, one MCP server: the fixture that answers for it is per-task.

        Schemas for an ``mcp_server`` block come from ``<task_dir>/fixtures/tools.json``,
        which is keyed on the task and not on the server, so a second server resolves
        against the first one's fixture rather than its own. A user block naming the
        agent's server is fine, and so is a user-only server: the ambiguity needs two
        different names.
        """
        user_server = self.tools.user.get("mcp_server")
        agent_server = self.tools.agent.get("mcp_server")
        if user_server and agent_server and user_server != agent_server:
            raise ValueError(
                _A_SECOND_MCP_SERVER.format(user_server=user_server, agent_server=agent_server)
            )
        return self

    def resolve_user_simulator(self) -> UserSimulatorConfig:
        """Return the effective user-simulator config from ``actors.user``.

        Unset actor fields resolve to the simulator defaults (``mode=llm``,
        ``persona=cooperative``). A task with no ``actors.user`` gets the
        default simulator.
        """
        spec = (self.actors or {}).get("user")
        if spec is None:
            return UserSimulatorConfig()
        return UserSimulatorConfig(
            mode=spec.mode or "llm",
            persona=spec.persona or "cooperative",
            backstory=spec.backstory,
            scripted_flow=spec.scripted_flow,
        )


RETIRED_STATE_CHECK_KEYS: frozenset[str] = frozenset({"env_assertions", "db_hash_check"})
"""The ``state_checks`` keys that are neither declared fields nor unknown keys.

Read by :class:`StateChecksConfig`'s own before-validator and by the authoring gate's
unknown-key refusal, so the two cannot disagree about which keys a recorded bundle is
allowed to carry.
"""


class StateChecksConfig(BaseModel):
    """State checks configuration.

    ``extra="forbid"`` because a dropped key leaves this component with no source and
    the fold renormalises around it: a ``jsonpaths`` typo beside a weighted sibling
    graded a trial ``1.0`` and passing where the assertion the author wrote scored
    ``0.5`` and failing. The two keys in :data:`RETIRED_STATE_CHECK_KEYS` are the one
    exception — :meth:`_reject_removed_state_check_keys` drops them, so the extra check
    never sees them.
    """

    model_config = {"extra": "forbid"}

    jsonpaths: list[dict[str, Any]] = Field(default_factory=list)
    hash: StateHashConfig | None = None
    db_probes: list[dict[str, Any]] = Field(default_factory=list)
    # Opt-in, per-field: record field names whose numeric-looking STRING values
    # fold ("130.00" == "130.0") when hashing state. Mirrors the runner-side
    # StateChecksConfig so the same grading.yaml key behaves identically on the
    # core GradingEngine path (to_hashable) and the runner path
    # (compute_stable_hash). See core/hash.py compute_stable_hash.
    numeric_string_fields: list[str] = Field(default_factory=list)
    # Opt-in, per-table: primary key for a table whose key is not the literal "id" —
    # one field name, or an ordered list of component names for a composite key
    # (e.g. {"widgets": "widget_id", "positions": ["account_id", "symbol"]}). A table
    # absent from the map resolves to "id", so id-keyed domains need nothing here.
    # Threaded to the runner DB proxy so upsert/delete/lookup key resolution is
    # config-driven rather than introspecting model source (which breaks when the
    # domain source is not on disk).
    id_fields: dict[str, str | list[str]] = Field(default_factory=dict)
    # Escape hatch for legacy tasks: downgrade the id_fields cross-check (id_fields
    # keys must appear in initial_state.tables) to a warning at every gate that runs it.
    # New tasks should fix typos or add the table, not enable this.
    relaxed_validation: bool = False

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_state_check_keys(cls, data: Any) -> Any:
        """Fail loud with a migration message on the removed state-check keys.

        ``env_assertions`` and ``db_hash_check`` never produced grading signal on
        either substrate, so a populated one is an error naming its replacement rather
        than a key this model quietly drops.

        An inert declaration (``env_assertions: []`` / ``db_hash_check: false``)
        requests nothing and is dropped here, so recorded trial bundles serialized
        against the old schema still load past this model's ``extra="forbid"`` —
        returning them untouched would not, since the extra check reads whatever this
        validator hands back. Every other undeclared key is that check's business.
        """
        if not isinstance(data, dict):
            return data
        if data.get("env_assertions"):
            raise ValueError(
                "state_checks.env_assertions has been removed — it never produced "
                "grading signal on either substrate. Declare the source that matches "
                "what you are asserting, and pick one: db_probes is exclusive with the "
                "other two, and jsonpaths beside a hash source needs a hash.weight to "
                "fold them by. Each block below loads on its own:\n"
                "  # state_checks.jsonpaths — per-record state assertions\n"
                "  state_checks:\n"
                "    jsonpaths:\n"
                "      - path: $.db.orders[0].status\n"
                "        equals: shipped\n"
                "  — or —\n"
                "  # state_checks.hash — whole-state comparison\n"
                "  state_checks:\n"
                "    hash:\n"
                "      enabled: true\n"
                "      golden_actions: [...]        # or expect_initial_state: true\n"
                "  — or —\n"
                "  # state_checks.db_probes — substrate SQL assertions\n"
                "  state_checks:\n"
                "    db_probes:\n"
                "      - name: order_shipped\n"
                "        dsn: postgresql://...\n"
                "        query: SELECT status FROM orders WHERE id = 1\n"
                "        expect:\n"
                "          - path: $.rows[0].status\n"
                "            equals: shipped"
            )
        if data.get("db_hash_check"):
            raise ValueError(
                "state_checks.db_hash_check has been removed — it never produced "
                "grading signal on either substrate, and silently passed when enabled "
                "with no expected hash. Use hash grading instead:\n"
                "  state_checks:\n"
                "    hash:\n"
                "      enabled: true\n"
                "      golden_actions: [...]        # or expect_initial_state: true"
            )
        return {key: value for key, value in data.items() if key not in RETIRED_STATE_CHECK_KEYS}

    @field_validator("id_fields")
    @classmethod
    def _validate_id_fields(cls, value: dict[str, str | list[str]]) -> dict[str, str | list[str]]:
        return validate_id_fields_declaration(value)

    @model_validator(mode="after")
    def _refuse_probes_beside_another_state_source(self) -> Self:
        """Reject at load a probe declared beside a source this component also scores.

        Defined above the weight rule, which Pydantic therefore runs second: a block
        declaring probes, assertions and a hash source with no weight satisfies both
        conditions, and a weight is not the fix for a block refused outright.
        """
        refuse_probes_beside_another_state_source(
            db_probes=self.db_probes,
            jsonpaths=self.jsonpaths,
            hash_config=self.hash,
            context="grading.yaml state_checks",
        )
        return self

    @model_validator(mode="after")
    def _validate_hash_weight_declaration(self) -> Self:
        """Reject at load the one shape whose ``state_checks`` score is undecidable."""
        resolve_hash_weight(
            self.hash,
            jsonpaths=self.jsonpaths,
            context=AUTHORED_HASH_WEIGHT_CONTEXT,
        )
        return self


class GradingCombineConfig(BaseModel):
    """Grading combination configuration.

    ``weights`` defaults to an empty dict so a project-level defaults
    block may declare only a partial view (e.g. ``pass_threshold`` alone).
    Consumers that require weights validate presence at use-site.

    ``extra="forbid"`` because every field here has a default a dropped key would
    silently substitute: a ``pass_treshold`` typo graded the pack at ``0.8``
    whatever the author wrote. The refusal holds on every construction path,
    ``project.yaml``'s ``task_defaults.grading_defaults.combine`` included.
    """

    model_config = {"extra": "forbid"}

    method: CombineMethod = "weighted"
    weights: dict[str, float] = Field(default_factory=dict)
    pass_threshold: float = 0.8

    @field_validator("method", mode="before")
    @classmethod
    def _validate_method(cls, value: Any) -> Any:
        # Before the Literal, which would answer a retired alias with a bare
        # literal_error naming no replacement.
        return validate_combine_method(value, context="grading.yaml combine.method")


class GradingConfig(BaseModel):
    """Grading specification"""

    model_config = {"extra": "ignore"}

    combine: GradingCombineConfig
    state_checks: StateChecksConfig | None = None
    transcript_rules: TranscriptRulesConfig | None = None
    trace_checks: TraceChecksConfig | None = None
    llm_judge: LLMJudgeConfig | None = None
    custom_checks: dict[str, Any] | None = None  # CustomChecksConfig as dict for flexibility


class LLMJudgeDefaults(BaseModel):
    """Project-level judge defaults under ``grading_defaults.llm_judge``.

    Carries only ``customization`` — a project default never carries a rubric
    (that is required per-task on :class:`LLMJudgeConfig`). ``customization``
    deep-merges under each task's own ``llm_judge.customization``, tri-state
    preserved (an unset task key never overrides a set project key)."""

    customization: JudgeCustomization | None = None

    model_config = {"extra": "forbid"}


class GradingDefaults(BaseModel):
    """Grading defaults applied to every task via ``task_defaults``. A
    task's own ``grading.yaml.combine`` deep-merges on top."""

    model_config = {"extra": "ignore"}

    combine: GradingCombineConfig | None = None
    llm_judge: LLMJudgeDefaults | None = None


class TaskDefaults(BaseModel):
    """Base task-level configuration inherited by every task in a project.

    Applied at loader time; ``task.yaml`` deltas deep-merge on top with
    task fields winning on conflict. Every field is optional — omitting a
    field means the engine default (or an adapter default) applies.
    """

    model_config = {"extra": "ignore"}

    adapter_type: str | None = None
    max_turns: int | None = Field(default=None, ge=1)
    interaction_mode: InteractionMode | None = None
    """Project-side default for :attr:`TaskConfig.interaction_mode`.
    ``None`` leaves the engine default (``conversational``) in effect;
    a task's own ``interaction_mode`` overrides. Enables a project to
    declare "every task under me is agent_only" once instead of per
    task.yaml."""
    system_prompt: str | None = None
    actors: dict[str, ActorSpec] | None = None
    """Named actor map inherited by every task. ``actors.user`` configures
    the user simulator; the loader lifts a legacy top-level
    ``user_simulator`` block into it. ``actors.agent`` and ``actors.judge``
    are reserved and rejected at load time."""

    policies: dict[str, Any] = Field(default_factory=dict)
    metadata: TaskMetadata | None = None
    adapter_settings: dict[str, Any] = Field(default_factory=dict)
    tools: ToolsConfig | None = None
    grading_defaults: GradingDefaults | None = None
    timeouts: TimeoutDefaults | None = None
    stuck_heuristics: StuckHeuristicsDefaults | None = None
    continue_prompt: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_user_simulator_kwarg(cls, data: Any) -> Any:
        """Direct-Python compat shim: lift a legacy ``user_simulator=...``
        constructor kwarg into ``actors["user"]`` with a
        ``DeprecationWarning``. YAML loads handle this via
        :func:`canonicalize_actor_config` per layer pre-merge; this covers
        the ``TaskDefaults(user_simulator=...)`` direct-construction case."""
        return _lift_user_simulator_kwarg(data)

    @field_validator("actors")
    @classmethod
    def _reject_reserved_actor_names(
        cls, value: dict[str, ActorSpec] | None
    ) -> dict[str, ActorSpec] | None:
        return _validate_actors_map(value)


SeedKind = Literal["sql_dump", "filesystem_dir", "redis_dump", "bare"]
"""Vocabulary for the reset-seed kinds :class:`SeedRef` accepts.

``sql_dump`` (postgres/sqlite dumps), ``filesystem_dir`` (copy into a
service workspace), ``redis_dump`` (RDB snapshots), ``bare`` (raw file,
no interpretation). Kind selection binds an overlay recipe with the
isolation-redesign milestone."""


SEED_KIND_BY_EXTENSION: dict[str, SeedKind] = {
    ".sql": "sql_dump",
    ".rdb": "redis_dump",
}
"""File-extension → seed kind. Ambiguous / unknown extensions require
the full ``{path, kind}`` shape. Public so the CLI's
``tolokaforge assets stamp`` verb can reuse the same mapping without
duplicating (and drifting)."""


class SeedRef(BaseModel):
    """One entry in ``assets.seeds`` — a named baseline that reset
    recipes and initial-state fixtures reference by name.

    Two authoring shapes both parse: the full ``{path, kind, digest}``
    dict, or a bare string path (kind inferred from the extension when
    unambiguous). The bare-string form is only legal at load time when
    the enclosing loader stamps the digest before construction — the
    ``tolokaforge assets stamp`` verb is the canonical migration path."""

    path: Path
    """Location of the seed file. Anchored to the project directory by
    the loader before this model is constructed."""

    kind: SeedKind
    """How the seed is applied. Values that require file-format-specific
    handling map to matching kinds; ``bare`` covers raw files that a
    later recipe consumes verbatim."""

    digest: str
    """``sha256:<64 hex>`` content hash. Verified against the file's
    bytes at load time by :func:`tolokaforge.core.project_loader.load_project_config`
    so a swap without re-stamping fails loud."""

    model_config = {"extra": "ignore"}

    @model_validator(mode="before")
    @classmethod
    def _accept_bare_string_shorthand(cls, data: Any) -> Any:
        """A bare string coerces to ``{path: <s>}`` with ``kind``
        inferred from the file extension. Ambiguous / unknown
        extensions are load-time errors — the author must switch to
        the full form."""
        if not isinstance(data, str):
            return data
        raw = data
        ext = Path(raw).suffix.lower()
        inferred = SEED_KIND_BY_EXTENSION.get(ext)
        if inferred is None:
            raise ValueError(
                f"SeedRef: cannot infer kind from path {raw!r} "
                f"(extension {ext!r} not in "
                f"{sorted(SEED_KIND_BY_EXTENSION)!r}). Declare the full "
                "form: {path: ..., kind: ...}."
            )
        return {"path": raw, "kind": inferred}


class AssetsConfig(BaseModel):
    """Project-level asset registry.

    Currently only ``seeds`` — a name → :class:`SeedRef` map — is
    modelled. Additional asset categories land as the design grows."""

    seeds: dict[str, SeedRef] = Field(default_factory=dict)

    model_config = {"extra": "ignore"}


class TaskDiscoveryConfig(BaseModel):
    """Where the loader finds task files under the project directory."""

    model_config = {"extra": "ignore"}

    glob: str = "tasks/**/task.yaml"


class TaskInventoryConfig(BaseModel):
    """Task discovery configuration on the Project."""

    model_config = {"extra": "ignore"}

    discovery: TaskDiscoveryConfig = Field(default_factory=TaskDiscoveryConfig)


class ProjectConfig(BaseModel):
    """Top-level Project spec — a ``project.yaml`` at a pack root.

    Holds identity, task discovery, the default environment every task
    inherits, and two labelled base blocks: ``task_defaults`` (base for
    tasks) and ``run_defaults`` (base for run configs). See
    ``docs/PROJECTS.md``.
    """

    model_config = {"extra": "ignore"}

    name: str
    version: int = 1
    description: str | None = None
    tasks: TaskInventoryConfig = Field(default_factory=TaskInventoryConfig)
    default_environment: EnvironmentPatch | None = None
    """Base environment every task inherits. Composed with each task's
    own ``environment_manifest`` patch by
    :func:`tolokaforge.core.project_loader.resolve` to produce the
    concrete :class:`EnvironmentManifest`."""

    assets: AssetsConfig | None = None
    """Project-level asset registry. Only ``seeds`` is modelled today;
    reset-recipe binding lands with the isolation redesign."""

    task_defaults: TaskDefaults = Field(default_factory=TaskDefaults)
    run_defaults: RunDefaults | None = None
