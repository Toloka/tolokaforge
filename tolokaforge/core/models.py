"""Pydantic models for configuration and data structures"""

import dataclasses
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, get_args

from pydantic import BaseModel, Field, field_serializer, field_validator, model_validator

from tolokaforge.core.llm.reasoning import ReasoningConfig, StructuredReasoning
from tolokaforge.core.llm.usage import CostSource, ProviderRawCall, Usage

# Rubric / Criterion / LLMJudgeConfig have a single canonical home in
# tolokaforge.runner.models — they cross both the YAML grading block and the
# gRPC wire (serialized inside TrialSpec). Re-exported here so existing
# ``core.models`` references (e.g. GradingConfig.llm_judge) resolve without a
# second, drifting definition. CriterionResult is the judge's per-criterion
# output and is consumed by the host-side Grade model below.
from tolokaforge.runner.models import Criterion as Criterion
from tolokaforge.runner.models import CriterionResult as CriterionResult
from tolokaforge.runner.models import EnvironmentManifest as EnvironmentManifest
from tolokaforge.runner.models import LLMJudgeConfig as LLMJudgeConfig
from tolokaforge.runner.models import Rubric as Rubric


class MessageRole(str, Enum):
    """Message role in conversation"""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class TrialStatus(str, Enum):
    """Trial execution status"""

    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    ERROR = "error"


class TerminationReason(str, Enum):
    """Reason why the dialogue was terminated"""

    AGENT_DONE = "agent_done"  # Agent signaled task completion
    USER_STOP = "user_stop"  # User signaled ###STOP###
    STUCK_DETECTED = "stuck_detected"  # Stuck condition detected
    TIMEOUT = "timeout"  # Episode timeout reached
    MAX_TURNS = "max_turns"  # Maximum turns limit reached
    ERROR = "error"  # Runtime error occurred
    RATE_LIMIT = "rate_limit"  # API rate limit error
    API_TIMEOUT = "api_timeout"  # API call timed out after retries
    API_ERROR = "api_error"  # Other API errors
    PROVISION_ERROR = "provision_error"  # Substrate provisioning failed before the trial body ran


class ToolCall(BaseModel):
    """Tool call from agent"""

    id: str
    name: str
    arguments: dict[str, Any]


class Message(BaseModel):
    """Conversation message"""

    role: MessageRole
    content: str = ""
    content_blocks: list[dict[str, Any]] | None = None  # Multimodal content (screenshots)
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    # Structured reasoning / thinking blocks. See tolokaforge.core.llm.reasoning.
    # Bare strings are rejected (Stage 0 migration); callers must pass
    # ``StructuredReasoning`` or a dict parsable by it.
    reasoning: StructuredReasoning | None = None
    ts: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))

    @field_validator("reasoning", mode="before")
    @classmethod
    def _validate_reasoning(cls, value: Any) -> Any:
        if value is None or isinstance(value, StructuredReasoning):
            return value
        if isinstance(value, str):
            raise ValueError(
                "Message.reasoning must be a StructuredReasoning (or dict with "
                "'blocks'/'summary'/'budget_used'), not a bare string. "
                "Legacy string reasoning is no longer supported — see "
                "tolokaforge.core.llm.reasoning.StructuredReasoning."
            )
        if isinstance(value, dict):
            blocks = value.get("blocks", ())
            from tolokaforge.core.llm.reasoning import ReasoningBlock

            coerced_blocks = tuple(
                b if isinstance(b, ReasoningBlock) else ReasoningBlock(**b) for b in blocks
            )
            return StructuredReasoning(
                blocks=coerced_blocks,
                summary=value.get("summary"),
                budget_used=value.get("budget_used"),
            )
        raise TypeError(
            f"Message.reasoning must be StructuredReasoning | dict | None, got {type(value).__name__}"
        )


class ToolUsage(BaseModel):
    """Tool usage statistics"""

    tool_name: str
    call_count: int = 0
    success_count: int = 0
    error_count: int = 0
    total_duration_s: float = 0.0


_VALID_COST_SOURCES: frozenset[str] = frozenset(get_args(CostSource))


