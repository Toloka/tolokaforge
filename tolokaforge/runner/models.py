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

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

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
        import re

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


class LLMJudgeConfig(BaseModel):
    """LLM-based grading configuration.

    Canonical home for the judge config that crosses both the YAML grading block
    and the gRPC wire (serialized inside ``TrialSpec.task.grading``). The
    ``output_schema`` field was dropped — the judge's structured-output schema is
    derived from the rubric's criteria (Stage 3), not author-specified. The
    judge *model* is no longer pinned here: it lives at the run level under
    ``RunConfig.models["judge"]`` and rides ``TrialSpec.judge_model_config``.
    """

    rubric: Rubric  # Structured grading rubric

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


class GradingConfig(BaseModel):
    """
    Complete grading configuration.

    Supports multiple methods combined with weights.
    """

    combine_method: Literal["weighted", "all_pass", "any_pass", "all"] = "weighted"
    weights: dict[str, float] = Field(default_factory=lambda: {"state_checks": 1.0})
    pass_threshold: float = 0.8

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

    state_checks: StateChecksConfig | None = None
    transcript_rules: TranscriptRulesConfig | None = None
    llm_judge: LLMJudgeConfig | None = None

    model_config = {"extra": "forbid"}


# =============================================================================
# EnvironmentManifest — typed schema for multi-service environments
# =============================================================================


HealthProbeKind = Literal["tcp", "http"]


class HealthProbe(BaseModel):
    """Readiness probe for a service, typed by protocol."""

    kind: HealthProbeKind
    """``"tcp"`` opens a socket; ``"http"`` issues a GET."""

    port: int
    """Container port to probe."""

    path: str | None = None
    """HTTP path. Required when ``kind == "http"``; must be unset for ``"tcp"``."""

    initial_delay_seconds: int = 0
    """Seconds to wait after container start before the first probe attempt."""

    interval_seconds: int = 5
    """Seconds between probe attempts."""

    timeout_seconds: int = 3
    """Seconds a single probe attempt may run before it counts as failed."""

    retries: int = 10
    """Failed attempts (after the initial delay) before the service is unhealthy."""

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _path_consistent_with_kind(self) -> HealthProbe:
        if self.kind == "http" and not self.path:
            raise ValueError("HealthProbe.path is required when kind == 'http'.")
        if self.kind == "tcp" and self.path is not None:
            raise ValueError("HealthProbe.path is not allowed when kind == 'tcp'.")
        return self


class PortSpec(BaseModel):
    """A container port declaration. The runtime backend assigns the host port."""

    container_port: int
    protocol: Literal["tcp", "udp"] = "tcp"

    model_config = {"extra": "forbid"}


VolumeKind = Literal["named", "bind"]


def _check_safe_relative_path(value: str, field_label: str) -> None:
    """Raise ``ValueError`` unless ``value`` is a non-empty relative path with
    no ``..`` segments. Used to keep manifest-declared paths inside the task
    pack root.

    POSIX-targeted (uses ``PurePosixPath``) because the runtime backends in
    scope (docker-compose Linux containers) interpret paths as POSIX. A
    Windows-style ``..\\escape`` is not flagged on POSIX; that is correct for
    the substrates we target, and would need a separate guard if the engine
    ever runs on a non-POSIX provisioner.
    """
    if not value:
        raise ValueError(f"{field_label} must be a non-empty path.")
    path = PurePosixPath(value)
    if path.is_absolute():
        raise ValueError(f"{field_label} must be a relative path; got absolute {value!r}.")
    if any(part == ".." for part in path.parts):
        raise ValueError(f"{field_label} must not contain '..' segments; got {value!r}.")


