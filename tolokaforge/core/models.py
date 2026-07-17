"""Pydantic models for configuration and data structures"""

import dataclasses
import warnings
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal, Self, get_args

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
from tolokaforge.runner.models import EnvironmentPatch as EnvironmentPatch
from tolokaforge.runner.models import JudgeCustomization as JudgeCustomization
from tolokaforge.runner.models import LLMJudgeConfig as LLMJudgeConfig
from tolokaforge.runner.models import ResetSpec as ResetSpec
from tolokaforge.runner.models import Rubric as Rubric
from tolokaforge.runner.models import ServiceIsolation as ServiceIsolation
from tolokaforge.runner.models import ServiceSpec as ServiceSpec
from tolokaforge.runner.models import StackPatch as StackPatch


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
    consistency_rejections: int = 0

    model_config = {"extra": "forbid"}


class JudgeKbGating(BaseModel):
    """The rubric judge's knowledge-search gating for this trial (host-side model).

    Kept distinct from :class:`JudgeUsage` (which stays strictly token/cost
    accounting): this is the audit + replay record of *which* KB tools the judge
    was offered and which config withheld. ``knowledge_search_disabled`` is the
    authoritative replay signal (#451) — ``True`` means
    ``grading.llm_judge.customization.disable_knowledge_search`` withheld KB,
    regardless of whether the agent had a KB tool this trial. ``offered`` /
    ``withheld`` are supporting audit detail; an empty ``withheld`` on a disabled
    judge means the agent had no KB tool to gate.
    """

    knowledge_search_disabled: bool
    offered: list[str]
    withheld: list[str]

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
    # The judge's knowledge-search gating for this trial (offered / withheld / whether
    # config disabled KB). ``None`` when no judge ran. Serialized inline in
    # ``grade.yaml`` (a scalar/lists block, unlike the transcript sidecar). Separate
    # from ``judge_usage``, which stays token/cost only. See docs/OUTPUT_FORMAT.md.
    judge_kb_gating: JudgeKbGating | None = None


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
    """Run-level cap on per-trial timeouts. Effective values applied
    by the runtime = ``min(TaskConfig.timeouts, this)`` — the
    task-scoped value is authoritative, this is an optional
    operator-side clamp. Unset means the task-scoped value governs.
    Field-name migration to ``TimeoutDefaults`` (``trial_seconds`` /
    ``tool_call_seconds``) lands with the cleanup milestone."""

    max_turns: int = 50
    """Run-level cap on per-trial ``max_turns``. Effective value at
    runtime = ``min(TaskConfig.max_turns, this)``. Unset behaviour is
    preserved by the default (50) acting as the cap when the task
    declares nothing higher — but authors should treat this as an
    optional clamp rather than the authoritative value."""

    auto_start_services: bool = True  # Auto-start Docker services via EngineStack

    continue_prompt: str = "Please proceed to the next step."
    """Deprecated. Not consumed by any runtime code today; the
    canonical home is ``TaskDefaults.continue_prompt``. Kept for
    backward compatibility of run configs that declare it; a
    ``DeprecationWarning`` fires when the field is explicitly set to
    a non-default value."""

    stuck_heuristics: StuckHeuristics = Field(default_factory=StuckHeuristics)
    """Deprecated. The conductor now reads stuck-heuristics from the
    task-scoped ``TaskConfig.stuck_heuristics`` (populated via the M2
    loader's per-task merge chain from
    ``project.task_defaults.stuck_heuristics``). Kept on this model
    for backward compatibility; a ``DeprecationWarning`` fires when
    the field is explicitly set."""

    runtime: Literal["shared", "per_trial"] | None = None
    """Deprecated operator override for backend selection.

    Backend selection is task-driven — the orchestrator picks
    :class:`PerTrialRuntimeBackend` when any task's manifest requires
    per-trial materialisation, otherwise :class:`SharedStackRuntimeBackend`.
    Setting this field bypasses that signal and emits a
    ``DeprecationWarning``. Retired in a future release.

    Legacy value ``docker`` is accepted as an alias for ``shared`` with
    the same deprecation warning; drop both from configs going forward.
    """

    @field_validator("runtime", mode="before")
    @classmethod
    def _accept_legacy_docker_alias(cls, value: Any) -> Any:
        """Accept ``docker`` as an alias for ``shared`` and emit the
        deprecation warning for any explicit setting."""
        if value is None:
            return value
        if value == "docker":
            warnings.warn(
                "OrchestratorConfig.runtime = 'docker' is a deprecated alias "
                "for 'shared'; update your run config.",
                DeprecationWarning,
                stacklevel=2,
            )
            value = "shared"
        warnings.warn(
            "OrchestratorConfig.runtime is deprecated; backend selection is "
            "now task-driven (any task requiring per-trial isolation forces "
            "PerTrialRuntimeBackend, otherwise SharedStackRuntimeBackend). "
            "Drop `orchestrator.runtime` from the run config. Retired in a "
            "future release.",
            DeprecationWarning,
            stacklevel=2,
        )
        return value

    @model_validator(mode="before")
    @classmethod
    def _warn_deprecated_task_scope_fields(cls, values: Any) -> Any:
        """Emit ``DeprecationWarning`` when a caller sets
        ``stuck_heuristics`` or ``continue_prompt`` on the run-side
        orchestrator config. Both fields have canonical homes on
        ``TaskDefaults`` (``TaskDefaults.stuck_heuristics``,
        ``TaskDefaults.continue_prompt``); the orchestrator copies are
        retained for backward compatibility and retired with the
        cleanup milestone.
        """
        if not isinstance(values, dict):
            return values
        for field_name in ("stuck_heuristics", "continue_prompt"):
            if field_name in values:
                warnings.warn(
                    f"OrchestratorConfig.{field_name} is deprecated; move "
                    f"it under task_defaults.{field_name} on the enclosing "
                    "project. The orchestrator copy is retained for "
                    "backward compatibility only.",
                    DeprecationWarning,
                    stacklevel=2,
                )
        return values

    typesense: TypeSenseConfig | None = None  # TypeSense server configuration