def _coerce_calls(value: Any) -> tuple[ProviderRawCall, ...]:
    """Coerce the round-tripped ``calls`` payload back into ``ProviderRawCall``.

    The YAML representation is ``list[dict]``; in-process construction
    passes ``ProviderRawCall`` instances directly. ``cost_source`` is
    rejected here when it isn't one of :data:`CostSource`'s literals,
    so a corrupt YAML surfaces as a Pydantic ``ValidationError`` rather
    than a silently downgraded record.
    """
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"Usage.calls must be a list/tuple, got {type(value).__name__}")
    out: list[ProviderRawCall] = []
    for item in value:
        if isinstance(item, ProviderRawCall):
            out.append(item)
            continue
        if not isinstance(item, dict):
            raise TypeError(
                f"Usage.calls entries must be ProviderRawCall | dict, got {type(item).__name__}"
            )
        source = item.get("cost_source", "unknown")
        if source not in _VALID_COST_SOURCES:
            raise ValueError(
                f"ProviderRawCall.cost_source must be one of {sorted(_VALID_COST_SOURCES)}, "
                f"got {source!r}"
            )
        out.append(ProviderRawCall(**item))
    return tuple(out)


class Metrics(BaseModel):
    """Trial execution metrics.

    ``usage`` carries the full :class:`Usage` accounting (prompt / completion /
    reasoning / cache-creation / cache-read / cached / provider_raw / calls).
    Per-call cost / latency live on ``usage.calls[*]`` — the previously
    redundant flat ``api_call_latencies_s`` list is gone.

    ``cost_usd`` is the trial-level sum of every API call's ``cost_usd``;
    walk ``usage.calls`` to find which calls were litellm- vs locally-priced
    (each :class:`ProviderRawCall` carries its own ``cost_source``). The
    earlier ``cost_usd_est`` / ``cost_usd_provider`` split is gone.
    """

    model_config = {"extra": "forbid"}

    latency_total_s: float = 0.0
    turns: int = 0
    api_calls: int = 0
    usage: Usage = Field(default_factory=Usage)
    cost_usd: float | None = None
    tool_calls: int = 0
    tool_success_rate: float = 0.0
    stuck_detected: bool = False
    tool_usage: list[ToolUsage] = Field(default_factory=list)

    @field_validator("usage", mode="before")
    @classmethod
    def _validate_usage(cls, value: Any) -> Any:
        """Accept ``Usage`` instances or their dict round-trip form.

        YAML deserialisation lands here with a plain ``dict``; the runtime
        accumulation path lands here with a :class:`Usage` instance
        (``default_factory=Usage`` / ``metrics.usage + result.usage``).
        Anything else is rejected to surface serialisation bugs instead of
        masking them. ``calls`` entries are coerced from dicts back into
        :class:`ProviderRawCall`, with ``cost_source`` validated against
        the :data:`CostSource` literal.
        """
        if value is None:
            return Usage()
        if isinstance(value, Usage):
            return value
        if isinstance(value, dict):
            # Drop unknown keys defensively so we can evolve Usage without
            # crashing on historical YAML — but keep ``provider_raw`` as-is.
            known_fields = {
                "prompt_tokens",
                "completion_tokens",
                "reasoning_tokens",
                "cached_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
                "provider_raw",
                "calls",
            }
            filtered = {k: v for k, v in value.items() if k in known_fields}
            if "calls" in filtered:
                filtered["calls"] = _coerce_calls(filtered["calls"])
            return Usage(**filtered)
        raise TypeError(f"Metrics.usage must be Usage | dict | None, got {type(value).__name__}")

    @field_serializer("usage")
    def _serialize_usage(self, value: Usage) -> dict[str, Any]:
        """Emit ``usage`` as a plain dict so ``metrics.yaml`` is readable.

        Pydantic's native dataclass serialisation works, but wrapping it here
        keeps the output-writer contract explicit and guarantees
        ``model_dump(mode="json")`` always yields a ``dict`` — Stage 5c of
        the plan requires this for the trajectory writer. Per-call records
        are emitted via :func:`dataclasses.asdict` so future
        :class:`ProviderRawCall` fields are surfaced automatically.
        """
        return {
            "prompt_tokens": value.prompt_tokens,
            "completion_tokens": value.completion_tokens,
            "reasoning_tokens": value.reasoning_tokens,
            "cached_tokens": value.cached_tokens,
            "cache_creation_input_tokens": value.cache_creation_input_tokens,
            "cache_read_input_tokens": value.cache_read_input_tokens,
            "provider_raw": dict(value.provider_raw),
            "calls": [dataclasses.asdict(call) for call in value.calls],
        }


