"""
Runner gRPC Service Implementation

This module implements the RunnerServiceServicer as defined in docs/GRPC_PROTOCOL.md.
It provides the gRPC interface for Host ↔ Runner communication.

The service manages:
- Trial registration and lifecycle
- Tool execution routing
- Grading via golden path comparison
- State management via DB Service

Usage:
    db_client = DBServiceClient("http://db-service:8000")
    service = RunnerServiceImpl(db_client)
    add_RunnerServiceServicer_to_server(service, server)
"""

import asyncio
import inspect
import json
import logging
import shutil
import sys
import threading
import time
import traceback
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import grpc
from pydantic import ValidationError

from tolokaforge.core.grading.check_runner import (
    _CHECK_EXECUTOR_ERROR_NAME,
    CheckExecutor,
    CheckRunner,
    validate_checks_module,
)
from tolokaforge.core.grading.checks_helpers import build_check_context, custom_checks_enabled
from tolokaforge.core.grading.checks_interface import (
    CheckResult,
    CheckResultSet,
    CustomChecksConfig,
    TaskContext,
    ToolCallStatus,
    Transcript,
)
from tolokaforge.core.grading.checks_interface import (
    Message as CheckMessage,
)
from tolokaforge.core.grading.checks_interface import (
    ToolCall as CheckToolCall,
)
from tolokaforge.core.grading.grade_components import GRADE_COMPONENTS
from tolokaforge.core.grading.judge import JudgeResult, JudgeStatus, LLMJudge
from tolokaforge.core.grading.judge_tools import DelegatingReadTool
from tolokaforge.core.grading.kb_search import KnowledgeSearch, RagServiceKnowledgeSearch
from tolokaforge.core.grading.state_diff import render_state_diff
from tolokaforge.core.grading.trace_checks import evaluate_trace_checks
from tolokaforge.core.grading.trace_timeline import (
    TimelineInconsistencyError,
    TrialTimeline,
    build_trial_timeline,
)
from tolokaforge.core.grading.transcript_wire import (
    decode_transcript_wire,
    split_leading_system_message,
)
from tolokaforge.core.models import (
    CriterionResult,
    LLMJudgeConfig,
    ModelConfig,
    TerminationReason,
)
from tolokaforge.core.trial import DEFAULT_TOOL_TIMEOUT_S, TrialSpec
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner import runner_pb2_grpc
from tolokaforge.runner.capabilities import BUILTIN_ADAPTERS
from tolokaforge.runner.db_client import (
    DBServiceClient,
    DBServiceError,
)
from tolokaforge.runner.db_client import (
    TrialNotFoundError as DBTrialNotFoundError,
)
from tolokaforge.runner.grading import (
    build_grade_reasons,
    combine_grade_components,
    compute_state_diff,
    evaluate_db_probes,
    evaluate_jsonpath_checks,
    evaluate_transcript_rules,
    resolve_state_checks_component,
)
from tolokaforge.runner.grading_ledger import (
    CUSTOM_CHECKS_DISABLED_SKIP,
    CUSTOM_CHECKS_KEY,
    DB_PROBES_KEY,
    EVALUATED,
    HASH_DISABLED_SKIP,
    JSONPATHS_KEY,
    LLM_JUDGE_KEY,
    NO_JUDGE_MESSAGES_SKIP,
    NO_TIMELINE_EVENTS_SKIP,
    audit_accounted_keys,
    hash_family_accounting,
    transcript_rules_author_keys,
)
from tolokaforge.runner.id_resolution import (
    check_id_fields_reference_known_tables,
    compute_diff_ops,
)
from tolokaforge.runner.models import (
    GoldenAction,
    HashGradingResult,
    KeyAccountingRecord,
    RecordedToolCall,
    RunnerGradeComponents,
    StateDiff,
    TaskDescription,
    ToolExecutorIdentity,
    TraceChecksConfig,
    TraceChecksResult,
    TranscriptEvaluationResult,
    TranscriptRulesConfig,
)
from tolokaforge.runner.protocol import (
    ENGINE_PROTOCOL_VERSION,
    parse_termination_reason,
    recorded_status,
)
from tolokaforge.runner.rag_client import (
    RAGServiceClient,
    RAGServiceError,
    load_documents_from_directory,
)
from tolokaforge.runner.tool_factory import (
    DockerComposeExecToolWrapper,
    MCPServerToolWrapper,
    RAGSearchToolWrapper,
    ToolFactory,
    ToolLifecycleContext,
    ToolReconstructionError,
)
from tolokaforge.tools.registry import ToolExecutionStatus

logger = logging.getLogger(__name__)

# Service version
SERVICE_VERSION = "1.0.0"

# Session working root handed to lifecycle tools as ``ToolLifecycleContext.work_dir``.
AGENT_WORK_DIR = "/work"

# The documented read-only mcp_core TypeSense KB connector the agent uses. The
# judge is allowed to reuse this ONE reconstructed tool (read-only passthrough)
# so it reads the same corpus the agent did. It is a closed (mcp_core) tool, not
# a tolokaforge type, so it is matched by this documented name — instance
# detection is unavailable. This is the deliberate, narrow exception to the
# judge's "harness-owned allowlist" rule (no generic MCP-tool passthrough — we
# cannot classify arbitrary MCP tools' read-only-ness).
_SEARCH_POLICY_TOOL_NAME = "search_policy"


def _build_runner_check_transcript(
    llm_messages: list[dict[str, Any]],
) -> Transcript:
    """Build a :class:`Transcript` from the runner's wire ``llm_messages``.

    Wire ``tool_calls`` are OpenAI-shaped
    (``{"function": {"name", "arguments": <json_str>}}``); this decodes them
    back into :class:`ToolCall` with ``result=None`` (results are not carried in
    ``llm_messages_json``), mirroring the host-side transcript build so a check
    reads identical evidence from either grading path.
    """
    check_messages: list[CheckMessage] = []
    for msg in llm_messages:
        role = str(msg.get("role", ""))
        content = str(msg.get("content", "") or "")
        raw_tool_calls = msg.get("tool_calls") or []
        tool_calls: list[CheckToolCall] = []
        for raw_tc in raw_tool_calls:
            fn = raw_tc.get("function") or {}
            name = str(fn.get("name") or raw_tc.get("name") or "")
            raw_args: Any = fn.get("arguments", raw_tc.get("arguments"))
            if isinstance(raw_args, str):
                try:
                    args_dict = json.loads(raw_args) if raw_args else {}
                except (json.JSONDecodeError, TypeError):
                    args_dict = {}
            elif isinstance(raw_args, dict):
                args_dict = raw_args
            else:
                args_dict = {}
            tool_calls.append(
                CheckToolCall(
                    name=name,
                    arguments=args_dict,
                    result=None,
                    status=ToolCallStatus.SUCCESS,
                )
            )
        check_messages.append(CheckMessage(role=role, content=content, tool_calls=tool_calls))
    return Transcript(messages=check_messages)


def _check_result_to_wire(result: CheckResult) -> "pb2.CustomCheckResult":
    """Convert a :class:`CheckResult` to the wire ``pb2.CustomCheckResult``.

    ``details`` (arbitrary dict) is JSON-encoded into the proto's
    ``details_json`` string; empty when the check emitted no details.
    """
    status_str = result.status.value if hasattr(result.status, "value") else str(result.status)
    details_json = json.dumps(result.details) if result.details else ""
    return pb2.CustomCheckResult(
        check_name=result.check_name,
        status=status_str,
        score=result.score,
        message=result.message,
        details_json=details_json,
    )


def _executor_error_to_wire(error: str) -> "pb2.CustomCheckResult":
    """Wrap a top-level :class:`CheckResultSet` error as a wire result.

    The audit — module-load failure / timeout / executor crash — travels to
    the host under the reserved :data:`_CHECK_EXECUTOR_ERROR_NAME` sentinel
    so the reasons string is not the only place it survives.
    """
    return pb2.CustomCheckResult(
        check_name=_CHECK_EXECUTOR_ERROR_NAME,
        status="error",
        score=0.0,
        message=error,
        details_json="",
    )


# =============================================================================
# Trial Context - Per-trial runtime state (with tool callables)
# =============================================================================


class TrialContextRuntime:
    """
    Per-trial runtime state in the Runner.

    This holds all the information needed to execute tools and grade a trial,
    including the parsed task description, reconstructed tools, and execution history.

    Note: This is a runtime class (not Pydantic) because it holds callable objects
    that cannot be serialized.

    Attributes:
        trial_id: Unique trial identifier (e.g., "airline_task_001:0")
        task_description: Parsed TaskDescription model from RegisterTrial
        agent_tools: Map of tool name -> tool callable for agent tools
        user_tools: Map of tool name -> tool callable for user-side tools
        tool_call_history: The trial's ordered tool-call record
        default_timeout: Default timeout for tool execution in seconds
    """

    def __init__(
        self,
        trial_id: str,
        task_description: TaskDescription,
        default_timeout: float = 30.0,
        judge_model_config: ModelConfig | None = None,
    ):
        self.trial_id = trial_id
        self.task_description = task_description
        self.agent_tools: dict[str, Callable] = {}
        self.user_tools: dict[str, Callable] = {}
        self.tool_call_history: list[RecordedToolCall] = []
        self.default_timeout = default_timeout
        # Run-level LLM config for the read-only rubric judge, carried from the
        # TrialSpec. None when no selected task uses an llm_judge component; the
        # orchestrator validates up front that it is present whenever a rubric is.
        self.judge_model_config = judge_model_config
        # Per-trial KnowledgeSearch resolved at setup (the SAME index the agent's
        # KB tool used), or None when the agent had no KB tool this trial. The
        # judge is offered ``search_kb`` iff this is non-None — faithful gating.
        # Per-context state (not a process-global dict) because the runner is
        # concurrent across trials; this gives lifecycle for free and avoids
        # locking/leak. See the kb_search resolver methods below.
        self._kb_search: KnowledgeSearch | None = None

    def register_kb_search(self, impl: KnowledgeSearch) -> None:
        """Bind the per-trial :class:`KnowledgeSearch` resolved at trial setup."""
        self._kb_search = impl

    def resolve_kb_search(self) -> KnowledgeSearch | None:
        """Return the trial's :class:`KnowledgeSearch`, or None if none was resolved."""
        return self._kb_search

    def clear_kb_search(self) -> None:
        """Drop the per-trial KB backend (called at trial teardown)."""
        self._kb_search = None

    @property
    def grading_config(self):
        """Get grading config from task description."""
        return self.task_description.grading

    def get_tool(
        self, tool_name: str, executor: ToolExecutorIdentity = ToolExecutorIdentity.AGENT
    ) -> Callable | None:
        """
        Get a tool callable by name and executor type.

        Args:
            tool_name: Name of the tool
            executor: Which side of the dialogue is calling

        Returns:
            Tool callable or None if not found
        """
        if executor is ToolExecutorIdentity.USER:
            return self.user_tools.get(tool_name)
        return self.agent_tools.get(tool_name)

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
    ) -> None:
        """
        Record a tool call in the trial's ordered history.

        Satisfies :class:`~tolokaforge.runner.models.ToolCallRecorder`.
        ``sequence`` is stamped here, so no caller can supply a wrong index.

        Args:
            call_id: Provider tool-call id joining this call to its result
            tool_name: Name of the tool called
            arguments: Tool arguments, verbatim
            executor: Which side of the dialogue made the call
            status: How the call ended
            output: Tool output, or the rejection/failure text
            latency_seconds: Execution time
        """
        self.tool_call_history.append(
            RecordedToolCall(
                call_id=call_id,
                sequence=len(self.tool_call_history),
                tool_name=tool_name,
                arguments=arguments,
                executor=executor,
                output=output,
                status=status,
                latency_seconds=latency_seconds,
                timestamp=datetime.now(timezone.utc),
            )
        )

    @property
    def recorded(self) -> tuple[RecordedToolCall, ...]:
        """The trial's tool calls, in execution order."""
        return tuple(self.tool_call_history)

    def clear_history(self) -> None:
        """Clear tool call history (used on reset)."""
        self.tool_call_history.clear()


# =============================================================================
# Runner Service Implementation
# =============================================================================