class HarnessAdapterConfig(BaseModel):
    """Configuration for external harness adapters (e.g., Tau-bench)"""

    type: str = "native"  # "native", "tau", etc.
    params: dict[str, Any] = Field(default_factory=dict)


class EvaluationConfig(BaseModel):
    """Evaluation configuration.

    ``projects`` lists project roots this run pulls tasks from. When
    omitted the loader defaults to the enclosing project (the project
    directory containing the run config file). Legacy configs may use
    ``task_packs`` — it is accepted here as an alias for ``projects``
    and coerced with a ``DeprecationWarning``.
    """

    tasks_glob: str = "**/task.yaml"
    projects: list[str] = Field(default_factory=list)
    task_packs: list[str] = Field(default_factory=list)
    output_dir: str
    cache_images: bool = True
    harness_adapter: HarnessAdapterConfig | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_task_packs_alias(cls, values: Any) -> Any:
        """Accept ``task_packs`` as a deprecated alias for ``projects``.

        Emits a ``DeprecationWarning`` when the legacy key appears with
        a non-empty value and no explicit ``projects`` key. When both
        keys carry values the loader keeps ``projects`` and drops
        ``task_packs`` (with a warning naming the collision).
        """
        if not isinstance(values, dict):
            return values
        legacy = values.get("task_packs")
        canonical = values.get("projects")
        if not legacy:
            return values
        if canonical:
            warnings.warn(
                "evaluation.task_packs and evaluation.projects both set; "
                "projects wins. Drop task_packs from the run config.",
                DeprecationWarning,
                stacklevel=2,
            )
            values["task_packs"] = []
            return values
        warnings.warn(
            "evaluation.task_packs is deprecated; use evaluation.projects "
            "instead. task_packs still accepted as an alias for one release.",
            DeprecationWarning,
            stacklevel=2,
        )
        values["projects"] = list(legacy)
        values["task_packs"] = []
        return values


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


class LocalDockerComputeConfig(BaseModel):
    """Configuration for the ``local-docker`` compute provider."""