class JudgeStatus(str, Enum):
    """Outcome of the rubric judge for a grade (mirrors proto ``JudgeStatus``).

    ``ERRORED`` is the fail-loud marker: the judge malfunctioned (retry / budget
    exhaustion or a crash) and there is NO trustworthy numeric score — the
    ``llm_judge`` component is incomplete, NOT 0.0. ``UNSPECIFIED`` means no
    judge was configured / run. The integer values match the proto enum so the
    gRPC wire value maps directly via :meth:`from_proto`.
    """

    UNSPECIFIED = "unspecified"
    COMPLETED = "completed"
    ERRORED = "errored"

    @classmethod
    def from_proto(cls, value: int) -> "JudgeStatus":
        """Map the proto ``JudgeStatus`` integer to this enum (fail loud on unknown)."""
        mapping = {0: cls.UNSPECIFIED, 1: cls.COMPLETED, 2: cls.ERRORED}
        if value not in mapping:
            raise ValueError(f"Unknown proto JudgeStatus value: {value}")
        return mapping[value]


class GradeComponents(BaseModel):
    """Individual grading component scores"""

    state_checks: float | None = None
    transcript_rules: float | None = None
    llm_judge: float | None = None
    custom_checks: float | None = None


class JudgeUsage(BaseModel):
    """The rubric judge's OWN token usage / cost (host-side boundary model).

    The judge runs its own LLM inside the Runner, so its spend is separate from
    the agent's (which lives in ``Metrics.usage``). This is intentionally a
    distinct, small type rather than the per-call
    :class:`~tolokaforge.core.llm.usage.Usage`: the judge reports a single
    aggregate (no per-call cache history), and its ``tool_calls`` / ``calls``
    counters are judge-specific accounting. Field set mirrors the runner's
    :class:`tolokaforge.core.grading.judge.JudgeUsage` dataclass 1:1 and the
    proto ``JudgeReport`` usage fields.
    """

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0

    model_config = {"extra": "forbid"}


class CustomCheckDetail(BaseModel):
    """Detail for individual custom check result"""

    check_name: str
    status: str  # "passed", "failed", "skipped", "error"
    score: float = 0.0
    message: str = ""
    details: dict[str, Any] | None = None


class Grade(BaseModel):
    """Grading result"""

    binary_pass: bool
    score: float = Field(ge=0.0, le=1.0)
    components: GradeComponents = Field(default_factory=GradeComponents)
    reasons: str | dict[str, list[str]] = ""
    state_diff: dict[str, Any] | None = None
    custom_checks_details: list[CustomCheckDetail] | None = None
    # Per-criterion rubric-judge breakdown. ``None`` when no LLM judge ran;
    # an empty list is distinct (judge ran, rubric had no scorable criteria).
    criterion_results: list[CriterionResult] | None = None
    # Rubric-judge outcome. ``UNSPECIFIED`` when no judge was configured;
    # ``ERRORED`` means the judge malfunctioned and the ``llm_judge`` component
    # carries NO score (it must NOT be read as 0.0). See JudgeStatus.
    judge_status: JudgeStatus = JudgeStatus.UNSPECIFIED
    # The judge's own token usage / cost. ``None`` when no judge ran; populated
    # for both COMPLETED and ERRORED runs (an errored judge still spent tokens).
    judge_usage: JudgeUsage | None = None
    # The judge's full message transcript (role / content / tool_calls dicts) —
    # the audit channel for WHY a criterion was scored as it was. Written to a
    # sidecar ``judge_trajectory.yaml`` artifact, not inlined in ``grade.yaml``,
    # so the grade stays scannable (mirrors the trajectory/prompts split). See
    # docs/OUTPUT_FORMAT.md. ``None`` when no judge ran or none was captured.
    judge_transcript: list[dict[str, Any]] | None = None


