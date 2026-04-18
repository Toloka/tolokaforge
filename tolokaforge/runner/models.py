"""
Pydantic Models for Runner Service

This module contains all Pydantic models used by the Runner service:
- TaskDescription and related models (from TASK_DESCRIPTION_SCHEMA.md)
- TrialContext for per-trial runtime state
- ToolCallRecord for tool execution history
- DB client response models
- Grading result models

All models use Pydantic v2 BaseModel for validation and serialization.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

# =============================================================================
# Enums (from TASK_DESCRIPTION_SCHEMA.md)
# =============================================================================


class AdapterType(str, Enum):
    """Source adapter that produced this description."""

    NATIVE = "native"
    TAU = "tau"
    TLK_MCP_CORE = "tlk_mcp_core"
    TERMINAL_BENCH = "terminal_bench"


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

    toolset: str  # Package/directory: "zendesk", "airline", "telecom"
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


class InitialStateConfig(BaseModel):
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


class InitializationAction(BaseModel):
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


class UserSimulatorConfig(BaseModel):
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


class EnvAssertion(BaseModel):
    """
    Assertion on environment state after trial.

    Used by Native adapter for checking device state.
    """

    env_type: Literal["assistant", "user"]
    func_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    assert_value: Any = True
    message: str | None = None

    model_config = {"extra": "forbid"}


class RequiredAction(BaseModel):
    """Tool call that must appear in the trajectory."""

    action_id: str
    requestor: Literal["assistant", "user"]
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    compare_args: list[str] | None = None  # Which args to compare, None = all

    model_config = {"extra": "forbid"}


class StateChecksConfig(BaseModel):
    """State-based grading configuration."""

    # Hash comparison
    hash_enabled: bool = False
    expected_hash: str | None = None  # Pre-computed (if available)
    golden_actions: list[GoldenAction] = Field(default_factory=list)

    # JSONPath assertions
    jsonpath_checks: list[dict[str, Any]] = Field(default_factory=list)

    # Environment assertions (Native adapter)
    env_assertions: list[EnvAssertion] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


class TranscriptRulesConfig(BaseModel):
    """Transcript-based grading configuration."""

    must_contain: list[str] = Field(default_factory=list)
    disallow_regex: list[str] = Field(default_factory=list)
    max_turns: int | None = None
    required_actions: list[RequiredAction] = Field(default_factory=list)
    communicate_info: list[dict[str, Any]] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


class LLMJudgeConfig(BaseModel):
    """LLM-based grading configuration."""

    model_ref: str  # "openrouter/anthropic/claude-sonnet-4.5"
    rubric: str  # Grading rubric text
    output_schema: dict[str, Any]  # Expected output format

    model_config = {"extra": "forbid"}


class GradingConfig(BaseModel):
    """
    Complete grading configuration.

    Supports multiple methods combined with weights.
    """

    combine_method: Literal["weighted", "all_pass", "any_pass", "all"] = "weighted"
    weights: dict[str, float] = Field(default_factory=lambda: {"state_checks": 1.0})
    pass_threshold: float = 0.8

    state_checks: StateChecksConfig | None = None
    transcript_rules: TranscriptRulesConfig | None = None
    llm_judge: LLMJudgeConfig | None = None

    model_config = {"extra": "forbid"}


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
    category: str  # Domain: "airline", "telecom", "retail"
    description: str  # Task description / user goal
    adapter_type: AdapterType
    schema_version: str = "1.0.0"

    # --- System Prompt ---
    system_prompt: str  # Full content, not file path

    # --- Tools ---
    agent_tools: list[ToolSchema] = Field(default_factory=list)
    user_tools: list[ToolSchema] = Field(default_factory=list)  # User-side device tools

    # --- State ---
    initial_state: InitialStateConfig = Field(default_factory=InitialStateConfig)
    initialization_actions: list[InitializationAction] = Field(default_factory=list)

    # --- User Simulator ---
    user_simulator: UserSimulatorConfig = Field(default_factory=UserSimulatorConfig)

    # --- Search ---
    search: SearchConfig = Field(default_factory=SearchConfig)

    # --- Grading ---
    grading: GradingConfig = Field(default_factory=GradingConfig)

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

    model_config = {"extra": "forbid"}


# =============================================================================
# Tool Call Record (for transcript grading)
# =============================================================================


class ToolCallRecord(BaseModel):
    """Record of a single tool call for transcript grading."""

    tool_name: str
    arguments: dict[str, Any]
    executor: str  # "agent" or "user"
    output: str
    status: str  # "success", "error", "timeout", "tool_not_found", "invalid_arguments"
    latency_seconds: float
    timestamp: str  # ISO format

    model_config = {"extra": "forbid"}


# =============================================================================
# Reconstructed Tools Container
# =============================================================================


class ReconstructedTools(BaseModel):
    """Container for reconstructed tools (non-serializable callables stored separately)."""

    agent_tool_names: list[str] = Field(default_factory=list)
    user_tool_names: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


# =============================================================================
# Trial Context (per-trial runtime state)
# =============================================================================


class TrialContext(BaseModel):
    """
    Per-trial runtime state in the Runner.

    This holds all the information needed to execute tools and grade a trial,
    including the parsed task description, reconstructed tools, and execution history.

    Note: agent_tools and user_tools are stored as Dict[str, Any] because
    Pydantic cannot serialize callables. The actual ToolWrapper objects are
    stored in a separate non-Pydantic dict in the service.
    """

    trial_id: str
    task_description: TaskDescription
    tool_call_history: list[ToolCallRecord] = Field(default_factory=list)
    default_timeout: float = 30.0

    # Note: We can't store callables in Pydantic, so tools are managed separately
    # in the service layer. These fields track which tools are available.
    agent_tool_names: list[str] = Field(default_factory=list)
    user_tool_names: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid", "arbitrary_types_allowed": True}

    @property
    def grading_config(self) -> GradingConfig:
        """Get grading config from task description."""
        return self.task_description.grading

    def record_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        output: str,
        status: str,
        executor: str,
        latency_seconds: float,
    ) -> None:
        """
        Record a tool call in the history for transcript grading.

        Args:
            tool_name: Name of the tool called
            arguments: Tool arguments
            output: Tool output or error message
            status: Execution status
            executor: "agent" or "user"
            latency_seconds: Execution time
        """
        from datetime import timezone

        record = ToolCallRecord(
            tool_name=tool_name,
            arguments=arguments,
            executor=executor,
            output=output,
            status=status,
            latency_seconds=latency_seconds,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        self.tool_call_history.append(record)

    def clear_history(self) -> None:
        """Clear tool call history (used on reset)."""
        self.tool_call_history.clear()


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


class TranscriptEvaluationResult(BaseModel):
    """Result of evaluating all transcript rules."""

    # Use 'passed' as the field name (not 'pass' which is a Python keyword)
    passed: bool = False
    score: float = 0.0
    details: list[TranscriptRuleResult] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


class GradeComponents(BaseModel):
    """Component scores for grading."""

    hash_match: bool | None = None
    hash_score: float = -1.0  # -1.0 means not evaluated
    jsonpath_score: float = -1.0  # -1.0 means not evaluated
    jsonpath_reasons: str = ""
    transcript_pass: bool | None = None
    transcript_score: float = -1.0

    model_config = {"extra": "forbid"}


class HashGradingResult(BaseModel):
    """Result of hash-based grading."""

    hash_match: bool
    hash_score: float
    state_diff: StateDiff | None = None
    golden_action_errors: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}