class ComputeConfig(BaseModel):
    """Compute substrate + parallelism + budget selection for a run.

    ``provider`` selects the runtime substrate; the sub-block matching
    the provider (e.g. ``local_docker``) carries provider-specific
    settings. When another provider is registered it shows up as a new
    ``Literal`` value on ``provider`` plus its own sub-block.
    """

    provider: Literal["local-docker"] = "local-docker"
    workers: int | None = Field(default=None, ge=1)
    max_budget_usd: float | None = Field(default=None, ge=0.0)
    max_requests_per_second: float | None = Field(default=None, gt=0.0)
    max_attempt_retries: int = Field(default=0, ge=0)
    log_tail: int = Field(default=500, ge=1)
    capture_logs_on_success: bool = False
    local_docker: LocalDockerComputeConfig | None = None

    capabilities: list[Any] = Field(default_factory=list)
    """Backend-capability declarations. Each entry is either a bare
    ``"name"`` string or a ``{"name": {params}}`` mapping with a single
    key. Field is typed ``list[Any]`` so :meth:`_validate_capability_entries`
    can emit context-rich errors (Pydantic's built-in union resolution
    reports the failure against every union arm, which reads badly for
    authors). Registry lookup and admission gate land with the isolation
    redesign; this field reserves the shape so packs can start
    declaring capabilities against the eventual registry vocabulary."""

    @field_validator("capabilities")
    @classmethod
    def _validate_capability_entries(cls, value: list[Any]) -> list[Any]:
        for idx, entry in enumerate(value):
            if isinstance(entry, str):
                if not entry:
                    raise ValueError(
                        f"ComputeConfig.capabilities[{idx}]: bare-string entry "
                        "must be a non-empty capability name."
                    )
                continue
            if isinstance(entry, dict):
                if len(entry) != 1:
                    raise ValueError(
                        f"ComputeConfig.capabilities[{idx}]: dict entry must have "
                        f"exactly one key (the capability name); got {sorted(entry)!r}."
                    )
                ((name, params),) = entry.items()
                if not isinstance(name, str) or not name:
                    raise ValueError(
                        f"ComputeConfig.capabilities[{idx}]: capability name must "
                        f"be a non-empty string; got {name!r}."
                    )
                if not isinstance(params, dict):
                    raise ValueError(
                        f"ComputeConfig.capabilities[{idx}]: params for capability "
                        f"{name!r} must be a mapping; got {type(params).__name__}."
                    )
                continue
            raise ValueError(
                f"ComputeConfig.capabilities[{idx}]: entry must be a string or a "
                f"single-key dict; got {type(entry).__name__}."
            )
        return value


class LocalStorageConfig(BaseModel):
    """Local-filesystem storage backend for artifacts or logs.

    Extras rejected so a mis-tagged input (e.g. ``bucket`` on a
    ``type=local`` block) fails loud instead of dropping the stray
    field. The discriminator on ``StorageBackend`` selects this variant
    by ``type``; extras=forbid makes the selection safe.
    """

    model_config = {"extra": "forbid"}

    type: Literal["local"] = "local"
    path: str


class S3StorageConfig(BaseModel):
    """S3 storage backend for artifacts or logs.

    Extras rejected — same rationale as :class:`LocalStorageConfig`.
    """

    model_config = {"extra": "forbid"}

    type: Literal["s3"] = "s3"
    bucket: str
    prefix: str | None = None


# Discriminated union over the ``type`` tag so mixed-tag inputs fail
# loud instead of silently dropping the fields of the losing variant.
StorageBackend = Annotated[LocalStorageConfig | S3StorageConfig, Field(discriminator="type")]


class QueueStorageConfig(BaseModel):
    """Queue backend for orchestrator state.

    ``backend='postgres'`` requires ``postgres_dsn`` — enforced by
    ``_require_postgres_dsn`` so a partial declaration fails at load
    instead of falling back silently.
    """

    backend: Literal["sqlite", "postgres"] = "sqlite"
    postgres_dsn: str | None = None

    @model_validator(mode="after")
    def _require_postgres_dsn(self) -> Self:
        if self.backend == "postgres" and not self.postgres_dsn:
            raise ValueError("QueueStorageConfig.backend='postgres' requires postgres_dsn.")
        return self