class Trajectory(BaseModel):
    """Complete trial trajectory.

    Carries only the message trace + status + metrics. The agent's system
    prompt and the user simulator's system prompt live in a sibling
    ``prompts.yaml`` artifact (written by
    :class:`~tolokaforge.core.output.artifacts.FileArtifactWriter.write_prompts`)
    so this file stays small and easy to scan during analysis. Tool
    schemas similarly live in ``tools_schemas.yaml``. Every trial bundle
    is self-contained — no cross-trial sidecars.
    """

    task_id: str
    trial_index: int
    start_ts: datetime
    end_ts: datetime
    status: TrialStatus = TrialStatus.COMPLETED
    termination_reason: TerminationReason | None = None
    messages: list[Message]
    final_env_state: dict[str, Any] = Field(default_factory=dict)
    metrics: Metrics = Field(default_factory=Metrics)
    tool_log: list[dict[str, Any]] = Field(default_factory=list)
    grade: Grade | None = None
    # Monotonic integer stamped on every trajectory; bumped whenever the
    # simulator prompt shape is revised so that downstream analytics can gate
    # comparisons across runs. Starts at 1 per the locked design decision
    # (see plan § "Locked design decisions" item 5). Stays on Trajectory
    # because it's metadata about the message-trace shape, not the prompt
    # itself.
    simulator_schema_version: int = 1


# Configuration Models


class OpenRouterConfig(BaseModel):
    """OpenRouter provider-routing knobs (https://openrouter.ai/docs/features/provider-routing).

    ``provider_order`` lists case-sensitive OpenRouter provider slugs in priority
    order, e.g. ``["Together"]`` or ``["DeepInfra", "Nebius"]``. With
    ``allow_fallbacks=False`` the request is restricted to those providers, which
    is how a model pins around a rate-limited default provider.
    """

    provider_order: list[str] | None = None
    allow_fallbacks: bool = True


class ModelConfig(BaseModel):
    """LLM model configuration"""

    provider: str
    name: str
    temperature: float = 0.0
    max_tokens: int | None = None
    seed: int | None = None
    # Reasoning / thinking configuration. Must be a struct form —
    # bare strings (``reasoning: medium``) are rejected with a migration
    # pointer. See docs/CONFIG.md § reasoning for the schema.
    reasoning: ReasoningConfig = Field(default_factory=ReasoningConfig)
    top_p: float | None = None  # Nucleus sampling parameter (0.0-1.0)
    capabilities: dict[str, Any] | None = None  # Override auto-detected model capabilities
    # OpenRouter-only provider routing; rejected for other providers by the validator below.
    openrouter: OpenRouterConfig | None = None

    @model_validator(mode="after")
    def _reject_openrouter_on_other_providers(self) -> "ModelConfig":
        if self.openrouter is not None and not self.provider.startswith("openrouter"):
            raise ValueError(
                f"`openrouter:` routing is only valid for openrouter models, "
                f"but provider is {self.provider!r}."
            )
        return self

    @field_validator("reasoning", mode="before")
    @classmethod
    def _validate_reasoning(cls, value: Any) -> Any:
        if value is None:
            return ReasoningConfig()
        if isinstance(value, ReasoningConfig):
            return value
        if isinstance(value, str):
            raise ValueError(
                f"`reasoning:` must be a struct ({{mode: ..., budget_tokens: ...}}), "
                f"not the bare string {value!r}. See docs/CONFIG.md."
            )
        if isinstance(value, dict):
            return ReasoningConfig(**value)
        raise TypeError(
            f"`reasoning:` must be ReasoningConfig | dict | None, got {type(value).__name__}"
        )


class TimeoutConfig(BaseModel):
    """Timeout configuration"""

    turn_s: int = 60
    episode_s: int = 1800


class StuckHeuristics(BaseModel):
    """Stuck detection configuration"""

    enabled: bool = True
    max_repeated_tool_calls: int = 10
    max_idle_turns: int = 12


class TypeSenseConfig(BaseModel):
    """TypeSense server configuration for knowledge base search.

    Supports three modes:
    - local: Orchestrator manages a local Docker container (auto start/stop)
    - remote: Connect to an external TypeSense server
    - disabled: TypeSense is disabled, search_policy returns empty results
    """

    enabled: bool = True  # Whether TypeSense is enabled
    mode: Literal["local", "remote", "disabled"] = "local"  # Server mode
    host: str = "127.0.0.1"  # TypeSense server host
    port: int | Literal["auto"] = "auto"  # Port ("auto" finds available port)
    api_key: str | None = None  # API key (auto-generated if None for local mode)
    data_dir: str = ".cache/typesense"  # Data directory for local mode
    image: str = "typesense/typesense:26.0"  # Docker image for local mode
    container_name: str = "tolokaforge-typesense"  # Container name for local mode
    timeout: float = 30.0  # Connection timeout
    cleanup_on_exit: bool = True  # Remove container on exit (local mode)