class VolumeMount(BaseModel):
    """A volume mounted into a service container."""

    kind: VolumeKind = "bind"
    """``"named"`` — ``source`` is a named volume identifier.
    ``"bind"`` — ``source`` is a path relative to the task pack root."""

    source: str
    """Volume name (when ``kind == "named"``) or path (when ``kind == "bind"``).
    Bind sources are validated as relative, ``..``-free paths."""

    target: str
    """Path inside the container."""

    read_only: bool = False

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _bind_source_stays_inside_task_pack(self) -> VolumeMount:
        if self.kind == "bind":
            _check_safe_relative_path(self.source, "VolumeMount.source (bind)")
        return self


class Resources(BaseModel):
    """Resource limits / requests as Kubernetes quantity strings."""

    cpu: str | None = None
    """E.g. ``"2"`` or ``"500m"``."""

    memory: str | None = None
    """E.g. ``"4Gi"`` or ``"512Mi"``."""

    model_config = {"extra": "forbid"}


InitialStateKind = Literal["sql", "copy", "script"]


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
    """Per-container security policy declarations."""

    run_as_user: int | None = None
    """UID the container process runs as. ``None`` defers to the image default."""

    run_as_group: int | None = None
    """GID the container process runs as. ``None`` defers to the image default."""

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


NetworkMode = Literal["isolated", "external"]


DependsOnCondition = Literal["service_started", "service_healthy"]


class DependsOn(BaseModel):
    """Service dependency with a wait condition.

    String entries in ``ServiceSpec.depends_on`` are shorthand for
    ``DependsOn(service=name, condition="service_started")``.
    """

    service: str
    """Name of another service declared in the same manifest."""

    condition: DependsOnCondition = "service_started"
    """``"service_started"`` waits only for the container to start.
    ``"service_healthy"`` also waits for the service's ``HealthProbe`` to pass."""

    model_config = {"extra": "forbid"}


_VALID_SERVICE_NAME_RE = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")
_MAX_DNS_LABEL_LENGTH = 63

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

# Accept the two algorithms in common use; reject malformed lengths and any
# unknown algorithm string. Hex is case-insensitive (Docker normalises to lower).
_IMAGE_DIGEST_RE = re.compile(r"^(?:sha256:[a-fA-F0-9]{64}|sha512:[a-fA-F0-9]{128})$")


def _image_tag_or_digest(image: str) -> tuple[str | None, str | None]:
    """Return ``(tag, digest)`` from a docker image reference.

    Either may be ``None``. The digest (if any) takes precedence — when the
    digest is set, the tag is reported as ``None`` even if a tag is also
    present in the reference.
    """
    if "@" in image:
        head, digest = image.rsplit("@", 1)
        return None, digest
    last_segment = image.rsplit("/", 1)[-1]
    if ":" not in last_segment:
        return None, None
    return last_segment.split(":", 1)[1], None