class StorageConfig(BaseModel):
    """Where a run's artifacts, logs, and queue state live."""

    artifacts: StorageBackend | None = None
    logs: StorageBackend | None = None
    queue: QueueStorageConfig | None = None


class TracingConfig(BaseModel):
    """Tracing exporter selection.

    A non-default ``exporter`` requires an ``endpoint``; ``none`` (the
    default) does not.
    """

    exporter: Literal["none", "otlp"] = "none"
    endpoint: str | None = None

    @model_validator(mode="after")
    def _require_endpoint_when_active(self) -> Self:
        if self.exporter != "none" and not self.endpoint:
            raise ValueError(f"TracingConfig.exporter={self.exporter!r} requires endpoint.")
        return self


class MetricsConfig(BaseModel):
    """Metrics exporter selection.

    A non-default ``exporter`` requires an ``endpoint``; ``none`` (the
    default) does not.
    """

    exporter: Literal["none", "prometheus"] = "none"
    endpoint: str | None = None

    @model_validator(mode="after")
    def _require_endpoint_when_active(self) -> Self:
        if self.exporter != "none" and not self.endpoint:
            raise ValueError(f"MetricsConfig.exporter={self.exporter!r} requires endpoint.")
        return self


class LoggingConfig(BaseModel):
    """Logging exporter selection.

    ``exporter='otlp'`` requires an ``endpoint``; ``stdout`` (the
    default) does not.
    """

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    exporter: Literal["stdout", "otlp"] = "stdout"
    endpoint: str | None = None

    @model_validator(mode="after")
    def _require_endpoint_when_active(self) -> Self:
        if self.exporter == "otlp" and not self.endpoint:
            raise ValueError(f"LoggingConfig.exporter={self.exporter!r} requires endpoint.")
        return self


class ObservabilityConfig(BaseModel):
    """Tracing, metrics, and logging exporters for a run."""

    tracing: TracingConfig | None = None
    metrics: MetricsConfig | None = None
    logging: LoggingConfig | None = None


_DUAL_HOME_COMPUTE_ALIASES: tuple[tuple[str, str], ...] = (
    ("workers", "workers"),
    ("max_budget_usd", "max_budget_usd"),
    ("max_requests_per_second", "max_requests_per_second"),
    ("max_attempt_retries", "max_attempt_retries"),
)
"""``orchestrator.<legacy>`` → ``compute.<canonical>`` field pairs.
Same names on both sides; kept explicit so a future rename hits one
list."""

_DUAL_HOME_STORAGE_QUEUE_ALIASES: tuple[tuple[str, str], ...] = (
    ("queue_backend", "backend"),
    ("queue_postgres_dsn", "postgres_dsn"),
)
"""``orchestrator.<legacy>`` → ``storage.queue.<canonical>`` field
pairs. Legacy names carry the ``queue_`` prefix; canonical names
don't (the ``queue`` sub-block is the namespace)."""