class OrchestratorConfig(BaseModel):
    """Orchestrator configuration"""

    workers: int = 8
    repeats: int = 5
    # Diagnostic only. Off by default so trial_index=N benefits from
    # warm state (caches, indexes) seeded by trial_index<N. Turn on to
    # decorrelate trial_index from "coldness" when measuring per-index
    # metric asymmetries.
    shuffle_trials: bool = False
    max_budget_usd: float | None = Field(
        default=None, ge=0.0
    )  # Optional hard stop for cumulative run spend
    max_requests_per_second: float | None = Field(
        default=None, gt=0.0
    )  # Optional global request throttle across workers
    max_attempt_retries: int = Field(
        default=0, ge=0
    )  # Number of retry attempts for transient infrastructure failures
    queue_backend: Literal["sqlite", "postgres"] = "sqlite"
    queue_postgres_dsn: str | None = None
    timeouts: TimeoutConfig = Field(default_factory=TimeoutConfig)
    max_turns: int = 50
    auto_start_services: bool = True  # Auto-start Docker services via ServiceStack
    continue_prompt: str = "Please proceed to the next step."
    stuck_heuristics: StuckHeuristics = Field(default_factory=StuckHeuristics)
    runtime: Literal["shared", "per_trial"] = "shared"
    """Runtime backend selection.

    * ``shared`` (default) — one docker-compose stack shared across every
      trial in the run (``SharedStackRuntimeBackend``). Preserves today's
      behaviour; fastest for stateless tasks.
    * ``per_trial`` — one docker-compose stack per trial via Testcontainers
      (``PerTrialRuntimeBackend``). Required by tasks whose manifest
      declares ``isolation: per_trial``.

    Legacy value ``docker`` is accepted as an alias for ``shared`` with a
    deprecation warning at load time; drop it from configs going forward.
    """

    @field_validator("runtime", mode="before")
    @classmethod
    def _accept_legacy_docker_alias(cls, value: Any) -> Any:
        """Accept ``docker`` as a deprecated alias for ``shared`` so
        older run configs continue to load. Emits a ``DeprecationWarning``
        and structured-log-friendly stderr line."""
        if value == "docker":
            import warnings

            warnings.warn(
                "OrchestratorConfig.runtime = 'docker' is a deprecated alias "
                "for 'shared'; update your run config.",
                DeprecationWarning,
                stacklevel=2,
            )
            return "shared"
        return value

    typesense: TypeSenseConfig | None = None  # TypeSense server configuration


class HarnessAdapterConfig(BaseModel):
    """Configuration for external harness adapters (e.g., Tau-bench)"""

    type: str = "native"  # "native", "tau", etc.
    params: dict[str, Any] = Field(default_factory=dict)


class EvaluationConfig(BaseModel):
    """Evaluation configuration"""

    tasks_glob: str = "**/task.yaml"
    task_packs: list[str] = Field(default_factory=list)
    output_dir: str
    cache_images: bool = True
    harness_adapter: HarnessAdapterConfig | None = None


class EngineConfig(BaseModel):
    """Engine-wide configuration that lives outside per-trial/per-model surface.

    Holds operator-level knobs that change *which engine extensions* a run
    picks up at startup — distinct from ``OrchestratorConfig`` (per-run
    execution semantics) and ``ModelConfig`` (per-model overrides).
    """

    presets_file: str | None = Field(
        default=None,
        description=(
            "Path to an additional model-presets YAML overlay. Merged onto "
            "the bundled tolokaforge/core/data/model_presets.yaml at startup "
            "so operators can register or shadow presets without an engine "
            "release. The --presets-file CLI flag takes precedence over this "
            "field. See docs/CONFIG.md and ADR 0002."
        ),
    )


class RunConfig(BaseModel):
    """Complete run configuration"""

    models: dict[str, ModelConfig]
    orchestrator: OrchestratorConfig
    evaluation: EvaluationConfig
    engine: EngineConfig | None = None


# Task Configuration Models


class InitializationAction(BaseModel):
    """One-time environment mutation executed before a trial starts."""

    env_type: Literal["assistant", "user"]
    func_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class InitialStateConfig(BaseModel):
    """Initial environment state configuration"""

    json_db: str | dict[str, Any] | None = None  # JSON DB initial state
    device_overrides: dict[str, Any] | None = None  # Per-task device state overrides
    filesystem: dict[str, Any] | None = None
    mock_web: dict[str, Any] | None = None
    rag: dict[str, Any] | None = None
    system_prompt: str | None = None  # Path to system prompt file (e.g., wiki.md)
    initialization_actions: list[InitializationAction] | None = None