class ServiceSpec(BaseModel):
    """One service in an environment manifest."""

    name: str
    """Unique within the manifest. Lowercase DNS label up to 63 characters."""

    image: str
    """Container image with an immutable tag or a ``@sha256:`` digest.
    Floating tags (``:latest``, ``:main``, ``:edge``, ``:stable``, ``:dev``,
    ``:develop``, ``:nightly``, ``:head``) are rejected."""

    command: list[str] | None = None
    env: dict[str, str] = Field(default_factory=dict)
    ports: list[PortSpec] = Field(default_factory=list)
    volumes: list[VolumeMount] = Field(default_factory=list)
    depends_on: list[str | DependsOn] = Field(default_factory=list)
    """Other services this one depends on. String entries are shorthand for
    ``DependsOn(service=name, condition="service_started")``."""

    health: HealthProbe | None = None
    resources: Resources | None = None
    """Per-service overrides for the manifest-level ``resources`` defaults."""

    security_context: SecurityContext | None = None
    """Per-container security policy. ``None`` means the container runs with
    the runtime backend's default posture."""

    model_config = {"extra": "forbid"}

    @field_validator("name")
    @classmethod
    def _name_is_valid_dns_label(cls, v: str) -> str:
        if len(v) > _MAX_DNS_LABEL_LENGTH:
            raise ValueError(
                f"ServiceSpec.name must be at most {_MAX_DNS_LABEL_LENGTH} "
                f"characters (RFC 1123 DNS label); got {len(v)}."
            )
        if not _VALID_SERVICE_NAME_RE.fullmatch(v):
            raise ValueError(
                f"ServiceSpec.name must be a lowercase DNS label "
                f"(matching ^[a-z]([-a-z0-9]*[a-z0-9])?$); got {v!r}."
            )
        return v

    @field_validator("image")
    @classmethod
    def _image_is_pinned(cls, v: str) -> str:
        if not v:
            raise ValueError("ServiceSpec.image must not be empty.")
        tag, digest = _image_tag_or_digest(v)
        if digest is not None:
            if not _IMAGE_DIGEST_RE.fullmatch(digest):
                raise ValueError(
                    f"ServiceSpec.image digest must be a well-formed "
                    f"`sha256:<64-hex>` or `sha512:<128-hex>` reference; "
                    f"got {v!r}."
                )
            return v
        if tag is None:
            raise ValueError(
                f"ServiceSpec.image must include an explicit tag or " f"digest; got {v!r}."
            )
        if tag == "":
            raise ValueError(f"ServiceSpec.image tag must be non-empty; got {v!r}.")
        if tag.lower() in _FLOATING_IMAGE_TAGS:
            raise ValueError(
                f"ServiceSpec.image must be pinned to an immutable tag or "
                f"digest; floating tag {tag!r} is not deterministic."
            )
        return v


def _dep_service_name(dep: str | DependsOn) -> str:
    return dep if isinstance(dep, str) else dep.service


class EnvironmentManifest(BaseModel):
    """Typed declaration of a task's multi-service environment."""

    services: list[ServiceSpec]
    """Non-empty. The first entry is the default / runner service."""

    initial_state: dict[str, InitialStateRef] = Field(default_factory=dict)
    """Keyed by service name. Each value declares a fixture the provisioner
    applies to that service before the readiness gate."""

    resources: Resources | None = None
    """Manifest-level resource defaults applied when a service declares no
    ``resources`` of its own."""

    network: NetworkMode = "isolated"
    """Network posture the provisioner is asked to enforce.
    ``"isolated"`` — services reach each other on the per-trial network only;
    no path out to the public internet or to other trials.
    ``"external"`` — services may reach the public internet (still no path to
    other trials). Opt in explicitly; the safe default is ``"isolated"``."""

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _validate_cross_service_references(self) -> EnvironmentManifest:
        if not self.services:
            raise ValueError("EnvironmentManifest.services must be non-empty.")

        names = [s.name for s in self.services]
        seen: set[str] = set()
        for name in names:
            if name in seen:
                raise ValueError(f"EnvironmentManifest.services has duplicate name {name!r}.")
            seen.add(name)

        for service in self.services:
            for dep in service.depends_on:
                dep_name = _dep_service_name(dep)
                if dep_name not in seen:
                    raise ValueError(
                        f"ServiceSpec({service.name!r}).depends_on references "
                        f"unknown service {dep_name!r}; manifest declares "
                        f"{sorted(seen)!r}."
                    )

        for state_key in self.initial_state:
            if state_key not in seen:
                raise ValueError(
                    f"EnvironmentManifest.initial_state has key {state_key!r} "
                    f"that does not match any declared service; manifest "
                    f"declares {sorted(seen)!r}."
                )

        return self


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

    # --- Environment ---
    environment_manifest: EnvironmentManifest | None = None
    """Typed declaration of the task's multi-service environment. ``None``
    means the task does not declare one."""

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
    llm_judge_score: float = -1.0  # -1.0 means not evaluated
    llm_judge_reasons: str = ""

    model_config = {"extra": "forbid"}


class HashGradingResult(BaseModel):
    """Result of hash-based grading."""

    hash_match: bool
    hash_score: float
    state_diff: StateDiff | None = None
    golden_action_errors: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}