class RunConfig(BaseModel):
    """Complete run configuration"""

    models: dict[str, ModelConfig]
    orchestrator: OrchestratorConfig
    evaluation: EvaluationConfig
    engine: EngineConfig | None = None
    compute: ComputeConfig | None = None
    storage: StorageConfig | None = None
    observability: ObservabilityConfig | None = None

    @property
    def effective_workers(self) -> int:
        """Effective worker count for this run.

        Canonical home is ``compute.workers``. When the user declared
        it (directly or via the ``orchestrator.workers`` legacy alias,
        which the parse-time lift moved to ``compute.workers``), the
        canonical value wins. Otherwise falls back to the
        ``OrchestratorConfig.workers`` default so runs that never
        touched either field still work.
        """
        if self.compute is not None and self.compute.workers is not None:
            return self.compute.workers
        return self.orchestrator.workers

    @property
    def effective_max_budget_usd(self) -> float | None:
        """Effective per-run budget cap in USD. ``compute.max_budget_usd``
        is canonical; falls back to ``orchestrator.max_budget_usd``."""
        if self.compute is not None and self.compute.max_budget_usd is not None:
            return self.compute.max_budget_usd
        return self.orchestrator.max_budget_usd

    @property
    def effective_max_requests_per_second(self) -> float | None:
        """Effective global request throttle. ``compute.max_requests_per_second``
        is canonical; falls back to
        ``orchestrator.max_requests_per_second``."""
        if self.compute is not None and self.compute.max_requests_per_second is not None:
            return self.compute.max_requests_per_second
        return self.orchestrator.max_requests_per_second

    @property
    def effective_max_attempt_retries(self) -> int:
        """Effective retry attempts for transient infra failures.
        ``compute.max_attempt_retries`` is canonical; falls back to
        ``orchestrator.max_attempt_retries``.

        Asymmetric with the other ``effective_*`` accessors: the field
        is a plain ``int`` with default ``0`` on both sides — there is
        no ``None`` sentinel to distinguish "unset" from "explicit 0".
        Whenever ``compute`` exists, its value is authoritative; the
        parse-time lift ensures both sides agree by the time either is
        constructed. Object-form callers (``RunConfig(compute=...)``)
        who need the fallback must leave ``compute`` unset entirely."""
        if self.compute is not None:
            return self.compute.max_attempt_retries
        return self.orchestrator.max_attempt_retries

    @property
    def effective_queue_backend(self) -> str:
        """Effective queue-storage backend. ``storage.queue.backend`` is
        canonical; falls back to ``orchestrator.queue_backend``."""
        if self.storage is not None and self.storage.queue is not None:
            return self.storage.queue.backend
        return self.orchestrator.queue_backend

    @property
    def effective_queue_postgres_dsn(self) -> str | None:
        """Effective postgres DSN when the queue backend is ``postgres``.
        ``storage.queue.postgres_dsn`` is canonical; falls back to
        ``orchestrator.queue_postgres_dsn``."""
        if self.storage is not None and self.storage.queue is not None:
            return self.storage.queue.postgres_dsn
        return self.orchestrator.queue_postgres_dsn

    @model_validator(mode="before")
    @classmethod
    def _lift_orchestrator_dual_home_aliases(cls, values: Any) -> Any:
        """Lift legacy ``orchestrator.*`` fields to their canonical
        ``compute.*`` / ``storage.queue.*`` homes at parse time.

        The six aliases (workers, max_budget_usd, max_requests_per_second,
        max_attempt_retries, queue_backend, queue_postgres_dsn) once
        lived on ``OrchestratorConfig`` alone; the Project layer moved
        them to ``ComputeConfig`` / ``StorageConfig``. This validator
        preserves the legacy shape as a read-time alias so unmigrated
        run configs still load, emits per-key ``DeprecationWarning``,
        and drops the legacy key from ``orchestrator`` so downstream
        reads route through the canonical field.

        Collision policy — if both sides carry values:
        - Equal values: warn once naming the collision, drop legacy.
        - Differing values: fail loud naming both keys and both values;
          the author must pick one.

        Scope: only dict-form inputs are lifted. Object-form callers
        that pass an already-constructed ``OrchestratorConfig``
        instance (e.g. tests using ``RunConfig(orchestrator=
        OrchestratorConfig(workers=4))``) bypass the lift entirely —
        the effective-config accessors' fallback branch surfaces the
        orchestrator value in that case, but no deprecation warning
        fires. Production YAML load always passes dicts, so the lift
        runs on every real load.
        """
        if not isinstance(values, dict):
            return values
        orch_input = values.get("orchestrator")
        if not isinstance(orch_input, dict):
            return values

        # Copy input containers before mutating so callers who kept a
        # reference to the raw dict (e.g. ``config_validator`` reads
        # ``raw["orchestrator"]`` after calling ``RunConfig(**raw)``)
        # still see their original layout.
        values = dict(values)
        orch = dict(orch_input)
        values["orchestrator"] = orch

        compute_input = values.get("compute")
        compute = dict(compute_input) if isinstance(compute_input, dict) else {}
        for legacy_key, canonical_key in _DUAL_HOME_COMPUTE_ALIASES:
            _lift_alias(orch, legacy_key, compute, canonical_key, "compute")
        if compute:
            values["compute"] = compute

        storage_input = values.get("storage")
        storage = dict(storage_input) if isinstance(storage_input, dict) else {}
        queue_input = storage.get("queue")
        queue = dict(queue_input) if isinstance(queue_input, dict) else {}
        for legacy_key, canonical_key in _DUAL_HOME_STORAGE_QUEUE_ALIASES:
            _lift_alias(orch, legacy_key, queue, canonical_key, "storage.queue")
        if queue:
            storage["queue"] = queue
            values["storage"] = storage

        return values