class ToolsConfig(BaseModel):
    """Tools configuration for task"""

    agent: dict[str, Any] = Field(default_factory=lambda: {"enabled": []})
    user: dict[str, Any] = Field(default_factory=lambda: {"enabled": []})


class UserSimulatorConfig(BaseModel):
    """User simulator configuration"""

    mode: Literal["scripted", "llm"] = "llm"
    persona: str = "cooperative"
    backstory: str | None = None  # User instruction for tau-bench parity
    scripted_flow: list[dict[str, str]] | None = None


class TaskMetadata(BaseModel):
    """Optional metadata used for analytics slicing."""

    complexity: str | None = None
    expected_failure_modes: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class TaskConfig(BaseModel):
    """Task specification"""

    task_id: str
    name: str
    category: str
    description: str
    adapter_type: str = "native"  # Adapter runtime type (native, tlk_mcp_core, tau, …)
    max_turns: int | None = None  # Optional per-task turn cap override
    initial_user_message: str | None = None  # If provided, sent directly as first user message
    initial_state: InitialStateConfig
    tools: ToolsConfig
    user_simulator: UserSimulatorConfig
    metadata: TaskMetadata = Field(default_factory=TaskMetadata)
    policies: dict[str, Any] = Field(
        default_factory=dict
    )  # Can contain guidance list or agent_system_prompt string
    grading: str  # Path to grading.yaml
    system_prompt: str | None = None  # Path to system prompt file (e.g., wiki.md)
    adapter_settings: dict[str, Any] | None = None  # Opaque dict parsed by each adapter type
    environment_manifest: EnvironmentManifest | None = None
    """Per-trial substrate declaration (ADR-0009). When set, the adapter
    forwards the manifest onto the ``TaskDescription`` it builds and the
    runtime backend materialises the declared compose stack per trial
    (``PerTrialRuntimeBackend``). Left ``None`` for tasks that run on the
    shared stack. ``compose_file`` is resolved relative to the task's
    directory during ``to_task_description``."""


# Grading Configuration Models


class EnvAssertion(BaseModel):
    """Environment assertion - runs a check function on agent or user environment"""

    env_type: Literal["assistant", "user"]  # which environment to check
    func_name: str  # assertion function name
    arguments: dict[str, Any] = Field(default_factory=dict)  # function arguments
    assert_value: bool = True  # expected return value
    message: str | None = None  # error message if assertion fails


class RequiredAction(BaseModel):
    """Required tool call that must appear in trajectory"""

    action_id: str  # unique identifier for this action
    requestor: Literal["assistant", "user"]  # who should make the call
    name: str  # tool name
    arguments: dict[str, Any] = Field(default_factory=dict)  # tool arguments
    compare_args: list[str] | None = None  # args to compare, None = all


class StateChecksConfig(BaseModel):
    """State checks configuration"""

    jsonpaths: list[dict[str, Any]] = Field(default_factory=list)
    hash: dict[str, Any] | None = None
    env_assertions: list[EnvAssertion] = Field(default_factory=list)  # NEW
    db_hash_check: bool = False  # NEW - compare final DB hash


class CommunicateInfo(BaseModel):
    """Information that should be communicated to user"""

    info: str  # information text to check for
    required: bool = True  # whether this info is required


class TranscriptRulesConfig(BaseModel):
    """Transcript rules configuration"""

    must_contain: list[str] = Field(default_factory=list)
    disallow_regex: list[str] = Field(default_factory=list)
    max_turns: int | None = None
    tool_expectations: dict[str, list[str]] | None = None
    required_actions: list[RequiredAction] = Field(default_factory=list)  # NEW
    communicate_info: list[CommunicateInfo] = Field(default_factory=list)  # NEW


class GradingCombineConfig(BaseModel):
    """Grading combination configuration"""

    method: str = "weighted"
    weights: dict[str, float]
    pass_threshold: float = 0.8


class GradingConfig(BaseModel):
    """Grading specification"""

    combine: GradingCombineConfig
    state_checks: StateChecksConfig | None = None
    transcript_rules: TranscriptRulesConfig | None = None
    llm_judge: LLMJudgeConfig | None = None
    custom_checks: dict[str, Any] | None = None  # CustomChecksConfig as dict for flexibility