class RunnerServiceImpl(runner_pb2_grpc.RunnerServiceServicer):
    """
    gRPC service implementation for the Runner.

    This service handles:
    - RegisterTrial: Initialize trial with TaskDescription
    - ExecuteTool: Execute tool calls from the LLM
    - GradeTrial: Compute grade via golden path comparison
    - GetState: Debug endpoint to inspect state
    - ResetTrial: Reset trial state for retries
    - CleanupTrial: Forget a trial's registration for retry-after-transient-failure paths
    - HealthCheck: Service health status

    The service maintains per-trial runtime state in TrialContextRuntime objects
    and delegates state storage to the DB Service via DBServiceClient.
    """

    def __init__(
        self,
        db_client: DBServiceClient,
        rag_client: RAGServiceClient | None = None,
        check_executor: CheckExecutor | None = None,
    ):
        """
        Initialize the Runner service.

        Args:
            db_client: HTTP client for DB Service communication
            rag_client: Optional RAG service client for search tools
            check_executor: Executor for the pack's ``checks.py``. Defaults to the
                in-process :class:`CheckRunner`; tests inject
                :class:`InMemoryCheckExecutor`.
        """
        self.db_client = db_client
        self.rag_client = rag_client
        self.check_executor: CheckExecutor = check_executor or CheckRunner()
        self.trials: dict[str, TrialContextRuntime] = {}
        self._available_adapters = list(BUILTIN_ADAPTERS)
        self._artifact_dirs: dict[str, Path] = {}  # trial_id -> temp dir for cleanup

        # Create a dedicated event loop thread for async operations.
        # gRPC runs each RPC handler in a ThreadPoolExecutor thread, which don't
        # have asyncio event loops. Using asyncio.run() or loop.run_until_complete()
        # creates/destroys loops per call, causing "Event loop is closed" errors
        # when httpx AsyncClient tries to use a closed loop.
        # Solution: A single long-lived event loop in a dedicated thread.
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_event_loop,
            daemon=True,
            name="runner-async-loop",
        )
        self._loop_thread.start()
        logger.info("Started dedicated event loop thread for async operations")

    def _run_event_loop(self) -> None:
        """Run the event loop forever in the dedicated thread."""
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run_async(self, coro, timeout: float = 300.0) -> Any:
        """
        Run an async coroutine on the dedicated event loop thread.

        This method is thread-safe and can be called from any gRPC handler thread.
        It submits the coroutine to the dedicated event loop and waits for the result.

        Args:
            coro: The coroutine to run
            timeout: Maximum time to wait for the result (default: 5 minutes)

        Returns:
            The result of the coroutine

        Raises:
            Any exception raised by the coroutine
        """
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            # Best-effort: release the orphaned coroutine instead of leaking it for
            # the loop's lifetime. cancel() is thread-safe for
            # run_coroutine_threadsafe futures; note it cannot interrupt a coroutine
            # already blocked inside a run_in_executor call (e.g. a wedged MCP
            # subprocess) — that deeper case is a separate concern. Reachable via the
            # search_policy judge bridge.
            future.cancel()
            raise

    def shutdown(self) -> None:
        """
        Shutdown the dedicated event loop thread and clean up temp directories.

        Call this when the service is being stopped to cleanly shut down
        the event loop and its thread.
        """
        # Clean up extracted artifact directories
        for trial_id, artifact_dir in self._artifact_dirs.items():
            try:
                # Remove extracted dir from sys.path
                dir_str = str(artifact_dir)
                if dir_str in sys.path:
                    sys.path.remove(dir_str)
                tools_str = str(artifact_dir / "tools")
                if tools_str in sys.path:
                    sys.path.remove(tools_str)
                shutil.rmtree(artifact_dir, ignore_errors=True)
                logger.debug(f"Cleaned up artifact dir for trial {trial_id}: {artifact_dir}")
            except Exception as e:
                logger.warning(f"Failed to clean up artifact dir {artifact_dir}: {e}")
        self._artifact_dirs.clear()

        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop_thread.join(timeout=5.0)
            logger.info("Stopped dedicated event loop thread")

    # =========================================================================
    # Tool artifact extraction
    # =========================================================================

    def _extract_tool_artifacts(self, trial_id: str, artifacts: dict[str, str]) -> Path:
        """Extract base64-encoded tool artifacts to a temp directory.

        Adds the temp directory to sys.path so tool modules can be imported.

        Args:
            trial_id: Trial identifier (for logging)
            artifacts: dict of {relative_path: base64_content}

        Returns:
            Path to the temp directory containing extracted files
        """
        import base64
        import tempfile

        safe_trial_id = trial_id.replace(":", "_").replace("/", "_")
        extract_dir = Path(tempfile.mkdtemp(prefix=f"tolokaforge-artifacts-{safe_trial_id}-"))

        for rel_path, b64_content in artifacts.items():
            out_path = extract_dir / rel_path
            out_path.parent.mkdir(parents=True, exist_ok=True)
            content = base64.b64decode(b64_content)
            out_path.write_bytes(content)

        # Add to sys.path so tool modules can be imported
        # Add the extract_dir itself (for packages like mcp_core/)
        extract_str = str(extract_dir)
        if extract_str not in sys.path:
            sys.path.insert(0, extract_str)

        # Also add tools/ subdirectory if it exists (some adapter layouts place
        # their tool packages under a 'tools/' folder rather than the artifact root)
        tools_dir = extract_dir / "tools"
        if tools_dir.exists():
            tools_str = str(tools_dir)
            if tools_str not in sys.path:
                sys.path.insert(0, tools_str)

        # Track for cleanup
        self._artifact_dirs[trial_id] = extract_dir

        logger.info(
            "Extracted %d artifacts to %s, added to sys.path",
            len(artifacts),
            extract_dir,
        )

        return extract_dir

    def _resolve_mcp_server_scripts(
        self, task_description: "TaskDescription", artifacts_dir: Path
    ) -> None:
        """Rewrite relative mcp_server_script paths to absolute paths.

        NativeAdapter stores a relative filename (e.g. ``"mcp_server.py"``) in
        ``ToolSource.mcp_server_script`` so the TaskDescription is portable.
        After artifacts are extracted to *artifacts_dir* we resolve every
        ``MCP_SERVER``-style tool's script path to an absolute one that the
        subprocess launcher can use directly.

        Mutates ``task_description.agent_tools`` and
        ``task_description.user_tools`` in-place.
        """
        from tolokaforge.runner.models import InvocationStyle

        for tool_schema in task_description.agent_tools + task_description.user_tools:
            source = tool_schema.source
            if (
                source is not None
                and source.invocation_style == InvocationStyle.MCP_SERVER
                and source.mcp_server_script
                and not Path(source.mcp_server_script).is_absolute()
            ):
                resolved = artifacts_dir / source.mcp_server_script
                source.mcp_server_script = str(resolved)
                logger.debug(
                    "Resolved mcp_server_script",
                    tool=tool_schema.name,
                    path=source.mcp_server_script,
                )

    def _cleanup_trial_artifacts(self, trial_id: str) -> None:
        """Clean up extracted artifacts for a completed trial."""
        artifact_dir = self._artifact_dirs.pop(trial_id, None)
        if artifact_dir is None:
            return
        try:
            dir_str = str(artifact_dir)
            if dir_str in sys.path:
                sys.path.remove(dir_str)
            tools_str = str(artifact_dir / "tools")
            if tools_str in sys.path:
                sys.path.remove(tools_str)
            shutil.rmtree(artifact_dir, ignore_errors=True)
            logger.debug(f"Cleaned up artifact dir for trial {trial_id}: {artifact_dir}")
        except Exception as e:
            logger.warning(f"Failed to clean up artifact dir {artifact_dir}: {e}")

    def _validate_custom_checks_startup(
        self,
        trial_id: str,
        task_description: "TaskDescription",
        artifacts_dir: Path | None,
    ) -> str | None:
        """Fail-loud validation of ``custom_checks`` before the trial runs.

        Returns ``None`` when the pack has no ``custom_checks`` config, has it
        disabled, or the config validates and ``checks.py`` loads with a
        supported ``interface_version``. Returns a human-readable error string
        naming the offending version and :data:`SUPPORTED_VERSIONS` (or the
        module-load failure) when the pack claims custom checks but the module
        cannot be loaded — the caller turns that into a
        ``RegisterTrialResponse(success=False, error=…)``.
        """
        grading = task_description.grading
        custom_checks_raw = grading.custom_checks if grading else None
        try:
            if not custom_checks_enabled(custom_checks_raw):
                return None
            custom_config = CustomChecksConfig(**custom_checks_raw)
        except ValidationError as exc:
            logger.error(f"RegisterTrial: {trial_id} - invalid custom_checks config: {exc}")
            return f"Invalid custom_checks config: {exc}"

        if artifacts_dir is None:
            error = (
                f"custom_checks.enabled but no tool_artifacts were delivered "
                f"(expected `{custom_config.file}` under the trial's artifacts dir)"
            )
            logger.error(f"RegisterTrial: {trial_id} - {error}")
            return error

        checks_file = artifacts_dir / custom_config.file
        try:
            validate_checks_module(
                checks_file=checks_file,
                task_dir=artifacts_dir,
                config=custom_config,
            )
        except ValueError as exc:
            logger.error(f"RegisterTrial: {trial_id} - custom_checks validation failed: {exc}")
            return f"custom_checks validation failed: {exc}"

        logger.info(
            f"RegisterTrial: {trial_id} - custom_checks validated "
            f"(interface_version={custom_config.interface_version}, file={custom_config.file})"
        )
        return None

    # =========================================================================
    # RegisterTrial - Initialize trial with TaskDescription
    # =========================================================================

    def RegisterTrial(
        self,
        request: pb2.RegisterTrialRequest,
        context: grpc.ServicerContext,
    ) -> pb2.RegisterTrialResponse:
        """
        Register a new trial from a serialised TrialSpec.

        Host sends TrialSpec JSON; the Runner reads ``spec.task`` and
        initialises the environment:
        1. Reject an engine whose wire-protocol version this runner cannot serve
        2. Validate the full TrialSpec into a Pydantic model (fail fast on invalid)
        3. Initialize DB Service with initial_state, schemas, unstable_fields (fail fast)
        4. Reconstruct tools from ToolSource definitions (fail fast)
        5. Return tool schemas for LLM configuration

        Args:
            request: RegisterTrialRequest with trial_id and trial_spec_json
            context: gRPC context

        Returns:
            RegisterTrialResponse with success status and tool schemas
        """
        trial_id = request.trial_id
        logger.info(f"RegisterTrial: {trial_id}")

        if request.engine_protocol_version < ENGINE_PROTOCOL_VERSION:
            error = (
                f"engine declares wire-protocol version {request.engine_protocol_version}, "
                f"this runner image requires at least {ENGINE_PROTOCOL_VERSION}: the engine "
                "and the runner image are version-skewed. Rebuild the runner image from this "
                "engine (make docker-build-core) or pin an image tag that matches it."
            )
            logger.error(f"RegisterTrial: {trial_id} - {error}")
            return pb2.RegisterTrialResponse(success=False, error=error)

        try:
            trial_spec = TrialSpec.model_validate_json(request.trial_spec_json)
        except ValidationError as e:
            logger.error(f"Failed to validate trial_spec_json: {e}")
            return pb2.RegisterTrialResponse(
                success=False,
                error=f"Invalid trial_spec_json: {e}",
            )

        task_description = trial_spec.task

        # Extract tool artifacts to temp directory if present
        artifacts_dir = None
        if task_description.tool_artifacts:
            artifacts_dir = self._extract_tool_artifacts(trial_id, task_description.tool_artifacts)
            logger.info(
                f"Extracted {len(task_description.tool_artifacts)} tool artifacts "
                f"to {artifacts_dir}"
            )
            # Resolve relative mcp_server_script paths to absolute paths inside
            # the extracted artifacts directory.  NativeAdapter stores only the
            # relative filename (e.g. "mcp_server.py") so the TaskDescription
            # stays portable across machines; the Runner fixes up the path here.
            self._resolve_mcp_server_scripts(task_description, artifacts_dir)

        # Reject an unsupported ``interface_version`` or a broken ``checks.py``
        # BEFORE DB init, tool reconstruction, and the agent loop — the load
        # cost of validation is bounded, the cost of running a trial to grade
        # time only to reject on version isn't.
        custom_checks_error = self._validate_custom_checks_startup(
            trial_id, task_description, artifacts_dir
        )
        if custom_checks_error is not None:
            # Extraction ran before validation, so a failing config leaves the
            # tmp dir on disk + on ``sys.path``. Clean up before returning so a
            # client-side retry does not compound the leak (or shadow later
            # imports of the same relative path from an unrelated trial).
            self._cleanup_trial_artifacts(trial_id)
            return pb2.RegisterTrialResponse(success=False, error=custom_checks_error)

        # Initialise mcp_core TypeSense registry so search_policy tools work.
        # Documents are already indexed by the host-side adapter; we just
        # register a client inside this container pointing at the same server.
        #
        # Gated on ``host`` (TypeSense is configured) ONLY — independent of
        # ``enabled``. ``enabled`` now means just "this task needs rag-service"
        # (it gates the RAG indexing block below); it does NOT govern TypeSense.
        # A TypeSense-only domain therefore sets ``enabled=False`` + ``host=…``:
        # TypeSense inits here, the RAG block is skipped (no rag_client needed).
        search_config = task_description.search
        if search_config and search_config.host:
            self._init_typesense_for_trial(search_config, artifacts_dir)

        # Create trial context with validated TaskDescription
        trial_context = TrialContextRuntime(
            trial_id=trial_id,
            task_description=task_description,
            default_timeout=request.default_tool_timeout_s or DEFAULT_TOOL_TIMEOUT_S,
            judge_model_config=trial_spec.judge_model_config,
        )

        # Initialize DB Service with initial_state — only when the trial
        # actually declares DB state to initialise. Tasks that use no DB
        # tables / schemas / unstable_fields (e.g. adapters that grade via
        # `custom_checks` + an HTTP endpoint on a sidecar, and drive no
        # runner-managed state) skip the DB call entirely, so the runner
        # provisions cleanly even when no `db-service` sits in the trial's
        # compose stack. Matches the guard the state-diff renderer already
        # applies at `_maybe_render_state_diff` (checks `not initial_state.tables`).
        # FAIL FAST is preserved for trials that DO declare DB state.
        initial_state = task_description.initial_state
        needs_db = bool(
            initial_state.tables or initial_state.schemas or initial_state.unstable_fields
        )
        if needs_db:
            try:
                # Run async operation on dedicated event loop thread
                self._run_async(
                    self.db_client.init_trial(
                        trial_id=trial_id,
                        tables=initial_state.tables,
                        schemas=[s.model_dump() for s in initial_state.schemas],
                        unstable_fields=[u.model_dump() for u in initial_state.unstable_fields],
                    )
                )
                logger.info(f"RegisterTrial: {trial_id} - DB Service initialized")
            except Exception as e:
                # FAIL FAST: DB init failure is a critical error
                logger.error(f"RegisterTrial: Failed to initialize DB Service: {e}")
                return pb2.RegisterTrialResponse(
                    success=False,
                    error=f"DB Service initialization failed: {e}",
                )
        else:
            logger.info(
                f"RegisterTrial: {trial_id} - no DB state declared; skipping DB Service init"
            )

        # Provision initial filesystem files (from initial_state.filesystem).
        #
        # Logical paths from task configs (``/env/fs/agent-visible/X``) are
        # translated to the runner container's actual agent-visible directory
        # (``/work/X``) so the agent's bash / read_file / write_file tools all
        # find the file at the same place. ``/work`` is the BashTool default
        # workdir and the base_path for the file tools.
        if initial_state.filesystem:
            base_dir = Path("/work")
            for dest_path, content in initial_state.filesystem.items():
                # Translate logical path to runner-local /work/ path.
                if dest_path.startswith("/env/fs/agent-visible/"):
                    rel = dest_path[len("/env/fs/agent-visible/") :]
                    file_path = base_dir / rel
                elif dest_path == "/env/fs/agent-visible":
                    # Bare directory — nothing to materialise; skip.
                    continue
                elif dest_path.startswith("/work/"):
                    file_path = Path(dest_path)
                elif dest_path.startswith("/"):
                    # Other absolute path — write literally (escape hatch).
                    file_path = Path(dest_path)
                else:
                    file_path = base_dir / dest_path
                try:
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    file_path.write_text(content, encoding="utf-8")
                    logger.info(f"RegisterTrial: {trial_id} - Provisioned file: {file_path}")
                except Exception as e:
                    logger.error(f"RegisterTrial: Failed to provision file {dest_path}: {e}")
                    return pb2.RegisterTrialResponse(
                        success=False,
                        error=f"Filesystem provisioning failed for {dest_path}: {e}",
                    )
            logger.info(
                f"RegisterTrial: {trial_id} - "
                f"Provisioned {len(initial_state.filesystem)} filesystem file(s)"
            )

        # Initialize RAG service if search is enabled (FAIL FAST).
        # ``enabled`` means the task needs rag-service; on the core stack
        # (no rag-service ⇒ rag_client is None) this hard-fails ON PURPOSE.
        search_config = task_description.search
        rag_client_for_trial = None
        if search_config and search_config.enabled:
            if self.rag_client is None:
                logger.error("RegisterTrial: Search enabled but RAG client not configured")
                return pb2.RegisterTrialResponse(
                    success=False,
                    error="Search enabled but RAG service not configured",
                )

            # Index documents for this trial
            try:
                self._run_async(
                    self._index_documents_for_trial(
                        trial_id=trial_id,
                        search_config=search_config,
                        artifacts_dir=artifacts_dir,
                    )
                )
                rag_client_for_trial = self.rag_client
                logger.info(f"RegisterTrial: {trial_id} - RAG documents indexed")
            except RAGServiceError as e:
                logger.error(f"RegisterTrial: Failed to index documents: {e}")
                return pb2.RegisterTrialResponse(
                    success=False,
                    error=f"RAG indexing failed: {e}",
                )

        # Reconstruct tools from ToolSource definitions (FAIL FAST)
        # Pass actual table names and data from initial_state so model registration uses correct names
        db_table_names = list(initial_state.tables.keys()) if initial_state.tables else []
        initial_state_data = initial_state.tables if initial_state.tables else {}
        # Per-table primary-key overrides from grading config (default "id"). Passed to
        # the DB proxy so upsert/delete/lookup key resolution is data-driven rather than
        # introspecting model source (which fails when the domain source is not on disk).
        state_checks = task_description.grading.state_checks if task_description.grading else None
        id_fields = dict(state_checks.id_fields) if state_checks else {}
        relaxed = bool(state_checks.relaxed_validation) if state_checks else False
        # Belt-and-suspenders: NativeAdapter runs this check at task-description build
        # time, but engines using other adapters (mcp_core, custom) bypass it.
        err = check_id_fields_reference_known_tables(
            id_fields, db_table_names, context=f"RegisterTrial: {trial_id}", relaxed=relaxed
        )
        if err:
            logger.error(err)
            return pb2.RegisterTrialResponse(success=False, error=err)
        try:
            tool_factory = ToolFactory(
                self.db_client,
                trial_id,
                rag_client_for_trial,
                db_table_names,
                initial_state_data,
                id_fields=id_fields,
            )

            # Set domain on DB proxy so search_policy tools can resolve
            # the TypeSense client via db.domain → get_typesense_for_domain().
            if search_config and search_config.domain_name:
                tool_factory._sync_proxy.domain = search_config.domain_name

            reconstructed = tool_factory.reconstruct_tools(
                agent_tools=[t.model_dump() for t in task_description.agent_tools],
                user_tools=[t.model_dump() for t in task_description.user_tools],
            )

            # Store reconstructed tools in trial context
            trial_context.agent_tools = dict(reconstructed.agent_tools.items())
            trial_context.user_tools = dict(reconstructed.user_tools.items())

            kb_search = self._resolve_judge_kb_search(trial_id, trial_context.agent_tools)
            if kb_search is not None:
                trial_context.register_kb_search(kb_search)

            logger.info(
                f"RegisterTrial: {trial_id} - Reconstructed "
                f"{len(reconstructed.agent_tools)} agent tools, "
                f"{len(reconstructed.user_tools)} user tools"
            )
        except ToolReconstructionError as e:
            # FAIL FAST: Tool reconstruction failure is a critical error
            logger.error(f"RegisterTrial: Failed to reconstruct tools: {e}")
            return pb2.RegisterTrialResponse(
                success=False,
                error=f"Tool reconstruction failed: {e}",
            )
        except Exception as e:
            # FAIL FAST: Any other error during tool reconstruction
            logger.error(f"RegisterTrial: Unexpected error reconstructing tools: {e}")
            return pb2.RegisterTrialResponse(
                success=False,
                error=f"Tool reconstruction failed: {e}",
            )

        # Store trial context
        self.trials[trial_id] = trial_context

        # Start any tools that manage per-trial resources. Driven by the tool's
        # ``has_lifecycle`` capability, not by adapter identity, so any lifecycle
        # tool (e.g. a compose-backed sandbox) is provisioned the same way.
        lifecycle_ctx = ToolLifecycleContext(
            trial_id=trial_id,
            artifacts_dir=str(artifacts_dir) if artifacts_dir is not None else None,
            work_dir=AGENT_WORK_DIR,
        )
        for tool in trial_context.agent_tools.values():
            if getattr(tool, "has_lifecycle", False):
                try:
                    tool.start(lifecycle_ctx)
                except Exception as e:
                    logger.error(f"RegisterTrial: Failed to start tool lifecycle: {e}")
                    return pb2.RegisterTrialResponse(
                        success=False,
                        error=f"Tool lifecycle start failed: {e}",
                    )

        # Build tool schemas for response
        tool_schemas = []
        for tool in task_description.agent_tools:
            schema = pb2.ToolSchema(
                name=tool.name,
                description=tool.description,
                parameters_json=json.dumps(tool.parameters),
                category=tool.category,
                timeout_s=tool.timeout_s,
            )
            tool_schemas.append(schema)

        for tool in task_description.user_tools:
            schema = pb2.ToolSchema(
                name=tool.name,
                description=tool.description,
                parameters_json=json.dumps(tool.parameters),
                category=tool.category,
                timeout_s=tool.timeout_s,
            )
            tool_schemas.append(schema)

        logger.info(
            f"RegisterTrial: {trial_id} - {len(task_description.agent_tools)} agent tools, "
            f"{len(task_description.user_tools)} user tools"
        )

        return pb2.RegisterTrialResponse(
            success=True,
            error="",
            tool_schemas=tool_schemas,
            num_agent_tools=len(task_description.agent_tools),
            num_user_tools=len(task_description.user_tools),
        )

    # =========================================================================
    # ExecuteTool - Execute a single tool call
    # =========================================================================

    def ExecuteTool(
        self,
        request: pb2.ExecuteToolRequest,
        context: grpc.ServicerContext,
    ) -> pb2.ExecuteToolResponse:
        """
        Execute a tool call from the LLM.

        Host forwards tool call, Runner executes and returns output:
        1. Look up trial context
        2. Find tool by name in agent_tools or user_tools
        3. Execute tool with arguments
        4. Record tool call in history
        5. Return output or error

        A call the runner refuses before execution — unparseable arguments, an
        unknown tool name — is still recorded, because the host appends a
        ``role: tool`` error message for it either way and a rejected call the
        record omits reads as a call that was never attempted.

        Args:
            request: ExecuteToolRequest with trial_id, tool_name, arguments_json
            context: gRPC context

        Returns:
            ExecuteToolResponse with status, output, and metrics

        Raises:
            ValueError: ``call_id`` is empty. Registered engines declare a
                protocol version that carries it and ``ToolCall.id`` rejects an
                empty id at message construction, so this is a harness bug rather
                than version skew. Aborting the RPC keeps it out of the
                non-success statuses, where it would be indistinguishable from
                the model emitting malformed arguments. It does still reach the
                agent as a tool failure: the host's
                ``GrpcRunnerClient.execute_tool`` turns any ``grpc.RpcError``
                into a failed ``ToolResult``. What the raise buys is the cause,
                named in the runner log.
        """
        trial_id = request.trial_id
        tool_name = request.tool_name
        executor = ToolExecutorIdentity(request.executor or ToolExecutorIdentity.AGENT.value)
        call_id = request.call_id

        logger.debug(f"ExecuteTool: {trial_id} - {tool_name} ({executor.value}) call_id={call_id}")

        if not call_id:
            raise ValueError(
                f"ExecuteTool for tool {tool_name!r} on trial {trial_id!r} carries no call_id. "
                "The id joins the call to the tool result it produced; without it two calls "
                "to the same tool with identical arguments are indistinguishable."
            )

        # Check if trial exists. Unrecordable by construction — there is no
        # trial context to record into — so this stays message-only.
        if trial_id not in self.trials:
            logger.warning(f"ExecuteTool: Trial not found: {trial_id}")
            return pb2.ExecuteToolResponse(
                status=pb2.EXECUTION_STATUS_TRIAL_NOT_FOUND,
                output="",
                error_message=f"Trial '{trial_id}' not found",
                metrics=pb2.ToolMetrics(),
            )

        trial_context = self.trials[trial_id]

        # Parse arguments
        try:
            arguments = json.loads(request.arguments_json) if request.arguments_json else {}
        except json.JSONDecodeError as e:
            logger.warning(f"ExecuteTool: Invalid arguments JSON: {e}")
            return self._reject_tool_call(
                trial_context=trial_context,
                call_id=call_id,
                tool_name=tool_name,
                arguments={},
                executor=executor,
                status=pb2.EXECUTION_STATUS_INVALID_ARGUMENTS,
                error_message=f"Invalid arguments JSON: {e}",
            )

        # Look up tool in the appropriate tool set
        tool = trial_context.get_tool(tool_name, executor)
        if tool is None:
            logger.warning(f"ExecuteTool: Tool not found: {tool_name} ({executor.value})")
            return self._reject_tool_call(
                trial_context=trial_context,
                call_id=call_id,
                tool_name=tool_name,
                arguments=arguments,
                executor=executor,
                status=pb2.EXECUTION_STATUS_TOOL_NOT_FOUND,
                error_message=f"Tool '{tool_name}' not found in {executor.value} tools",
            )

        # Determine timeout
        timeout_seconds = request.timeout_seconds
        if timeout_seconds <= 0:
            # Use tool-specific timeout or default
            timeout_seconds = getattr(tool, "timeout_s", trial_context.default_timeout)

        # Run async execution on dedicated event loop thread
        try:
            result = self._run_async(
                self._execute_tool_async(
                    trial_context=trial_context,
                    tool=tool,
                    call_id=call_id,
                    tool_name=tool_name,
                    arguments=arguments,
                    executor=executor,
                    timeout_seconds=timeout_seconds,
                )
            )
            return result
        except Exception as e:
            # This should not happen - _execute_tool_async catches all exceptions
            logger.error(f"ExecuteTool: Unexpected error in async execution: {e}")
            logger.error(traceback.format_exc())
            return pb2.ExecuteToolResponse(
                status=pb2.EXECUTION_STATUS_ERROR,
                output="",
                error_message=f"Internal error: {type(e).__name__}",
                metrics=pb2.ToolMetrics(),
            )

    def _reject_tool_call(
        self,
        trial_context: TrialContextRuntime,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        executor: ToolExecutorIdentity,
        status: int,
        error_message: str,
    ) -> pb2.ExecuteToolResponse:
        """Record a call the runner refused before execution and return its response."""
        trial_context.record(
            call_id=call_id,
            tool_name=tool_name,
            arguments=arguments,
            output=error_message,
            status=recorded_status(status),
            executor=executor,
            latency_seconds=0.0,
        )
        return pb2.ExecuteToolResponse(
            status=status,
            output="",
            error_message=error_message,
            metrics=pb2.ToolMetrics(),
        )

    async def _execute_tool_async(
        self,
        trial_context: TrialContextRuntime,
        tool: Any,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        executor: ToolExecutorIdentity,
        timeout_seconds: float,
    ) -> pb2.ExecuteToolResponse:
        """
        Async implementation of tool execution with timeout and error handling.

        This method:
        1. Executes the tool with timeout enforcement
        2. Records the tool call in history
        3. Returns appropriate response based on outcome

        Tool execution errors are caught and returned as ERROR status,
        never propagated to crash the Runner.
        """
        start_time = time.time()
        output = ""
        status = pb2.EXECUTION_STATUS_SUCCESS
        error_message = ""

        try:
            # Execute tool with timeout
            # Tool wrappers have async execute(arguments) -> str method
            if hasattr(tool, "execute"):
                # ToolWrapper interface
                result = await asyncio.wait_for(
                    tool.execute(arguments),
                    timeout=timeout_seconds,
                )
            elif callable(tool):
                # Direct callable (for testing or simple tools)
                if inspect.iscoroutinefunction(tool):
                    result = await asyncio.wait_for(
                        tool(arguments),
                        timeout=timeout_seconds,
                    )
                else:
                    # Sync callable - run in executor
                    loop = asyncio.get_event_loop()
                    result = await asyncio.wait_for(
                        loop.run_in_executor(None, lambda: tool(arguments)),
                        timeout=timeout_seconds,
                    )
            else:
                raise TypeError(f"Tool {tool_name} is not callable")

            # Convert result to string
            if isinstance(result, str):
                output = result
            elif result is None:
                output = "Success"
            else:
                output = json.dumps(result, default=str)

            status = pb2.EXECUTION_STATUS_SUCCESS
            logger.debug(f"ExecuteTool: {tool_name} completed successfully")

        except asyncio.TimeoutError:
            status = pb2.EXECUTION_STATUS_TIMEOUT
            error_message = f"Tool execution timed out after {timeout_seconds}s"
            logger.warning(f"ExecuteTool: {tool_name} timed out after {timeout_seconds}s")

        except Exception as e:
            # Catch all exceptions from tool execution
            status = pb2.EXECUTION_STATUS_ERROR
            # Sanitize error message - don't expose internal details
            error_message = f"Tool error: {type(e).__name__}: {str(e)}"
            logger.error(f"ExecuteTool: {tool_name} raised exception: {e}")
            logger.error(traceback.format_exc())

        # Calculate latency
        latency_seconds = time.time() - start_time

        # Record tool call in history
        trial_context.record(
            call_id=call_id,
            tool_name=tool_name,
            arguments=arguments,
            output=output if status == pb2.EXECUTION_STATUS_SUCCESS else error_message,
            status=recorded_status(status),
            executor=executor,
            latency_seconds=latency_seconds,
        )

        # Build response
        return pb2.ExecuteToolResponse(
            status=status,
            output=output,
            error_message=error_message,
            metrics=pb2.ToolMetrics(
                latency_seconds=latency_seconds,
                exit_code=0 if status == pb2.EXECUTION_STATUS_SUCCESS else 1,
                state_mutations=0,  # TODO: Track state mutations if needed
            ),
        )

    # =========================================================================
    # GradeTrial - Compute grade for completed trial
    # =========================================================================

    def GradeTrial(
        self,
        request: pb2.GradeTrialRequest,
        context: grpc.ServicerContext,
    ) -> pb2.GradeTrialResponse:
        """
        Grade the completed trial.

        Host sends trajectory, Runner computes grade via golden path comparison:
        1. Get current trial state hash
        2. Snapshot current state
        3. Reset to initial state
        4. Execute golden path actions
        5. Get golden state hash
        6. Restore trial state
        7. Compare hashes and compute score

        Args:
            request: GradeTrialRequest with trial_id, optional llm_messages_json
                and optional termination_reason
            context: gRPC context

        Returns:
            GradeTrialResponse with success status and grade
        """
        trial_id = request.trial_id
        logger.info(f"GradeTrial: {trial_id}")

        # Check if trial exists
        if trial_id not in self.trials:
            logger.warning(f"GradeTrial: Trial not found: {trial_id}")
            return pb2.GradeTrialResponse(
                success=False,
                error=f"Trial '{trial_id}' not found",
            )

        # Run async grading on dedicated event loop thread. The 600s budget (up
        # from 300s) accommodates the runner-side rubric judge, which runs its own
        # multi-turn LLM loop; the judge has its own internal max_turns + wall-time
        # budget and fails loud well within this outer ceiling.
        try:
            result = self._run_async(self._grade_trial_async(request), timeout=600.0)
            return result
        except Exception as e:
            logger.error(f"GradeTrial: Unexpected error: {e}")
            logger.error(traceback.format_exc())
            return pb2.GradeTrialResponse(
                success=False,
                error=f"Grading error: {type(e).__name__}: {str(e)}",
            )

    async def _grade_trial_async(self, request: pb2.GradeTrialRequest) -> pb2.GradeTrialResponse:
        """
        Async implementation of GradeTrial.

        Implements the grading algorithm from docs/GRPC_PROTOCOL.md:
        A) Hash-based grading (if golden_actions exist)
        B) Transcript rules grading (if transcript_rules exist)
        C) Accounted-keys ledger — fail loud on a populated scored key nothing read
        D) Combine scores
        """
        trial_id = request.trial_id
        trial_context = self.trials[trial_id]

        try:
            termination_reason = parse_termination_reason(request.termination_reason)
        except ValueError as e:
            logger.error(f"GradeTrial: {trial_id} - {e}")
            return pb2.GradeTrialResponse(success=False, error=str(e))

        grading_config = trial_context.grading_config

        # Declarative grading-method dispatch (no adapter identity): a task can request
        # test-execution grading — run a reference suite in the env and score it —
        # instead of the default state/transcript/judge combination.
        if grading_config and grading_config.grading_method == "test_execution":
            return await self._grade_via_test_execution(trial_id, trial_context)

        # Initialize grading components
        components = RunnerGradeComponents()
        state_diff: StateDiff | None = None
        transcript_result: TranscriptEvaluationResult | None = None
        hash_result: HashGradingResult | None = None
        # Author key -> what became of it, filled in below at the points an
        # evaluator is invoked or deliberately skipped. audit_accounted_keys
        # subtracts it from what the config populated.
        accounted_keys: dict[str, KeyAccountingRecord] = {}

        # Edge case: No grading config at all → pass by default
        if grading_config is None:
            logger.info(f"GradeTrial: {trial_id} - No grading config, passing by default")
            return pb2.GradeTrialResponse(
                success=True,
                error="",
                grade=pb2.Grade(
                    binary_pass=True,
                    score=1.0,
                    components=pb2.GradeComponents(
                        state_checks=-1.0,
                        transcript_rules=-1.0,
                        llm_judge=-1.0,
                        custom_checks=-1.0,
                    ),
                    reasons="No grading config - passed by default",
                    state_diff_json="",
                ),
            )

        # The trial's two views of itself, joined before any component runs: a
        # payload that cannot be reconciled with the tool-call record fails the
        # RPC rather than being graded around.
        try:
            llm_messages, timeline = self._grade_time_views(
                request, trial_context, termination_reason
            )
        except (ValueError, TimelineInconsistencyError) as exc:
            logger.error(f"GradeTrial: {trial_id} - {type(exc).__name__}: {exc}")
            return pb2.GradeTrialResponse(
                success=False,
                error=f"Trial {trial_id!r} is not gradeable: {type(exc).__name__}: {exc}",
            )

        # Get state_checks config (may contain golden_actions)
        state_checks_config = grading_config.state_checks
        golden_actions: list[GoldenAction] = []
        if state_checks_config:
            golden_actions = state_checks_config.golden_actions

        # A) HASH-BASED GRADING
        # Run hash grading when hash_enabled is set (even with empty golden_actions,
        # which represents refusal tasks where the expected state == initial state).
        if state_checks_config and state_checks_config.hash_enabled:
            logger.info(
                f"GradeTrial: {trial_id} - Executing hash-based grading with {len(golden_actions)} golden actions"
            )
            try:
                hash_result = await self._execute_hash_grading(
                    trial_id,
                    trial_context,
                    golden_actions,
                    numeric_string_fields=state_checks_config.numeric_string_fields,
                )
                components.hash_match = hash_result.hash_match
                components.hash_score = hash_result.hash_score
                state_diff = hash_result.state_diff
                accounted_keys.update(hash_family_accounting(EVALUATED))
            except Exception as e:
                logger.error(f"GradeTrial: Hash grading failed: {e}")
                logger.error(traceback.format_exc())
                # Hash grading failure is a grading error
                return pb2.GradeTrialResponse(
                    success=False,
                    error=f"Hash grading failed: {type(e).__name__}: {str(e)}",
                )
        elif state_checks_config:
            # `hash:` keys still arrive populated with no evaluator to consume
            # them — the adapter fills golden_actions whether or not
            # `enabled: true` is set.
            accounted_keys.update(hash_family_accounting(HASH_DISABLED_SKIP))

        # A.2) JSONPATH ASSERTIONS (if jsonpath_checks exist)
        if state_checks_config and state_checks_config.jsonpath_checks:
            logger.info(
                f"GradeTrial: {trial_id} - Evaluating "
                f"{len(state_checks_config.jsonpath_checks)} jsonpath checks"
            )
            # Only fetch DB state when at least one assertion targets it via
            # ``path:``. File-only (``path_glob:``) checks never needed the DB,
            # so fetching unconditionally would add a round-trip and a new
            # failure mode for tasks that previously graded without it.
            jsonpath_checks = state_checks_config.jsonpath_checks
            jsonpath_state = None
            if any(check.get("path") is not None for check in jsonpath_checks):
                stable_state_response = await self.db_client.get_stable_state(trial_id)
                jsonpath_state = {
                    "db": stable_state_response.data,
                    "tables": stable_state_response.data,
                }
            jsonpath_score, jsonpath_reasons = evaluate_jsonpath_checks(
                jsonpath_checks,
                state=jsonpath_state,
            )
            components.jsonpath_score = jsonpath_score
            components.jsonpath_reasons = jsonpath_reasons
            accounted_keys[JSONPATHS_KEY] = EVALUATED
            logger.info(f"GradeTrial: {trial_id} - Jsonpath checks: score={jsonpath_score:.2f}")

        # A.3) DB PROBES (substrate SQL assertions) — the sole state source for
        # tasks that use it; combining db_probes with hash/jsonpath checks in one
        # task is out of scope, so its score fills the state_checks component.
        if state_checks_config and state_checks_config.db_probes:
            logger.info(
                f"GradeTrial: {trial_id} - Evaluating "
                f"{len(state_checks_config.db_probes)} db probes"
            )
            probes = [probe.model_dump() for probe in state_checks_config.db_probes]
            db_probe_score, db_probe_reasons = await evaluate_db_probes(probes)
            components.db_probe_score = db_probe_score
            components.db_probe_reasons = db_probe_reasons
            accounted_keys[DB_PROBES_KEY] = EVALUATED
            logger.info(f"GradeTrial: {trial_id} - DB probes: score={db_probe_score:.2f}")

        # B) TRANSCRIPT RULES GRADING (if transcript_rules exist)
        transcript_rules_config = grading_config.transcript_rules
        if transcript_rules_config:
            transcript_result, transcript_accounting = self._grade_transcript_rules(
                trial_id, transcript_rules_config, timeline
            )
            accounted_keys.update(transcript_accounting)
            if transcript_result is not None:
                components.transcript_pass = transcript_result.passed
                components.transcript_score = transcript_result.score

        # B.1) TRACE CHECKS — declarative constraints over the event timeline,
        # scored by the same function the core engine calls.
        trace_checks_result = TraceChecksResult()
        if grading_config.trace_checks:
            trace_checks_result = self._grade_trace_checks(
                trial_id, grading_config.trace_checks, timeline
            )
            accounted_keys.update(trace_checks_result.accounted_keys)
            if trace_checks_result.constraints:
                components.trace_checks_score = trace_checks_result.score

        # B.2) LLM JUDGE GRADING (if llm_judge configured) — runner-side read-only
        # rubric judge on the shared ToolCallingLoop. Returns per-criterion results
        # + a weighted score, or ERRORED (no score) on its own malfunction. Never a
        # 0.0/0.5 fallback (fail loud — AGENTS.md rule 1).
        llm_judge_config = grading_config.llm_judge
        judge_status = pb2.JUDGE_STATUS_UNSPECIFIED
        criterion_results: list[CriterionResult] = []
        judge_reasons: str | None = None
        judge_gate_failed = False
        judge_report: pb2.JudgeReport | None = None
        if llm_judge_config:
            if llm_messages:
                logger.info(f"GradeTrial: {trial_id} - Evaluating LLM judge")
                judge_result = await self._grade_llm_judge(
                    trial_id, llm_judge_config, llm_messages, trial_context
                )
                accounted_keys[LLM_JUDGE_KEY] = EVALUATED
                judge_reasons = judge_result.reasons
                criterion_results = list(judge_result.criterion_results)
                # Cross the judge's own usage + audit transcript to the host. Built
                # for both COMPLETED and ERRORED runs — an errored judge still spent
                # tokens, and its partial transcript is the key debugging artifact.
                judge_report = pb2.JudgeReport(
                    calls=judge_result.usage.calls,
                    prompt_tokens=judge_result.usage.prompt_tokens,
                    completion_tokens=judge_result.usage.completion_tokens,
                    reasoning_tokens=judge_result.usage.reasoning_tokens,
                    cost_usd=judge_result.usage.cost_usd,
                    tool_calls=judge_result.usage.tool_calls,
                    consistency_rejections=judge_result.usage.consistency_rejections,
                    transcript_json=json.dumps(list(judge_result.transcript)),
                    knowledge_search_disabled=judge_result.knowledge_search_disabled,
                    kb_tools_offered=list(judge_result.kb_tools_offered),
                    kb_tools_withheld=list(judge_result.kb_tools_withheld),
                    state_diff_text=judge_result.state_diff or "",
                    read_tools_offered=list(judge_result.read_tools_offered),
                    custom_system_prompt=judge_result.custom_system_prompt,
                    include_agent_system_prompt=judge_result.include_agent_system_prompt,
                )
                if judge_result.status is JudgeStatus.ERRORED:
                    # Fail loud: the judge component is incomplete, NOT zero. Leave
                    # the component score at the -1.0 sentinel so it is excluded
                    # from the weighted combine, and surface ERRORED on the Grade.
                    judge_status = pb2.JUDGE_STATUS_ERRORED
                    logger.error(f"GradeTrial: {trial_id} - LLM judge ERRORED: {judge_reasons}")
                else:
                    judge_status = pb2.JUDGE_STATUS_COMPLETED
                    judge_gate_failed = judge_result.gate_failed
                    # A failed required criterion is a HARD fail of the component
                    # regardless of the weighted score — a high score must not
                    # rescue it. Feed 0.0 so the combine reflects the gate.
                    components.llm_judge_score = 0.0 if judge_gate_failed else judge_result.score
                    logger.info(
                        f"GradeTrial: {trial_id} - LLM judge: "
                        f"score={judge_result.score:.2f}, gate_failed={judge_gate_failed}"
                    )
            else:
                logger.info(f"GradeTrial: {trial_id} - Skipping LLM judge (no transcript messages)")
                accounted_keys[LLM_JUDGE_KEY] = NO_JUDGE_MESSAGES_SKIP

        # B.3) CUSTOM PYTHON CHECKS — the pack's ``checks.py`` executes runner-side
        # when ``grading.custom_checks.enabled``; the aggregate score fills
        # ``components.custom_checks`` and the per-check breakdown rides
        # ``Grade.custom_checks`` (see ADR-0012).
        custom_checks_score, custom_check_wire_results = await self._grade_custom_checks(
            trial_id, trial_context, llm_messages
        )
        components.custom_checks_score = custom_checks_score
        # A pack that wrote the block but left it off never reaches the executor,
        # so the key is populated with nothing consuming it — the same shape as
        # `hash:` keys arriving with `hash.enabled` false.
        accounted_keys[CUSTOM_CHECKS_KEY] = (
            EVALUATED
            if custom_checks_enabled(grading_config.custom_checks)
            else CUSTOM_CHECKS_DISABLED_SKIP
        )

        # C) LEDGER — a scored key the config populated that no evaluator consumed
        # and no skip site claimed would score nothing while the trial still got a
        # grade. Fail the RPC naming it; never fold it in as 0.0.
        audit = audit_accounted_keys(grading_config, accounted_keys)
        if audit.error:
            logger.error(f"GradeTrial: {trial_id} - {audit.error}")
            return pb2.GradeTrialResponse(success=False, error=audit.error)

        # D) COMBINE SCORES
        # Resolved before the combine, which resolves the same slot again, so that an
        # undecidable fold fails the RPC naming this trial rather than reaching the
        # outer catch-all as an anonymous grading error.
        try:
            state_checks_slot = resolve_state_checks_component(
                hash_score=components.hash_score,
                jsonpath_score=components.jsonpath_score,
                db_probe_score=components.db_probe_score,
                hash_weight=state_checks_config.hash_weight if state_checks_config else None,
            )
        except ValueError as exc:
            logger.error(f"GradeTrial: {trial_id} - {exc}")
            return pb2.GradeTrialResponse(
                success=False,
                error=f"Trial {trial_id!r} is not gradeable: {type(exc).__name__}: {exc}",
            )

        components_dict = components.model_dump()
        grading_config_dict = grading_config.model_dump()
        score, binary_pass = combine_grade_components(components_dict, grading_config_dict)

        # A failed required rubric criterion or a tripped trace gate fails the trial
        # outright — independent of pass_threshold and any other heavily-weighted
        # component. One rule, two gates: the core engine applies the trace half at
        # its own combine (``combine.py``), so the verdict does not depend on which
        # substrate graded the trial.
        if judge_gate_failed or trace_checks_result.gate_failed:
            binary_pass = False

        # Build reasons string
        state_diff_dict = state_diff.model_dump() if state_diff else None
        transcript_result_dict = transcript_result.model_dump() if transcript_result else None
        reasons = build_grade_reasons(
            components_dict,
            state_diff_dict,
            transcript_result_dict,
            judge_reasons=judge_reasons or None,
            trace_checks_result=trace_checks_result.model_dump(mode="json"),
        )
        if judge_status == pb2.JUDGE_STATUS_ERRORED:
            reasons += f" | JUDGE ERRORED: {judge_reasons}"

        # A populated key whose evaluator was skipped scored nothing; say so on the
        # grade rather than letting the trial look fully evaluated.
        if audit.skip_notes:
            reasons += " | " + "; ".join(audit.skip_notes)

        # The ledger's skip notes cover populated SCORED_CHECK keys; hash.weight is a
        # CONFIG_INPUT the fold can skip on its own, so it reports itself.
        if state_checks_slot.inert_weight_reason:
            reasons += f" | {state_checks_slot.inert_weight_reason}"

        # Append golden action errors if any (critical for debugging golden replay failures)
        if hash_result and hash_result.golden_action_errors:
            errors_str = "; ".join(hash_result.golden_action_errors)
            reasons += f" | GOLDEN REPLAY ERRORS: {errors_str}"

        logger.info(
            f"GradeTrial: {trial_id} - score={score:.2f}, pass={binary_pass}, "
            f"termination_reason={termination_reason.value if termination_reason else 'none'}"
        )

        # -1.0 is the wire's not-evaluated sentinel for a component.
        wire_component_scores = {
            spec.name: getattr(components, spec.runner_score_field)
            for spec in GRADE_COMPONENTS
            if spec.runner_score_field is not None
        }
        wire_component_scores["state_checks"] = (
            -1.0 if state_checks_slot.component is None else state_checks_slot.component
        )

        return pb2.GradeTrialResponse(
            success=True,
            error="",
            grade=pb2.Grade(
                binary_pass=binary_pass,
                score=score,
                components=pb2.GradeComponents(**wire_component_scores),
                reasons=reasons,
                state_diff_json=json.dumps(state_diff_dict) if state_diff_dict else "",
                custom_checks=custom_check_wire_results,
                criterion_results=[
                    pb2.CriterionResult(
                        id=cr.id, met=cr.met, score=cr.score, justification=cr.justification
                    )
                    for cr in criterion_results
                ],
                judge_status=judge_status,
                judge_report=judge_report,
                trace_checks=[
                    pb2.TraceConstraintResult(
                        id=item.id,
                        kind=item.kind,
                        passed=item.passed,
                        weight=item.weight,
                        message=item.message,
                        matched_positions=item.matched_positions,
                        severity=item.severity,
                        undecided=item.undecided,
                    )
                    for item in trace_checks_result.constraints
                ],
                trace_checks_summary=pb2.TraceChecksSummary(
                    winning_path=trace_checks_result.winning_path,
                    gate_failed=trace_checks_result.gate_failed,
                    failed_gate_ids=trace_checks_result.failed_gate_ids,
                    paths=[
                        pb2.TracePathResult(
                            id=path.id, score=path.score, gate_failed=path.gate_failed
                        )
                        for path in trace_checks_result.paths
                    ],
                ),
            ),
        )

    def _grade_time_views(
        self,
        request: pb2.GradeTrialRequest,
        trial_context: TrialContextRuntime,
        termination_reason: TerminationReason | None,
    ) -> tuple[list[dict[str, Any]], TrialTimeline]:
        """The trial's grade-time views: the wire messages and the event timeline.

        The agent policy is the payload's leading ``system`` message and is split
        off first: the judge needs it separately, and the timeline is built from the
        transcript alone.

        The split is *not* what makes a hash-only trial work — whose payload is the
        policy and nothing else, and which must read as a records-only trial rather
        than as a message view whose every recorded call is unlinkable. That is
        guaranteed one layer down, by the builder counting only assistant and user
        turns as a message view (non-guarantee N3). Measured: removing this split
        leaves ``message_view_present`` ``False`` either way. Keep the split for the
        judge, but do not move the hash-only guarantee up here — the lock that
        protects it is ``test_a_view_of_only_harness_text_is_not_a_message_view``.

        Raises:
            ValueError: the payload does not decode into a transcript.
            TimelineInconsistencyError: the two views cannot be joined.
        """
        if not request.llm_messages_json:
            return [], build_trial_timeline([], trial_context.recorded, termination_reason)

        llm_messages: list[dict[str, Any]] = json.loads(request.llm_messages_json)
        _, transcript = split_leading_system_message(llm_messages)
        return llm_messages, build_trial_timeline(
            decode_transcript_wire(transcript), trial_context.recorded, termination_reason
        )

    def _grade_transcript_rules(
        self,
        trial_id: str,
        transcript_rules_config: TranscriptRulesConfig,
        timeline: TrialTimeline,
    ) -> tuple[TranscriptEvaluationResult | None, dict[str, KeyAccountingRecord]]:
        """Score the pack's transcript rules, returning ``(result, accounted keys)``.

        A ``None`` result is the trial that left no trace of itself — a timeline
        carrying neither a conversational turn nor a tool call — and declared no
        activity floor: nothing is evaluated and the component is left out of the
        combine.

        A declared floor *is* evaluated on that timeline, because no events is
        precisely the answer it asks for, while every other rule would score
        against evidence the trial does not carry. So the floor alone reaches the
        evaluator there and its siblings are recorded as skipped — the blanket skip
        goes down first so the floor's own record survives it.
        """
        activity_floor = transcript_rules_config.min_assistant_turns
        skipped_siblings: dict[str, KeyAccountingRecord] = {}
        if timeline.events:
            logger.info(f"GradeTrial: {trial_id} - Evaluating transcript rules")
            # The author-facing TranscriptRulesConfig as a dict; the grader
            # decomposes its fields (must_contain / disallow_regex / max_turns /
            # min_assistant_turns / required_actions / communicate_info) into
            # per-field sub-checks.
            rules_dict = transcript_rules_config.model_dump()
        elif activity_floor is None:
            logger.info(
                f"GradeTrial: {trial_id} - Skipping transcript rules (no messages or tool calls)"
            )
            return None, dict.fromkeys(transcript_rules_author_keys(), NO_TIMELINE_EVENTS_SKIP)
        else:
            logger.info(
                f"GradeTrial: {trial_id} - Evaluating the activity floor alone "
                "(no messages or tool calls)"
            )
            rules_dict = TranscriptRulesConfig(min_assistant_turns=activity_floor).model_dump()
            skipped_siblings = dict.fromkeys(
                transcript_rules_author_keys(), NO_TIMELINE_EVENTS_SKIP
            )

        result = evaluate_transcript_rules(timeline, rules_dict)
        return result, {**skipped_siblings, **result.accounted_keys}

    def _grade_trace_checks(
        self, trial_id: str, config: TraceChecksConfig, timeline: TrialTimeline
    ) -> TraceChecksResult:
        """Score the pack's trace checks over the timeline both substrates build.

        A result carrying no constraint verdicts is the trial that left no trace
        of itself — a timeline with neither a conversational turn nor a tool call
        — where every constraint would score against evidence the trial does not
        have. The component is then left out of the combine, and the evaluator's
        own accounting records the skip against each kind the block declared.
        """
        result = evaluate_trace_checks(timeline, config)
        if not result.constraints:
            logger.info(
                f"GradeTrial: {trial_id} - Skipping trace checks (no messages or tool calls)"
            )
            return result
        logger.info(f"GradeTrial: {trial_id} - Trace checks: score={result.score:.2f}")
        return result

    async def _grade_llm_judge(
        self,
        trial_id: str,
        llm_judge_config: "LLMJudgeConfig",
        llm_messages: list[dict[str, Any]],
        trial_context: TrialContextRuntime,
    ) -> "JudgeResult":
        """Run the read-only rubric judge for one trial.

        Async/sync bridge: ``_grade_trial_async`` runs ON the dedicated event
        loop thread. The judge loop (``LLMJudge.run``) is synchronous and the
        DB client is async, so we run the judge in a *thread executor*
        (``run_in_executor``) and give its read-only DB tools a ``DBReader`` that
        bridges each call back to this loop via
        ``asyncio.run_coroutine_threadsafe`` — safe because the executor thread is
        never the loop thread. Nothing here can deadlock the loop.

        Narrow input surface: only ``{agent_system_prompt, transcript, rubric,
        read-tools}`` are passed; the deterministic-oracle config never reaches
        the judge.
        """
        loop = self._loop
        db_client = self.db_client

        class _LoopBridgeDBReader:
            """Synchronous read seam bridging to the async DB client on ``loop``."""

            def get_state(self, tables: list[str] | None = None) -> dict[str, Any]:
                fut = asyncio.run_coroutine_threadsafe(db_client.get_state(trial_id, tables), loop)
                return fut.result(timeout=30.0).data

            def query(self, jsonpath: str) -> dict[str, Any]:
                fut = asyncio.run_coroutine_threadsafe(db_client.query(trial_id, jsonpath), loop)
                return {"results": fut.result(timeout=30.0).results}

        # Agent policy comes from the transcript's leading system message (the
        # only oracle-free policy signal the runner has). Split from the
        # transcript so it is injected as policy, not replayed as a turn.
        agent_system_prompt, transcript = split_leading_system_message(list(llm_messages))

        # search_kb only when a KnowledgeSearch was resolved for THIS trial at
        # setup — the SAME per-trial index the agent's KB tool searched. Faithful
        # gating: agent had a KB ⇒ judge gets the same KB; none ⇒ no tool. This
        # replaces the old ``rag_url = self.rag_client.base_url`` path, which
        # keyed on container-level client existence and hit the wrong (global)
        # index.
        kb_search = trial_context.resolve_kb_search()
        # TypeSense KB plane: if the agent had the read-only ``search_policy`` tool
        # (the documented mcp_core TypeSense connector), let the judge reuse THAT
        # EXACT reconstructed tool via a read-only passthrough — same tool, query,
        # backend, and ranking the agent saw. This is orthogonal to the rag
        # ``kb_search`` path above; both end as read-only tools in the judge
        # registry. See ``_build_judge_search_policy_tools``.
        extra_read_tools = self._build_judge_search_policy_tools(trial_context)
        # File readers only when a real workspace exists on this runner.
        workspace_dir = self._judge_workspace_dir(trial_context)

        # The judge model is a run-level config that rides the TrialSpec. The
        # orchestrator validates up front that it is present whenever any selected
        # task declares an llm_judge component, so reaching here without it is a
        # contract violation — fail loud (AGENTS.md rule 1), never invent a model
        # or silently skip grading.
        judge_model_config = trial_context.judge_model_config
        if judge_model_config is None:
            raise ValueError(
                f"Trial {trial_id} has an llm_judge rubric but no judge ModelConfig on "
                "its TrialSpec (judge_model_config is None). The run config must set "
                "models.judge; the orchestrator should have rejected this run up front."
            )

        # Diff-first default: hand the judge the initial → final state delta (the
        # agent's own edits) as its primary view of the outcome, instead of it
        # dumping the whole final DB via get_db_state. This is NOT the
        # trial-vs-golden diff (that would leak the oracle and bias grading, see
        # docs/RUBRIC_GRADING_DESIGN.md #7/#8) — it reveals only what the agent changed.
        # Persisted for free: the opening message it lands in is captured into
        # judge_trajectory.yaml. Computed here on the loop thread where both
        # states are in hand.
        #
        # This is an aid to the judge, not a grade component: if building it
        # fails (DB read hiccup, unexpected state shape), degrade to no diff
        # rather than failing the whole grade — the judge still has its read-only
        # tools, and the hash / jsonpath components already computed must not be
        # discarded. The judge's OWN fail-loud contract still governs grading;
        # this only guards the optional context we hand it.
        try:
            state_diff_text = await self._build_judge_state_diff(trial_id, trial_context)
        except Exception as exc:  # noqa: BLE001 — optional context, never fail the grade
            logger.warning(
                "Failed to build judge state diff; grading without it "
                f"(trial_id={trial_id}, error={exc})"
            )
            state_diff_text = None

        # Judge-side tool gating. The agent's tool surface is
        # untouched: the runner still resolves kb_search / extra_read_tools
        # faithfully above; the judge withholds the KB-tagged ones by construction
        # when the effective customization asks for it. Absent/None → False.
        customization = llm_judge_config.customization
        disable_knowledge_search = bool(customization and customization.disable_knowledge_search)
        custom_system_prompt = customization.system_prompt if customization else None
        include_agent_system_prompt = (
            customization.include_agent_system_prompt
            if customization and customization.include_agent_system_prompt is not None
            else True
        )

        def _run() -> JudgeResult:
            return LLMJudge(
                judge_model_config,
                disable_knowledge_search=disable_knowledge_search,
                custom_system_prompt=custom_system_prompt,
                include_agent_system_prompt=include_agent_system_prompt,
            ).run(
                rubric=llm_judge_config.rubric,
                agent_system_prompt=agent_system_prompt,
                transcript=transcript,
                db_reader=_LoopBridgeDBReader(),
                kb_search=kb_search,
                extra_read_tools=extra_read_tools,
                workspace_dir=workspace_dir,
                state_diff=state_diff_text,
            )

        return await loop.run_in_executor(None, _run)

    async def _grade_custom_checks(
        self,
        trial_id: str,
        trial_context: TrialContextRuntime,
        llm_messages: list[dict[str, Any]],
    ) -> tuple[float, list["pb2.CustomCheckResult"]]:
        """Run the pack's ``checks.py`` against the trial's evidence.

        Returns ``(score, wire_results)``. A missing/disabled config returns
        ``(-1.0, [])`` so :func:`combine_grade_components` treats the
        component as not-evaluated (the empty-active-set guard then fires
        for a custom-checks-only pack instead of silently passing).

        On executor error (missing ``checks.py``, module load failure,
        timeout): a sentinel wire entry preserves the audit, and the score
        follows ``fail_on_error`` — ``0.0`` when true (contributes to the
        weighted total as a fail), ``-1.0`` when false (component not
        evaluated).
        """
        grading_config = trial_context.grading_config
        custom_config_raw = grading_config.custom_checks if grading_config else None
        if not custom_checks_enabled(custom_config_raw):
            return -1.0, []

        config = CustomChecksConfig(**custom_config_raw)

        artifacts_dir = self._artifact_dirs.get(trial_id)
        if artifacts_dir is None:
            error_msg = (
                f"custom_checks.enabled but no artifacts_dir for trial {trial_id!r} "
                "(checks.py was not delivered by the adapter)"
            )
            logger.error("GradeTrial: %s - %s", trial_id, error_msg)
            score = 0.0 if config.fail_on_error else -1.0
            return score, [_executor_error_to_wire(error_msg)]
        checks_file = artifacts_dir / config.file

        task_description = trial_context.task_description
        initial_tables = task_description.initial_state.tables
        initial_state_json_db: dict[str, Any] | None = (
            dict(initial_tables) if initial_tables else None
        )

        # Try to fetch the trial's final DB state. On failure — DB Service
        # unreachable, trial never registered (empty initial_state skips the
        # init in ``RegisterTrial`` above), etc. — degrade to an empty
        # ``final_env_state`` so ``build_check_context`` still gets a
        # well-formed input and the check runs against an honest empty state
        # rather than crashing the grade path. Any real state a tool call
        # wrote to a functional DB is preserved (matches the substrate parity
        # test's expectation that a fixture DB with data drives discrimination
        # even when ``initial_state.tables`` is empty).
        try:
            final_state_response = await self.db_client.get_state(trial_id)
            final_env_state: dict[str, Any] = final_state_response.data
        except (
            Exception
        ) as exc:  # noqa: BLE001 — grade path degrades to empty state; the DB failure surfaces via component metadata, not a crash
            logger.warning(
                "GradeTrial: %s - final DB state fetch failed (%s); grading against empty state",
                trial_id,
                exc,
            )
            final_env_state = {}

        ctx = build_check_context(
            initial_state_json_db=initial_state_json_db,
            final_env_state=final_env_state,
            transcript=_build_runner_check_transcript(llm_messages),
            task=TaskContext(
                task_id=task_description.task_id,
                task_name=task_description.name,
                task_description=task_description.description,
                domain=task_description.category or "",
            ),
        )

        logger.info(f"GradeTrial: {trial_id} - Running custom checks from {checks_file}")
        try:
            result: CheckResultSet = await self._loop.run_in_executor(
                None,
                lambda: self.check_executor.run(
                    checks_file=checks_file,
                    task_dir=artifacts_dir,
                    ctx=ctx,
                    config=config,
                ),
            )
        except Exception as exc:
            # An executor that raises rather than capturing into
            # :class:`CheckResultSet` is a contract violation; convert it to
            # the same sentinel-entry shape as ``result.error`` so the audit
            # survives and the whole trial's grade is not lost to the outer
            # handler.
            logger.exception(
                "GradeTrial: %s - custom checks executor raised",
                trial_id,
            )
            score = 0.0 if config.fail_on_error else -1.0
            return score, [_executor_error_to_wire(str(exc))]

        wire_results = [_check_result_to_wire(r) for r in result.results]

        if result.error:
            logger.error(
                "GradeTrial: %s - custom checks executor error: %s",
                trial_id,
                result.error,
            )
            wire_results.append(_executor_error_to_wire(result.error))
            score = 0.0 if config.fail_on_error else -1.0
            return score, wire_results

        logger.info(
            f"GradeTrial: {trial_id} - custom checks: "
            f"{result.passed}/{result.total} passed, score={result.aggregate_score:.2f}"
        )
        return result.aggregate_score, wire_results

    async def _build_judge_state_diff(
        self, trial_id: str, trial_context: TrialContextRuntime
    ) -> str | None:
        """Render the ``initial → final`` DB state diff for the judge, or ``None``.

        Returns ``None`` when there is no DB client or no initial-state tables for
        this trial (e.g. non-DB tasks) — the judge then falls back to its
        read-only tools. Otherwise fetches the raw final state (the same shape
        ``get_db_state`` returns) and diffs it against the pre-run initial tables,
        matching rows by declared primary key and dropping ``unstable_fields`` as
        noise so the diff shows only meaningful edits.
        """
        db_client = self.db_client
        task_desc = trial_context.task_description
        initial_state = task_desc.initial_state if task_desc else None
        if db_client is None or initial_state is None or not initial_state.tables:
            return None
        final_state = (await db_client.get_state(trial_id)).data
        primary_keys = {s.table_name: s.primary_key for s in initial_state.schemas}
        unstable_fields = {(u.table_name, u.field_name) for u in initial_state.unstable_fields}
        return render_state_diff(
            initial_state.tables,
            final_state,
            primary_keys=primary_keys,
            unstable_fields=unstable_fields,
        )

    def _resolve_judge_kb_search(
        self, trial_id: str, agent_tools: dict[str, Callable]
    ) -> KnowledgeSearch | None:
        """Resolve the judge's per-trial KnowledgeSearch, or None.

        Gated on the SAME signal that gave the AGENT a rag ``search_kb``: a
        ``RAGSearchToolWrapper`` was reconstructed (dispatch=RAG + a rag client)
        — NOT ``search_config.enabled`` (the decoupled TypeSense plane;
        ``native.py`` hardcodes ``search.enabled=False``, so gating there never
        fires for real rag tasks and wrongly fires for TypeSense ones). Detected
        by instance, not tool name, so a renamed tool can't fool it.

        Binding to the same ``rag_client`` + ``trial_id`` means the judge
        retrieves from the SAME per-trial index by construction: if the agent's
        ``search_kb`` works the judge's does too, and if it 404s both do.
        Per-trial indexing gating is a separate concern.
        """
        if self.rag_client is None:
            return None
        if not any(isinstance(t, RAGSearchToolWrapper) for t in agent_tools.values()):
            return None
        return RagServiceKnowledgeSearch(self.rag_client, trial_id)

    def _build_judge_search_policy_tools(
        self, trial_context: TrialContextRuntime
    ) -> list[DelegatingReadTool]:
        """Build judge passthrough tools reusing the agent's ``search_policy`` tools.

        TypeSense KB faithfulness: when the agent used the read-only
        ``search_policy`` KB tool (the mcp_core TypeSense connector), the judge
        must be able to search the SAME knowledge base. We do this by reusing the
        agent's OWN already-reconstructed ``search_policy`` ``ToolWrapper`` — no
        mcp_core import, no assumptions about TypeSense internals or
        ``search_policy``'s I/O format. We just re-publish its real schema (so the
        judge LLM fills args correctly) and relay its output verbatim.

        Gate = mirror the agent (same shape as the rag path): a passthrough is
        offered for each reconstructed tool whose name identifies the documented
        ``search_policy`` connector. Detection is by the documented connector name
        — ``search_policy`` is a closed mcp_core tool, not a tolokaforge type, so
        instance detection is unavailable; the name is the contract. We accept the
        bare name ``search_policy`` AND any adapter toolset-namespace PREFIX of it
        (e.g. ``connectors_typesense_search_policy``): namespaced adapters
        (``tlk_mcp_core`` / ``frozen_mcp_core``) key ``agent_tools`` by the
        prefixed ``schema.name``, so an exact lookup would miss them. The suffix
        is anchored on ``_search_policy`` so unrelated names (``search_policy_v2``,
        ``search_policy_admin``) do NOT match. This only WIDENS which agent KB
        tools are reused; the read-only-by-convention assumption of the
        ``search_policy`` connector extends to its namespaced variants, so we stay
        within the documented read-only connector convention (never a generic MCP
        passthrough — we cannot classify arbitrary tools' read-only-ness).

        Multiple connectors: an agent may expose several TypeSense domains, each a
        distinct ``*_search_policy`` connector. We build one passthrough per match,
        each named from its own ``tool_schema.name`` (the distinct namespaced names
        guarantee no judge-registry collision).

        Async bridge: the agent ``ToolWrapper.execute`` is async, but the judge
        loop runs synchronously in a worker thread. ``invoke`` bridges each call
        to the runner's dedicated event loop via ``self._run_async`` (the same
        loop the DB reader bridges to), so the judge tool's sync ``execute`` can
        drive the agent tool's async ``execute``.
        """

        def _is_search_policy(name: str) -> bool:
            return name == _SEARCH_POLICY_TOOL_NAME or name.endswith(f"_{_SEARCH_POLICY_TOOL_NAME}")

        def _make_invoke(tool: Any) -> Callable[[dict[str, Any]], str]:
            # Bind the per-iteration tool via a factory so the closure does not
            # capture the loop variable by reference (late-binding bug).
            def invoke(arguments: dict[str, Any]) -> str:
                return self._run_async(tool.execute(arguments))

            return invoke

        passthroughs: list[DelegatingReadTool] = []
        for name, agent_tool in trial_context.agent_tools.items():
            if not _is_search_policy(name):
                continue

            if not hasattr(agent_tool, "tool_schema") or not callable(
                getattr(agent_tool, "execute", None)
            ):
                logger.warning(
                    f"{name} in agent_tools is not a ToolWrapper "
                    f"({type(agent_tool).__name__}); skipping judge KB passthrough"
                )
                continue

            schema = agent_tool.tool_schema
            passthroughs.append(
                DelegatingReadTool(
                    name=schema.name,
                    description=schema.description,
                    parameters=schema.parameters,
                    invoke=_make_invoke(agent_tool),
                    knowledge_search=True,
                )
            )

        return passthroughs

    @staticmethod
    def _judge_workspace_dir(trial_context: TrialContextRuntime) -> Path | None:
        """Return the agent workspace dir for judge file reads, if one exists here.

        Read-only file tools are offered to the judge only when a real workspace
        directory is present on this runner. Most state-routed tasks have none.
        """
        task = trial_context.task_description
        candidate = getattr(getattr(task, "initial_state", None), "agent_visible_dir", None)
        if candidate:
            path = Path(candidate)
            if path.exists():
                return path
        return None

    async def _execute_hash_grading(
        self,
        trial_id: str,
        trial_context: TrialContextRuntime,
        golden_actions: list[GoldenAction],
        *,
        numeric_string_fields: list[str] | None = None,
    ) -> HashGradingResult:
        """
        Execute hash-based grading algorithm.

        Steps:
        1. Get current trial stable hash
        2. Snapshot current state
        3. Reset to initial state
        4. Execute golden path actions
        5. Snapshot golden state (for diff if mismatch)
        6. Get golden stable hash
        7. Restore trial state
        8. Compare hashes
        9. If mismatch, compute state diff

        Args:
            trial_id: Trial identifier
            trial_context: Trial context with tools
            golden_actions: List of golden path actions to execute

        Returns:
            HashGradingResult with hash_match, hash_score, and optional state_diff
        """
        # Detect MCP server wrappers — their state lives in a subprocess, not
        # in the db-service, so we must sync before hashing and reset the MCP
        # subprocess state when the db-service is reset.
        mcp_wrapper = self._find_mcp_server_wrapper(trial_context)

        # 1. Get current trial stable hash
        # For MCP_SERVER tasks the db-service was never updated during the trial
        # (the MCP subprocess holds state in memory), so sync first.
        if mcp_wrapper is not None:
            logger.info(f"GradeTrial: {trial_id} - Syncing MCP server state to db-service (trial)")
            try:
                loop = asyncio.get_event_loop()
                mcp_state = await loop.run_in_executor(None, mcp_wrapper.get_state)
                await self._sync_mcp_state_to_db(trial_id, mcp_state)
            except Exception as e:
                logger.error(f"GradeTrial: Failed to sync MCP state before trial_hash: {e}")
                raise

        trial_hash = await self.db_client.get_stable_hash(
            trial_id, numeric_string_fields=numeric_string_fields
        )
        logger.debug(f"GradeTrial: Trial hash = {trial_hash[:16]}...")

        # 2. Snapshot current state
        await self.db_client.create_snapshot(trial_id, "pre_golden")
        logger.debug("GradeTrial: Created snapshot 'pre_golden'")

        # 3. Reset to initial state
        await self.db_client.reset_trial(trial_id)
        logger.debug("GradeTrial: Reset to initial state")

        # For MCP_SERVER tasks also reset the subprocess state so golden actions
        # execute from a clean initial state (not from the agent's final state).
        if mcp_wrapper is not None:
            initial_tables = (
                trial_context.task_description.initial_state.tables
                if trial_context.task_description and trial_context.task_description.initial_state
                else {}
            )
            logger.info(f"GradeTrial: {trial_id} - Resetting MCP server state to initial")
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, lambda: mcp_wrapper.reset_state(initial_tables))
            except Exception as e:
                logger.error(f"GradeTrial: Failed to reset MCP state: {e}")
                raise

        # 4. Execute golden path actions
        #
        # Golden actions are replayed with their original arguments — no ID
        # substitution.  This matches mcp_core's apply_golden_set_to_database()
        # which also replays without substitution.  The InMemoryDatabase (and
        # the JSON DB Service that mirrors it) uses deterministic ID generation
        # (len(existing) + 1), so hardcoded IDs in golden actions always match
        # the IDs produced on replay from the same initial state.
        golden_action_errors: list[str] = []

        for i, action in enumerate(golden_actions):
            tool_name = action.tool_name
            arguments = dict(action.arguments)  # copy

            tool = trial_context.agent_tools.get(tool_name)
            if tool is None:
                # Try with domain prefix (golden actions use unprefixed names)
                for registered_name in trial_context.agent_tools:
                    if registered_name.endswith(f"_{tool_name}"):
                        tool = trial_context.agent_tools[registered_name]
                        logger.debug(
                            f"GradeTrial: Matched golden tool '{tool_name}' -> '{registered_name}'"
                        )
                        break
            if tool is None:
                # Golden action references tool that doesn't exist
                err_msg = f"Golden action {i} ({tool_name}): tool not found"
                logger.error(f"GradeTrial: {err_msg}")
                golden_action_errors.append(err_msg)
                continue  # Continue - partial golden state still useful

            try:
                # Execute tool
                if hasattr(tool, "execute"):
                    await tool.execute(arguments)
                elif callable(tool):
                    if inspect.iscoroutinefunction(tool):
                        await tool(arguments)
                    else:
                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(None, lambda t=tool, a=arguments: t(a))
                logger.debug(f"GradeTrial: Golden action {i} executed: {tool_name}")

            except Exception as e:
                # Golden action failure — log with full traceback for debugging
                err_msg = f"Golden action {i} ({tool_name}) failed: {type(e).__name__}: {e}"
                logger.error(f"GradeTrial: {err_msg}")
                logger.error(traceback.format_exc())
                golden_action_errors.append(err_msg)

        # For MCP_SERVER tasks: sync subprocess state to db-service so the
        # hash reflects what the golden actions actually produced.
        if mcp_wrapper is not None:
            logger.info(f"GradeTrial: {trial_id} - Syncing MCP server state to db-service (golden)")
            try:
                loop = asyncio.get_event_loop()
                golden_mcp_state = await loop.run_in_executor(None, mcp_wrapper.get_state)
                await self._sync_mcp_state_to_db(trial_id, golden_mcp_state)
            except Exception as e:
                logger.error(f"GradeTrial: Failed to sync MCP state after golden actions: {e}")
                raise

        # 5. Snapshot golden state (for diff if mismatch)
        await self.db_client.create_snapshot(trial_id, "golden_result")
        logger.debug("GradeTrial: Created snapshot 'golden_result'")

        # 6. Get golden stable hash
        # get_stable_hash returns the hash string directly
        golden_hash = await self.db_client.get_stable_hash(
            trial_id, numeric_string_fields=numeric_string_fields
        )
        logger.debug(f"GradeTrial: Golden hash = {golden_hash[:16]}...")

        # 7. Restore trial state
        await self.db_client.restore_snapshot(trial_id, "pre_golden")
        logger.debug("GradeTrial: Restored snapshot 'pre_golden'")

        # 8. Compare hashes
        hash_match = trial_hash == golden_hash
        hash_score = 1.0 if hash_match else 0.0

        # 9. If mismatch, compute state diff
        state_diff: StateDiff | None = None
        if not hash_match:
            logger.info("GradeTrial: Hash mismatch, computing state diff")

            # Get trial state
            trial_state_response = await self.db_client.get_stable_state(trial_id)
            trial_state = trial_state_response.data

            # Restore golden state and get it
            await self.db_client.restore_snapshot(trial_id, "golden_result")
            golden_state_response = await self.db_client.get_stable_state(trial_id)
            golden_state = golden_state_response.data

            # Restore trial state again
            await self.db_client.restore_snapshot(trial_id, "pre_golden")

            # Compute diff using grading module (returns StateDiff model directly)
            state_diff = compute_state_diff(trial_state, golden_state)

        return HashGradingResult(
            hash_match=hash_match,
            hash_score=hash_score,
            state_diff=state_diff,
            golden_action_errors=golden_action_errors,
        )

    # =========================================================================
    # MCP-server grading helpers
    # =========================================================================

    @staticmethod
    def _find_mcp_server_wrapper(
        trial_context: "TrialContextRuntime",
    ) -> MCPServerToolWrapper | None:
        """Return the first MCPServerToolWrapper found in agent_tools, or None."""
        for wrapper in trial_context.agent_tools.values():
            if isinstance(wrapper, MCPServerToolWrapper):
                return wrapper
        return None

    async def _sync_mcp_state_to_db(
        self,
        trial_id: str,
        mcp_state: dict[str, list[dict]],
    ) -> None:
        """Sync an MCP server's in-memory state to the db-service via diff mutations.

        Computes inserts / updates / deletes for every table and applies them
        through ``db_client.mutate``. Keys are resolved per table from
        ``state_checks.id_fields`` (default ``"id"``); a record missing its
        resolved key raises fail-loud rather than collapsing every keyless
        record to a single ``None`` bucket.

        Args:
            trial_id:  Trial identifier used by db-service.
            mcp_state: Full state dict from MCPServerToolWrapper.get_state().
        """
        current_response = await self.db_client.get_state(trial_id)
        current_state: dict[str, list[dict]] = current_response.data
        id_fields = self._id_fields_for_trial(trial_id)

        for table_name, new_records in mcp_state.items():
            current_records = current_state.get(table_name, [])
            if current_records == new_records:
                continue
            operations = compute_diff_ops(current_records, new_records, table_name, id_fields)
            if operations:
                await self.db_client.mutate(trial_id, table_name, operations)
                logger.debug(
                    f"_sync_mcp_state_to_db: {trial_id}/{table_name} — {len(operations)} op(s)"
                )

    def _id_fields_for_trial(self, trial_id: str) -> dict[str, str]:
        """Return the id_fields map declared by the trial's grading config, or ``{}``.

        Callers of ``_sync_mcp_state_to_db`` (GradeTrial, GetState) always guard
        that the trial is registered before invoking, so indexed access here is
        deliberate — a missing trial is a bug in the caller.
        """
        trial = self.trials[trial_id]
        if trial.task_description is None:
            return {}
        grading = trial.task_description.grading
        if grading is None or grading.state_checks is None:
            return {}
        return grading.state_checks.id_fields

    # =========================================================================
    # GetState - Debug endpoint to inspect current state
    # =========================================================================

    def GetState(
        self,
        request: pb2.GetStateRequest,
        context: grpc.ServicerContext,
    ) -> pb2.GetStateResponse:
        """
        Get current state snapshot for debugging.

        Delegates to DB Service to retrieve trial state.

        Args:
            request: GetStateRequest with trial_id and options
            context: gRPC context

        Returns:
            GetStateResponse with state JSON and hashes
        """
        trial_id = request.trial_id
        logger.debug(f"GetState: {trial_id}")

        # Run async operation on dedicated event loop thread
        try:
            result = self._run_async(self._get_state_async(request))
            return result
        except DBTrialNotFoundError:
            return pb2.GetStateResponse(
                success=False,
                error=f"Trial '{trial_id}' not found in DB Service",
            )
        except DBServiceError as e:
            logger.error(f"GetState: DB Service error: {e}")
            return pb2.GetStateResponse(
                success=False,
                error=f"DB Service error: {e.message}",
            )
        except Exception as e:
            logger.error(f"GetState: Unexpected error: {e}")
            return pb2.GetStateResponse(
                success=False,
                error=f"Unexpected error: {e}",
            )

    async def _get_state_async(self, request: pb2.GetStateRequest) -> pb2.GetStateResponse:
        """Async implementation of GetState."""
        trial_id = request.trial_id
        tables = list(request.tables) if request.tables else None

        # For native MCP-server tasks the db-service is never updated during
        # the trial (the subprocess holds state in memory).  Sync first so the
        # caller gets the real final state instead of the stale initial state.
        trial_context = self.trials.get(trial_id)
        if trial_context is not None:
            mcp_wrapper = self._find_mcp_server_wrapper(trial_context)
            if mcp_wrapper is not None:
                try:
                    loop = asyncio.get_event_loop()
                    mcp_state = await loop.run_in_executor(None, mcp_wrapper.get_state)
                    await self._sync_mcp_state_to_db(trial_id, mcp_state)
                    logger.debug(f"GetState: synced MCP subprocess state for {trial_id}")
                except Exception as e:
                    logger.warning(f"GetState: could not sync MCP state for {trial_id}: {e}")

        if request.include_unstable:
            # Get full state
            state_response = await self.db_client.get_state(trial_id, tables)
            state_json = json.dumps(state_response.data)
            stable_hash = state_response.stable_hash
            full_hash = state_response.full_hash
        else:
            # Get stable state (unstable fields filtered)
            stable_state_response = await self.db_client.get_stable_state(trial_id)
            state_json = json.dumps(stable_state_response.data)
            stable_hash = stable_state_response.stable_hash
            full_hash = ""  # Not available for stable state

        return pb2.GetStateResponse(
            success=True,
            error="",
            state_json=state_json,
            stable_hash=stable_hash,
            full_hash=full_hash,
        )

    # =========================================================================
    # Terminal-bench grading
    # =========================================================================

    async def _grade_via_test_execution(
        self,
        trial_id: str,
        trial_context: "TrialContextRuntime",
    ) -> "pb2.GradeTrialResponse":
        """Grade by running a reference test suite inside the trial's env container.

        Selected declaratively via ``grading.grading_method == "test_execution"`` —
        no adapter identity involved.

        1. Execute ``test.sh`` (pytest + reward calculation) in the task container.
        2. Read the reward float from ``/logs/verifier/reward.txt``.
        3. Return a ``GradeTrialResponse`` with the reward as score.
        """
        # Find an exec-capable lifecycle tool to run the suite in the env.
        bash_tool: DockerComposeExecToolWrapper | None = None
        for tool in trial_context.agent_tools.values():
            if isinstance(tool, DockerComposeExecToolWrapper):
                bash_tool = tool
                break

        if bash_tool is None:
            # Actionable for the adapter author: they asked for test-execution
            # grading but didn't ship a tool that can run the suite inside the env.
            error_msg = (
                "test-execution grading was requested (grading_method='test_execution') "
                "but no exec-capable env tool was found in this trial. Include an "
                "exec-capable lifecycle tool (e.g. DockerComposeExecToolWrapper) in "
                "TaskDescription.agent_tools so the runner can execute the test suite "
                "inside the trial environment."
            )
            logger.error(f"GradeTrial(test-execution): {trial_id} - {error_msg}")
            return pb2.GradeTrialResponse(success=False, error=error_msg)

        loop = asyncio.get_event_loop()

        # Run test.sh
        logger.info(f"GradeTrial(test-execution): {trial_id} - running test.sh")
        try:
            test_output = await loop.run_in_executor(
                None,
                bash_tool._exec_sync,
                "cd /tests && bash test.sh 2>&1",
                300.0,  # verifier timeout
            )
        except Exception as e:
            logger.error(f"GradeTrial(test-execution): test.sh failed: {e}")
            return pb2.GradeTrialResponse(
                success=True,
                error="",
                grade=pb2.Grade(
                    binary_pass=False,
                    score=0.0,
                    components=pb2.GradeComponents(custom_checks=0.0),
                    reasons=f"test.sh execution failed: {e}",
                ),
            )

        # Read reward
        try:
            reward_str = await loop.run_in_executor(
                None,
                bash_tool._exec_sync,
                "cat /logs/verifier/reward.txt 2>/dev/null || echo 0.0",
                10.0,
            )
            reward = float(reward_str.strip().split("\n")[-1])
            reward = max(0.0, min(1.0, reward))
        except (ValueError, IndexError):
            reward = 0.0

        logger.info(f"GradeTrial(test-execution): {trial_id} - reward={reward:.4f}")

        return pb2.GradeTrialResponse(
            success=True,
            error="",
            grade=pb2.Grade(
                binary_pass=(reward >= 0.5),
                score=reward,
                components=pb2.GradeComponents(custom_checks=reward),
                reasons=(
                    f"test-execution reward: {reward:.4f}\n\n"
                    f"test output (truncated):\n{test_output[:2000]}"
                ),
            ),
        )

    # =========================================================================
    # ResetTrial - Reset state to initial for retries
    # =========================================================================

    def ResetTrial(
        self,
        request: pb2.ResetTrialRequest,
        context: grpc.ServicerContext,
    ) -> pb2.ResetTrialResponse:
        """
        Reset trial state to initial state for retries.

        Delegates to DB Service to reset state and optionally
        re-executes initialization actions.

        Args:
            request: ResetTrialRequest with trial_id
            context: gRPC context

        Returns:
            ResetTrialResponse with success status and new state hash
        """
        trial_id = request.trial_id
        logger.info(f"ResetTrial: {trial_id}")

        # Run async operation on dedicated event loop thread
        try:
            result = self._run_async(self._reset_trial_async(request))
            return result
        except DBTrialNotFoundError:
            return pb2.ResetTrialResponse(
                success=False,
                error=f"Trial '{trial_id}' not found in DB Service",
            )
        except DBServiceError as e:
            logger.error(f"ResetTrial: DB Service error: {e}")
            return pb2.ResetTrialResponse(
                success=False,
                error=f"DB Service error: {e.message}",
            )
        except Exception as e:
            logger.error(f"ResetTrial: Unexpected error: {e}")
            return pb2.ResetTrialResponse(
                success=False,
                error=f"Unexpected error: {e}",
            )

    async def _reset_trial_async(self, request: pb2.ResetTrialRequest) -> pb2.ResetTrialResponse:
        """Async implementation of ResetTrial."""
        trial_id = request.trial_id

        # Reset state in DB Service
        reset_response = await self.db_client.reset_trial(trial_id)

        # Clear tool call history in trial context
        if trial_id in self.trials:
            trial_context = self.trials[trial_id]
            trial_context.clear_history()

            # Stop any per-trial lifecycle tools started during registration.
            # Capability-driven (has_lifecycle), not adapter identity.
            for tool in trial_context.agent_tools.values():
                if getattr(tool, "has_lifecycle", False):
                    try:
                        tool.stop()
                    except Exception as e:
                        logger.warning(f"ResetTrial: Failed to stop tool lifecycle: {e}")

        # TODO: Re-execute initialization_actions if requested
        # if request.execute_init_actions:
        #     trial_context = self.trials.get(trial_id)
        #     if trial_context:
        #         init_actions = trial_context.task_description.initialization_actions
        #         for action in init_actions:
        #             await self._execute_tool_internal(trial_id, action.tool_name, action.arguments)

        return pb2.ResetTrialResponse(
            success=True,
            error="",
            state_hash=reset_response.hash,
        )

    # =========================================================================
    # CleanupTrial - Forget a trial's registration
    # =========================================================================

    def CleanupTrial(
        self,
        request: pb2.CleanupTrialRequest,
        context: grpc.ServicerContext,
    ) -> pb2.CleanupTrialResponse:
        """
        Forget a trial's registration so the same ``trial_id`` can be re-registered.

        Used by the orchestrator's retry path to discard a prior attempt's
        runner-side state (``self.trials[trial_id]``, extracted artifacts, and
        the DB Service trial row) before re-attempting registration. Without
        this, ``RegisterTrial`` on the second attempt fails with
        ``Trial 'X' already exists``.

        Idempotent: succeeds with ``success=True`` when ``trial_id`` is unknown.
        ``ResetTrial`` is not a substitute — it preserves the registration.

        Args:
            request: CleanupTrialRequest with trial_id
            context: gRPC context

        Returns:
            CleanupTrialResponse with success status and any error message.
        """
        trial_id = request.trial_id
        logger.info(f"CleanupTrial: {trial_id}")

        try:
            self._run_async(self.cleanup_trial(trial_id))
            return pb2.CleanupTrialResponse(success=True, error="")
        except Exception as e:
            logger.error(f"CleanupTrial: Unexpected error: {e}")
            return pb2.CleanupTrialResponse(
                success=False,
                error=f"Unexpected error: {e}",
            )

    # =========================================================================
    # HealthCheck - Service health status
    # =========================================================================

    def HealthCheck(
        self,
        request: pb2.HealthCheckRequest,
        context: grpc.ServicerContext,
    ) -> pb2.HealthCheckResponse:
        """
        Service health check.

        Returns service status, version, active trials count,
        and DB Service connectivity.

        Args:
            request: HealthCheckRequest (empty)
            context: gRPC context

        Returns:
            HealthCheckResponse with health status
        """
        logger.debug("HealthCheck")

        # Run async operation on dedicated event loop thread
        try:
            result = self._run_async(self._health_check_async())
            return result
        except Exception as e:
            logger.error(f"HealthCheck: Error: {e}")
            return pb2.HealthCheckResponse(
                status="unhealthy",
                version=SERVICE_VERSION,
                num_active_trials=len(self.trials),
                db_service_connected=False,
                available_adapters=self._available_adapters,
            )

    async def _health_check_async(self) -> pb2.HealthCheckResponse:
        """Async implementation of HealthCheck."""
        # Check DB Service connectivity
        db_connected = False
        try:
            health_response = await self.db_client.health_check()
            db_connected = health_response.status == "healthy"
        except Exception as e:
            logger.warning(f"DB Service health check failed: {e}")
            db_connected = False

        # Determine overall status
        if db_connected:
            status = "healthy"
        else:
            status = "degraded"

        return pb2.HealthCheckResponse(
            status=status,
            version=SERVICE_VERSION,
            num_active_trials=len(self.trials),
            db_service_connected=db_connected,
            available_adapters=self._available_adapters,
        )

    # =========================================================================
    # Trial Cleanup
    # =========================================================================

    async def cleanup_trial(self, trial_id: str) -> None:
        """
        Clean up a trial's resources.

        Removes trial context, extracted tool artifacts, and deletes the trial
        from DB Service. Idempotent: returns silently when the trial is absent
        from every store.

        Args:
            trial_id: Trial identifier to clean up
        """
        logger.info(f"Cleaning up trial: {trial_id}")

        # Remove from local context
        trial_context = self.trials.get(trial_id)
        if trial_context is not None:
            # Explicit teardown of the per-trial judge KnowledgeSearch. Dropping
            # the context below already GCs it, but clearing here documents intent
            # and keeps lifecycle symmetric with register_kb_search at setup.
            trial_context.clear_kb_search()
            del self.trials[trial_id]

        # KNOWN LIMITATION: the mcp_core TypeSense client handle registered by
        # ``_init_typesense_for_trial`` (via mcp_core's
        # ``initialize_typesense_for_domain``) is NOT torn down here. mcp_core is
        # an optional, lazily-imported dependency that is not importable in this
        # repo, and its ``typesense_registry`` exposes no clearly-named
        # deregister/clear API we can confirm. Blind-calling an unknown teardown
        # would risk crashing cleanup or hiding errors (AGENTS.md rule 1), so the
        # leak is documented rather than papered over. Per-domain registration is
        # idempotent (re-init for the same domain re-registers, not duplicates),
        # so the practical impact is a bounded handle held for the runner's
        # lifetime, not unbounded growth across trials.

        # Drop extracted tool artifacts (no-op if none were extracted)
        self._cleanup_trial_artifacts(trial_id)

        # Delete from DB Service
        try:
            await self.db_client.delete_trial(trial_id)
        except DBTrialNotFoundError:
            pass  # Already deleted
        except DBServiceError as e:
            logger.warning(f"Failed to delete trial from DB Service: {e}")

    def cleanup_all_trials(self) -> None:
        """Clean up all trials (for shutdown)."""
        trial_ids = list(self.trials.keys())
        for trial_id in trial_ids:
            try:
                self._run_async(self.cleanup_trial(trial_id))
            except Exception as e:
                logger.warning(f"Failed to cleanup trial {trial_id}: {e}")

    # =========================================================================
    # TypeSense Client Initialization (for mcp_core search tools)
    # =========================================================================

    def _init_typesense_for_trial(
        self,
        search_config: Any,  # SearchConfig from models
        artifacts_dir: Path | None,
    ) -> None:
        """Initialise mcp_core TypeSense registry for search_policy tools.

        Inside Docker, the ``search_policy`` tool calls
        ``get_typesense_for_domain(domain)`` from mcp_core's global registry.
        This method registers a :class:`TypesenseIndex` client so that call
        succeeds.

        Documents are already indexed by the host-side adapter.
        ``initialize_typesense_for_domain()`` detects this (``doc_count > 0``)
        and skips re-indexing — it only registers the client handle.

        The document snippets are needed solely to compute the deterministic
        collection name (``<domain>_<sha256[:8]>``).
        """
        try:
            from mcp_core.search.typesense_registry import initialize_typesense_for_domain
        except ImportError:
            logger.warning(
                "mcp_core.search.typesense_registry not available — "
                "search_policy tools will fail with 'Search service is not available'"
            )
            return

        domain = search_config.domain_name or "default"
        host = search_config.host
        port = search_config.port or 8108
        api_key = search_config.api_key

        # Load document snippets from extracted docindex/ directory.
        # These are needed to compute the deterministic collection name
        # that matches what the host-side adapter already indexed.
        snippets: list[str] = []
        if artifacts_dir:
            docindex_dir = artifacts_dir / "docindex"
            if docindex_dir.is_dir():
                for md_file in sorted(docindex_dir.glob("*.md")):
                    try:
                        content = md_file.read_text(encoding="utf-8")
                        if content.strip():
                            snippets.append(content)
                    except Exception as e:
                        logger.warning(f"Failed to read docindex file {md_file}: {e}")

        if not snippets:
            logger.warning(
                f"No docindex snippets found for domain '{domain}' "
                f"(artifacts_dir={artifacts_dir}) — TypeSense client not registered"
            )
            return

        logger.info(
            f"Initialising TypeSense client for domain '{domain}': "
            f"host={host}, port={port}, snippets={len(snippets)}"
        )

        try:
            client = initialize_typesense_for_domain(
                domain=domain,
                snippets=snippets,
                host=host,
                port=port,
                api_key=api_key,
            )
            if client:
                logger.info(
                    f"TypeSense client registered for domain '{domain}' "
                    f"(is_available={client.is_available})"
                )
            else:
                logger.warning(
                    f"TypeSense initialization returned None for domain '{domain}' — "
                    "search_policy tools will fail"
                )
        except Exception as e:
            # Graceful degradation: log but don't fail the whole trial
            logger.warning(
                f"TypeSense initialization failed for domain '{domain}': {e} — "
                "search_policy tools will fail"
            )

    # =========================================================================
    # RAG Document Indexing
    # =========================================================================

    async def _index_documents_for_trial(
        self,
        trial_id: str,
        search_config: Any,  # SearchConfig from models
        artifacts_dir: Path | None,
    ) -> None:
        """
        Index a trial's search corpus into the RAG service.

        The corpus travels in ``tool_artifacts`` and is extracted to
        *artifacts_dir*; a relative ``documents_path`` (the pack's declared
        ``corpus_dir``) is resolved against it as ``artifacts_dir /
        documents_path``, mirroring :meth:`_resolve_mcp_server_scripts`. An
        absolute ``documents_path`` is used literally (escape hatch).

        Args:
            trial_id: Unique trial identifier
            search_config: SearchConfig with documents_path and domain_name
            artifacts_dir: Directory the trial's tool_artifacts were extracted
                to, or ``None`` when the task shipped none

        Raises:
            RAGServiceError: if the RAG client is not configured, a relative
                corpus path cannot be resolved, or the resolved directory
                holds no documents — a declared corpus that indexes empty is a
                bundling bug, not an agent failure, so the trial hard-fails
                here rather than running against an empty index.
        """
        if self.rag_client is None:
            raise RAGServiceError("RAG client not configured")

        documents_path = search_config.documents_path
        domain_name = search_config.domain_name or "default"

        if not documents_path:
            raise RAGServiceError(
                f"Trial {trial_id}: search is enabled but documents_path is unset"
            )

        corpus_path = Path(documents_path)
        if not corpus_path.is_absolute():
            if artifacts_dir is None:
                raise RAGServiceError(
                    f"Trial {trial_id}: relative documents_path {documents_path!r} cannot be "
                    f"resolved — the task shipped no extracted artifacts directory"
                )
            corpus_path = artifacts_dir / corpus_path

        documents = load_documents_from_directory(str(corpus_path), domain_name)

        if not documents:
            raise RAGServiceError(
                f"Trial {trial_id}: no documents in resolved corpus {corpus_path} "
                f"(documents_path={documents_path!r}) — corpus bundling is broken"
            )

        logger.info(
            f"Indexing {len(documents)} documents for trial {trial_id}",
            extra={
                "trial_id": trial_id,
                "domain_name": domain_name,
                "documents_path": str(corpus_path),
            },
        )

        # Index documents in RAG service
        await self.rag_client.index_documents(
            trial_id=trial_id,
            domain_name=domain_name,
            documents=documents,
        )