def _lift_alias(
    legacy_container: dict[str, Any],
    legacy_key: str,
    canonical_container: dict[str, Any],
    canonical_key: str,
    canonical_container_label: str,
) -> None:
    """Lift a single legacy key into a canonical container in place.

    Removes the legacy key from *legacy_container* (so downstream reads
    can't accidentally see both). Emits ``DeprecationWarning`` when the
    legacy key was set; raises ``ValueError`` on a collision with a
    different canonical value.
    """
    if legacy_key not in legacy_container:
        return
    legacy_value = legacy_container[legacy_key]
    canonical_value = canonical_container.get(canonical_key)
    if canonical_value is not None and canonical_value != legacy_value:
        raise ValueError(
            f"orchestrator.{legacy_key}={legacy_value!r} conflicts with "
            f"{canonical_container_label}.{canonical_key}={canonical_value!r}; "
            f"drop the legacy `orchestrator.{legacy_key}` and keep the "
            f"canonical `{canonical_container_label}.{canonical_key}`."
        )
    if canonical_value is None:
        canonical_container[canonical_key] = legacy_value
    warnings.warn(
        f"orchestrator.{legacy_key} is deprecated; use "
        f"{canonical_container_label}.{canonical_key} instead. Legacy "
        "field will be removed in a future release.",
        DeprecationWarning,
        stacklevel=4,
    )
    del legacy_container[legacy_key]


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


_RESERVED_ACTOR_NAMES = frozenset({"agent", "judge"})
"""Actor names reserved for future actors. ``actors.user`` is the
counterpart actor today; ``actors.agent`` / ``actors.judge`` are
reserved so a pack cannot use the names for something else in the
meantime."""


class ActorSpec(BaseModel):
    """Single entry inside the ``actors`` map.

    Fields mirror the shape :class:`UserSimulatorConfig` carries today —
    binding ``actors.user`` to the simulator's config lands with the
    canonical rename. Sub-keys ``tools`` and ``service`` are reserved
    by the design for future actor types; using them today is a load
    error (``extra="forbid"``).
    """

    mode: Literal["scripted", "llm"] | None = None
    persona: str | None = None
    backstory: str | None = None
    scripted_flow: list[dict[str, str]] | None = None

    model_config = {"extra": "forbid"}


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


class TaskMetadata(BaseModel):
    """Optional metadata used for analytics slicing."""

    complexity: str | None = None
    expected_failure_modes: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class TimeoutDefaults(BaseModel):
    """Task-shape timeouts applied to every task via ``task_defaults``.
    Consumed at task scope; the run-side ``OrchestratorConfig.timeouts``
    is a run-level cap that clamps these via the min rule at read
    time."""

    trial_seconds: int = Field(default=600, ge=1)
    tool_call_seconds: int = Field(default=60, ge=1)


class StuckHeuristicsDefaults(BaseModel):
    """Task-shape stuck-detection knobs applied to every task via
    ``task_defaults``. The canonical home for stuck-heuristic config;
    ``OrchestratorConfig.stuck_heuristics`` is deprecated and no longer
    read by the conductor."""

    enabled: bool = True
    max_repeated_tool_calls: int = Field(default=5, ge=1)
    max_idle_turns: int = Field(default=3, ge=1)


