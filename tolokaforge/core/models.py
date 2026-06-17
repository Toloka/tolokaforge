"""Pydantic models for configuration and data structures"""

import dataclasses
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, get_args

from pydantic import BaseModel, Field, field_serializer, field_validator

from tolokaforge.core.llm.reasoning import ReasoningConfig, StructuredReasoning
from tolokaforge.core.llm.usage import CostSource, ProviderRawCall, Usage


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


class GradeComponents(BaseModel):
    """Individual grading component scores"""

    state_checks: float | None = None
    transcript_rules: float | None = None
    llm_judge: float | None = None
    custom_checks: float | None = None


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
    # OpenRouter provider routing (pin to specific upstream providers); ignored for other providers.
    provider_order: list[str] | None = None
    allow_fallbacks: bool = True

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
    runtime: Literal["docker"] = "docker"  # Runtime mode (docker only; in-process was removed)
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


class RunConfig(BaseModel):
    """Complete run configuration"""

    models: dict[str, ModelConfig]
    orchestrator: OrchestratorConfig
    evaluation: EvaluationConfig


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


class LLMJudgeConfig(BaseModel):
    """LLM judge configuration"""

    model_ref: str | None = None
    rubric: str
    output_schema: dict[str, Any]
    agentic: bool = False
    system_prompt: str | None = None
    tool_packs: list[str] = Field(default_factory=list)


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