class TaskConfig(BaseModel):
    """Task specification"""

    task_id: str
    name: str | None = None
    """Display name. Optional — falls back to ``task_id`` in the adapter."""

    category: str | None = None
    """Legacy grouping label. Optional — project-level association
    replaces the informational role this served for reporting."""

    description: str
    adapter_type: str = "native"  # Adapter runtime type (native, tlk_mcp_core, tau, …)
    max_turns: int | None = None  # Optional per-task turn cap override
    initial_user_message: str | None = None  # If provided, sent directly as first user message
    initial_state: InitialStateConfig = Field(default_factory=InitialStateConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    user_simulator: UserSimulatorConfig = Field(default_factory=UserSimulatorConfig)
    actors: dict[str, ActorSpec] | None = None
    """Task-level actor overrides. Reserved at the parse layer; runtime
    binding lands with the canonical rename. Reserved actor names
    (``agent``, ``judge``) rejected at load time — same rule as
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
    conductor reads from here; ``OrchestratorConfig.stuck_heuristics``
    is deprecated."""

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

    @field_validator("actors")
    @classmethod
    def _reject_reserved_actor_names(
        cls, value: dict[str, ActorSpec] | None
    ) -> dict[str, ActorSpec] | None:
        return _validate_actors_map(value)


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
    db_probes: list[dict[str, Any]] = Field(default_factory=list)


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
    """Grading combination configuration.

    ``weights`` defaults to an empty dict so a project-level defaults
    block may declare only a partial view (e.g. ``pass_threshold`` alone).
    Consumers that require weights validate presence at use-site.
    """

    method: str = "weighted"
    weights: dict[str, float] = Field(default_factory=dict)
    pass_threshold: float = 0.8


class GradingConfig(BaseModel):
    """Grading specification"""

    combine: GradingCombineConfig
    state_checks: StateChecksConfig | None = None
    transcript_rules: TranscriptRulesConfig | None = None
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

    combine: GradingCombineConfig | None = None
    llm_judge: LLMJudgeDefaults | None = None


class TaskDefaults(BaseModel):
    """Base task-level configuration inherited by every task in a project.

    Applied at loader time; ``task.yaml`` deltas deep-merge on top with
    task fields winning on conflict. Every field is optional — omitting a
    field means the engine default (or an adapter default) applies.
    """

    adapter_type: str | None = None
    max_turns: int | None = Field(default=None, ge=1)
    system_prompt: str | None = None
    user_simulator: UserSimulatorConfig | None = None
    actors: dict[str, ActorSpec] | None = None
    """Named actor map. ``actors.user`` is the counterpart actor today
    (co-existing with ``user_simulator`` until the canonical rename);
    ``actors.agent`` and ``actors.judge`` are reserved and rejected at
    load time. Runtime binding lands with the rename milestone."""

    policies: dict[str, Any] = Field(default_factory=dict)
    metadata: TaskMetadata | None = None
    adapter_settings: dict[str, Any] = Field(default_factory=dict)
    tools: ToolsConfig | None = None
    grading_defaults: GradingDefaults | None = None
    timeouts: TimeoutDefaults | None = None
    stuck_heuristics: StuckHeuristicsDefaults | None = None
    continue_prompt: str | None = None

    @field_validator("actors")
    @classmethod
    def _reject_reserved_actor_names(
        cls, value: dict[str, ActorSpec] | None
    ) -> dict[str, ActorSpec] | None:
        return _validate_actors_map(value)


class RunDefaults(BaseModel):
    """Base run-level configuration inherited by every ``run_configs/*.yaml``.

    Applied at loader time; per-invocation run-config files deep-merge on
    top. Every field is optional — a project without ``run_defaults`` acts
    as if every run config were a standalone declaration.
    """

    compute: ComputeConfig | None = None
    storage: StorageConfig | None = None
    observability: ObservabilityConfig | None = None
    orchestrator: OrchestratorConfig | None = None
    models: dict[str, ModelConfig] = Field(default_factory=dict)


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

    model_config = {"extra": "forbid"}

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

    model_config = {"extra": "forbid"}


class TaskDiscoveryConfig(BaseModel):
    """Where the loader finds task files under the project directory."""

    glob: str = "tasks/**/task.yaml"


class TaskInventoryConfig(BaseModel):
    """Task discovery configuration on the Project."""

    discovery: TaskDiscoveryConfig = Field(default_factory=TaskDiscoveryConfig)


class ProjectConfig(BaseModel):
    """Top-level Project spec — a ``project.yaml`` at a pack root.

    Holds identity, task discovery, the default environment every task
    inherits, and two labelled base blocks: ``task_defaults`` (base for
    tasks) and ``run_defaults`` (base for run configs). See
    ``docs/PROJECTS.md``.
    """

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
