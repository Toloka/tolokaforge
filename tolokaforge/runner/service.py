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
from collections.abc import Callable, Collection
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import grpc
from pydantic import ValidationError

from tolokaforge.core.grading import composite
from tolokaforge.core.grading.check_runner import (
    CheckExecutor,
    validate_checks_module,
)
from tolokaforge.core.grading.checks_helpers import custom_checks_enabled
from tolokaforge.core.grading.checks_interface import CustomChecksConfig
from tolokaforge.core.grading.golden_replay import (
    FailedGoldenAction,
    GoldenReplayRecord,
    declared_failure,
    resolve_golden_action_names,
)
from tolokaforge.core.grading.grade_components import GRADE_COMPONENTS
from tolokaforge.core.grading.jsonpath_addressing import (
    addresses_the_database,
    block_addresses_the_database,
    unreachable_target,
)
from tolokaforge.core.grading.judge_model_provider import JudgeModelProvider
from tolokaforge.core.grading.judge_result import JudgeResult, JudgeStatus
from tolokaforge.core.grading.judge_tools import DelegatingReadTool
from tolokaforge.core.grading.kb_search import KnowledgeSearch, RagServiceKnowledgeSearch
from tolokaforge.core.grading.state_check_backend import StateCheckBackend
from tolokaforge.core.grading.substrate import (
    GradingSubstrate,
    InProcessGradingSubstrate,
)
from tolokaforge.core.grading.trace_timeline import (
    TimelineInconsistencyError,
    TrialTimeline,
    build_trial_timeline,
)
from tolokaforge.core.grading.transcript_rule_matcher import TranscriptRuleMatcher
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
from tolokaforge.core.plugin_registry import (
    load_custom_check_executor,
    load_judge_model_provider,
    load_rubric_evaluator,
    load_state_check_backend,
    load_transcript_rule_matcher,
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
    compose_runner_trial_verdict,
    compute_state_diff,
    resolve_state_checks_component,
)
from tolokaforge.runner.grading_ledger import (
    CUSTOM_CHECKS_DISABLED_SKIP,
    CUSTOM_CHECKS_KEY,
    EVALUATED,
    HASH_DISABLED_SKIP,
    LLM_JUDGE_KEY,
    NO_JUDGE_MESSAGES_SKIP,
    audit_accounted_keys,
    hash_family_accounting,
    hash_family_skip_accounting,
)
from tolokaforge.runner.harness_state import snapshot_container_filesystem
from tolokaforge.runner.id_resolution import (
    check_id_fields_against_seeded_tables,
    compute_diff_ops,
)
from tolokaforge.runner.models import (
    HashComparisonBasis,
    HashGradingResult,
    KeyAccountingRecord,
    RecordedToolCall,
    RunnerGradeComponents,
    RunnerInitialStateConfig,
    RunnerStateChecksConfig,
    SearchConfig,
    SearchPlane,
    StateDiff,
    TaskDescription,
    ToolExecutorIdentity,
    TraceChecksConfig,
    TraceChecksResult,
    TranscriptEvaluationResult,
    TranscriptRulesConfig,
    provisions_database,
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
from tolokaforge.runner.search_plane import (
    PartialTypeSenseAddressError,
    ResolvedSearchPlane,
    ResolvedTypeSenseBinding,
    resolve_search_plane,
    resolve_typesense_binding,
)
from tolokaforge.runner.tool_factory import (
    DockerComposeExecToolWrapper,
    MCPServerToolWrapper,
    RAGSearchToolWrapper,
    ToolCallOutcome,
    ToolFactory,
    ToolLifecycleContext,
    ToolReconstructionError,
    ToolWrapper,
)
from tolokaforge.tools.registry import ToolExecutionStatus, raised_tool_failure_text

logger = logging.getLogger(__name__)


def _first_docker_compose_exec_tool(
    tools: Collection[Callable],
) -> DockerComposeExecToolWrapper | None:
    """Return the first exec-capable wrapper among *tools*, or ``None``.

    Used from two grade-time paths — ``_grade_via_test_execution`` (running
    ``test.sh``) and ``_read_filesystem_for_state`` (snapshotting a harness
    trial's tree) — that both need to reach into the trial container via the
    same wrapper the runner already registered for the tool.
    """
    for tool in tools:
        if isinstance(tool, DockerComposeExecToolWrapper):
            return tool
    return None


# Service version
SERVICE_VERSION = "1.0.0"

# Session working root handed to lifecycle tools as ``ToolLifecycleContext.work_dir``.
AGENT_WORK_DIR = "/work"

# Head start the inner ``asyncio.wait_for`` gets over the outer future deadline in
# ``ExecuteTool``. Both enforce the same tool budget, so on an equal deadline the
# winner is scheduler noise — and an outer win reports a genuine timeout as ERROR.
_TOOL_TIMEOUT_SLACK_S = 5.0

# The documented read-only mcp_core TypeSense KB connector the agent uses. The
# judge is allowed to reuse this ONE reconstructed tool (read-only passthrough)
# so it reads the same corpus the agent did. It is a closed (mcp_core) tool, not
# a tolokaforge type, so it is matched by this documented name — instance
# detection is unavailable. This is the deliberate, narrow exception to the
# judge's "harness-owned allowlist" rule (no generic MCP-tool passthrough — we
# cannot classify arbitrary MCP tools' read-only-ness).
_SEARCH_POLICY_TOOL_NAME = "search_policy"


def _tool_registered_for_trial(name: str, registered: Collection[str]) -> str | None:
    """The runner resolves a golden-action name against the tools it registered.

    Golden actions are authored unprefixed, so a registered name ending in
    ``_<name>`` resolves too. Not core's rule, which matches the pack's ``TOOLS``
    map exactly; #815 owns unifying the two namespaces.
    """
    if name in registered:
        return name
    return next((candidate for candidate in registered if candidate.endswith(f"_{name}")), None)


async def _invoke_golden_tool(tool: Any, arguments: dict[str, Any]) -> ToolCallOutcome:
    """What a registered tool answered a replayed golden action with, and how it read the call.

    ``execute_call`` first, which every ``ToolWrapper`` carries and which is the one shape
    able to report a substrate declaring the call a failure beside the text it answered; the
    outcome it hands over is read for its type, its content — the output text, the flag's
    value — being its implementor's contract to keep. Every other shape reports no declared
    failure, having no substrate to hear from, and hands its answer back rather than dropping
    it, because the answer is what :func:`declared_failure` reads a reported failure out of:
    a shape whose return were dropped would record no failure a pack signalling through it
    declared.

    An answer this cannot read is refused, never recorded as a success — a registered object
    reachable through none of those shapes, an ``execute_call`` answering anything but a
    :class:`ToolCallOutcome`, and anything the arms above build an outcome from that is not
    the ``str`` every payload the runner reads is. The refusal names the offending shape and
    reaches the replay loop's ``except`` arm, which records the action as raised; a golden
    action read as having taken effect when nothing about it could be read is the world the
    trial is then hashed against being wrong with nothing said about it.
    """
    if hasattr(tool, "execute_call"):
        outcome = await tool.execute_call(arguments)
        if not isinstance(outcome, ToolCallOutcome):
            raise TypeError(
                f"A replayed golden action's execute_call answered {type(outcome).__name__!r} "
                "where the replay reads a ToolCallOutcome, so what the call did cannot be read."
            )
        return outcome
    if hasattr(tool, "execute"):
        return _readable_outcome(await tool.execute(arguments))
    if not callable(tool):
        raise TypeError(
            f"Registered tool of type {type(tool).__name__!r} answers a replayed golden action "
            "through neither execute_call, execute, nor a call, so nothing it did can be read."
        )
    if inspect.iscoroutinefunction(tool):
        return _readable_outcome(await tool(arguments))
    loop = asyncio.get_event_loop()
    return _readable_outcome(await loop.run_in_executor(None, lambda: tool(arguments)))


def _readable_outcome(answer: object) -> ToolCallOutcome:
    """The outcome a shape with no substrate to hear from answered, refused unless it reads.

    Refused rather than coerced when the answer is not a ``str``: ``str()`` of a mapping
    destroys the ``"error"`` key :func:`declared_failure` reads a reported failure out of, so
    coercing turns a failure a pack declared into a silent success — worse than either
    accepting or refusing it. A coroutine, which is what an ``async`` ``__call__`` hands the
    sync-executor arm since :func:`inspect.iscoroutinefunction` reads the object rather than
    its ``__call__``, is closed before the refusal so it draws no "never awaited" warning.

    The refusal covers a *success* mapping too, so a pack out of this tree signalling through
    a mapping-answering duck-typed callable gains a raised annotation on every golden action
    it replays, its verdict unchanged. A wrapper answering a :class:`ToolCallOutcome` through
    ``execute_call`` is read as it means and gains none.
    """
    if isinstance(answer, str):
        return ToolCallOutcome(output=answer, declared_failure=False)
    if inspect.iscoroutine(answer):
        answer.close()
    raise TypeError(
        f"A replayed golden action answered {type(answer).__name__!r} where the replay reads a "
        "str, so what the call did cannot be read from it."
    )


def _search_plane_context(
    trial_id: str,
    search_config: SearchConfig,
    resolved_plane: ResolvedSearchPlane | None,
    binding: ResolvedTypeSenseBinding | None,
) -> str:
    """The prefix every search-plane refusal opens with: trial, domain, plane, address, source.

    Both bases are the ones the resolvers returned. Re-deriving either here would
    let a message name a plane or an address the client was not built from.

    ``binding`` may be ``None`` for the "kb-task-in-a-typesense-disabled-run"
    refusal — a run without a TypeSense plane resolves no address to name; the
    prefix names "no address" in that case rather than crashing on
    ``binding.host``.
    """
    domain = search_config.domain_name or "default"
    plane = (
        f"{resolved_plane.plane.value} ({resolved_plane.basis.value})"
        if resolved_plane is not None
        else "none declared"
    )
    address = (
        f"TypeSense at {binding.host}:{binding.port} (from {binding.basis.value})"
        if binding is not None
        else "no TypeSense address resolved"
    )
    return f"Trial {trial_id}: domain '{domain}', search plane {plane}, {address}"


def _bundles_a_docindex(artifacts_dir: Path | None) -> bool:
    """Whether a ``docindex/`` corpus arrived in the trial's artifacts."""
    return artifacts_dir is not None and (artifacts_dir / "docindex").is_dir()


def _no_plane_refusal(
    trial_id: str, search_config: SearchConfig, binding: ResolvedTypeSenseBinding | None
) -> str:
    """Why a corpus no plane serves is refused — an adapter half-way through migrating.

    An adapter that stops emitting connection details before it declares
    ``search.plane`` produces a task the derivation cannot place: a knowledge base,
    a run whose stack offers TypeSense, and nothing saying the two belong together.
    Registration would otherwise succeed with no search client — the silently dead
    plane every other refusal here exists to prevent.
    """
    return (
        f"{_search_plane_context(trial_id, search_config, None, binding)} — the task declares a "
        f"knowledge base ('{search_config.documents_path}') that neither plane serves: "
        f"search.plane is unset and the task carries no connection details to derive it from, so "
        f"no search client is registered, and search.enabled is false, so no rag-service index is "
        f"built. Declare search.plane: typesense for a corpus this run's TypeSense serves, or "
        f"search.plane: rag_service with search.enabled: true."
    )


def _unreachable_state_checks_refusal(
    state_checks: RunnerStateChecksConfig, initial_state: RunnerInitialStateConfig
) -> str | None:
    """Why this ``state_checks`` block cannot be graded against this trial, if it cannot.

    The sentence alone; the caller names the trial it refused, so one call site decides
    how every refusal in this family opens on the wire.

    Two authoring defects leave ``GradeTrial`` with no state to read, and each is
    refused by name rather than scored, because the score either would produce is a
    component value the agent did not earn. A block reading the database of a task that
    provisions none reaches the DB client for a trial ``RegisterTrial`` never registered
    there; a ``path:`` rooted outside what the runner composes resolves against a
    JSONPath state built from the database alone, and scores ``0.0`` for a state never
    read.

    The authoring gate states the same rule before a trial is paid for, and neither
    point makes the other redundant: ``core.grading.config_validation`` is named in
    :data:`~tolokaforge.core._runner_subset.RUNNER_SUBSET_EXCLUDED_FILES`, so the gate
    ships in the base wheel and never inside the runner image, and it is skipped
    wholesale for a task whose grading source cannot be interrogated. A trial arriving
    here may never have been offered to it.
    """
    if block_addresses_the_database(state_checks.authored_state_sources()) and (
        not provisions_database(initial_state)
    ):
        if state_checks.hash_enabled:
            where = "state_checks.hash"
            declares = (
                f"is enabled and compares against {state_checks.hash_comparison_basis().value}"
            )
        else:
            assertion = next(a for a in state_checks.jsonpath_checks if addresses_the_database(a))
            where = "state_checks.jsonpaths"
            described = assertion.get("description")
            declares = f"declares path {assertion.get('path')!r}" + (
                f" ({described})" if described else ""
            )
        return (
            f"{where} {declares}, which reads the trial's database, but the task's "
            f"initial_state provisions none — no tables, no schemas, no unstable_fields "
            f"— so no DB service was registered for this trial and there is no state for "
            f"{where} to read. Seed the state the assertion reads under "
            f"initial_state.json_db, or drop {where} from the pack."
        )

    for assertion in state_checks.jsonpath_checks:
        target = unreachable_target(assertion)
        if target is None:
            continue
        # Only ``BEYOND_THE_RUNNERS_STATE`` (``agent`` / ``user`` /
        # ``mock_web_url`` / ``rag_corpus_dir``) reaches here. ``FILESYSTEM``
        # is graded by the runner via ``_read_agent_visible_filesystem`` and
        # ``TRIAL_DATABASE`` returns ``None`` from :func:`unreachable_target`.
        described = assertion.get("description")
        return (
            f"state_checks.jsonpaths declares path {assertion.get('path')!r}"
            + (f" ({described})" if described else "")
            + ", which addresses state neither substrate carries at grading time: the "
            "core engine composes agent / user / mock_web_url / rag_corpus_dir from a "
            "run's live env, none of which are the runner's to reach. Address the "
            "trial's database (rooted at db or tables), or drop the assertion."
        )
    return None


def _backstop_seconds(tool: Any, trial_default: float) -> float:
    """The band the runner applies around a call on *tool*.

    A :class:`ToolWrapper` names its own band, which for a tool enforcing a
    per-call budget of its own sits above that budget. Anything else — a bare
    callable injected onto a trial — has no band to name, so the trial's
    default stands.
    """
    if isinstance(tool, ToolWrapper):
        return tool.effective_timeout_s
    return trial_default


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
        default_timeout: Fallback band for a tool that names none of its own
        lifecycle_ctx: The context this trial's lifecycle tools were started with
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
        # The context this trial's lifecycle tools were started with, kept so a
        # tool whose session the backstop poisoned is rebuilt against the same
        # artifacts_dir and work_dir rather than a reconstruction of them.
        self.lifecycle_ctx: ToolLifecycleContext | None = None
        self._unusable_tools: dict[tuple[ToolExecutorIdentity, str], str] = {}

    def mark_tool_unusable(
        self, tool_name: str, executor: ToolExecutorIdentity, reason: str
    ) -> None:
        """Refuse every later call to this tool, naming *reason*."""
        self._unusable_tools[(executor, tool_name)] = reason

    def unusable_reason(self, tool_name: str, executor: ToolExecutorIdentity) -> str | None:
        """Why this tool can no longer be called, or ``None`` if it can."""
        return self._unusable_tools.get((executor, tool_name))

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
            call_id: The trial's episode-unique tool-call id, joining this call
                to its result
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
            check_executor: Executor for the pack's ``checks.py``. Defaults to
                the ``check_runner`` entry point of
                ``tolokaforge.custom_check_executors``; tests inject
                :class:`InMemoryCheckExecutor`.
        """
        self.db_client = db_client
        self.rag_client = rag_client
        self.check_executor: CheckExecutor = (
            check_executor
            if check_executor is not None
            else load_custom_check_executor("check_runner")()
        )
        self._judge_model_provider: JudgeModelProvider = load_judge_model_provider("litellm")()
        self._transcript_rule_matcher: TranscriptRuleMatcher = load_transcript_rule_matcher(
            "default"
        )()
        self._state_check_backends: dict[str, StateCheckBackend] = {
            "jsonpath": load_state_check_backend("jsonpath")(),
            "db_probes": load_state_check_backend("db_probes")(),
        }
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

        Delegates the extraction body to the shared
        :func:`tolokaforge.core.grading.tool_artifacts.extract_tool_artifacts`
        helper, then records the returned directory on
        :attr:`_artifact_dirs` so :meth:`_cleanup_trial_artifacts` /
        :meth:`shutdown` can remove it from ``sys.path`` and delete the
        tree when the trial ends.
        """
        from tolokaforge.core.grading.tool_artifacts import extract_tool_artifacts

        extract_dir = extract_tool_artifacts(trial_id, artifacts)
        self._artifact_dirs[trial_id] = extract_dir
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

        # This gate runs BEFORE the RAG one deliberately — a task declaring both
        # planes reports its TypeSense failure, which is the nearer one.
        search_plane_error = self._register_search_plane(
            trial_id, task_description.search, artifacts_dir
        )
        if search_plane_error is not None:
            # Extraction ran before this gate, so a refusal leaves the tmp dir
            # on disk and on ``sys.path`` — drop it as the custom-checks gate does.
            self._cleanup_trial_artifacts(trial_id)
            logger.error(f"RegisterTrial: {search_plane_error}")
            return pb2.RegisterTrialResponse(success=False, error=search_plane_error)

        # Create trial context with validated TaskDescription
        trial_context = TrialContextRuntime(
            trial_id=trial_id,
            task_description=task_description,
            default_timeout=request.default_tool_timeout_s or DEFAULT_TOOL_TIMEOUT_S,
            judge_model_config=trial_spec.judge_model_config,
        )

        # A task that provisions no database (e.g. an adapter grading via
        # `custom_checks` + an HTTP endpoint on a sidecar, driving no
        # runner-managed state) skips the DB call entirely, so the runner
        # provisions cleanly even when no `db-service` sits in the trial's
        # compose stack. FAIL FAST is preserved for trials that DO declare DB state.
        initial_state = task_description.initial_state
        if provisions_database(initial_state):
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
        err = check_id_fields_against_seeded_tables(
            id_fields, initial_state_data, context=f"RegisterTrial: {trial_id}", relaxed=relaxed
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
        # tool (e.g. a compose-backed sandbox) is provisioned the same way —
        # and over both registries, because either actor may be given one.
        trial_context.lifecycle_ctx = ToolLifecycleContext(
            trial_id=trial_id,
            artifacts_dir=str(artifacts_dir) if artifacts_dir is not None else None,
            work_dir=AGENT_WORK_DIR,
        )
        for tool in (*trial_context.agent_tools.values(), *trial_context.user_tools.values()):
            if getattr(tool, "has_lifecycle", False):
                try:
                    tool.start(trial_context.lifecycle_ctx)
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

        tool, refusal = self._resolve_tool_or_refuse(
            trial_context=trial_context,
            call_id=call_id,
            tool_name=tool_name,
            arguments=arguments,
            executor=executor,
        )
        if tool is None:
            return refusal

        timeout_seconds = request.timeout_seconds
        if timeout_seconds <= 0:
            timeout_seconds = _backstop_seconds(tool, trial_context.default_timeout)

        # Run async execution on dedicated event loop thread. The RPC timeout is
        # the effective deadline: the inner tool wrapper enforces the same value on
        # its own subprocess, so the two must agree — otherwise a slow tool (a long
        # harness CLI, a heavy compose exec) hits the 5-minute default and errors
        # while its subprocess is still writing.
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
                ),
                timeout=timeout_seconds + _TOOL_TIMEOUT_SLACK_S,
            )
            return result
        except Exception as e:
            # _execute_tool_async catches everything the tool raises, so what
            # reaches here is the bridge itself failing (the outer deadline, a
            # closed loop) rather than a tool outcome.
            logger.error(f"ExecuteTool: Unexpected error in async execution: {e}")
            logger.error(traceback.format_exc())
            return pb2.ExecuteToolResponse(
                status=pb2.EXECUTION_STATUS_ERROR,
                output="",
                error_message=f"Internal error: {type(e).__name__}",
                metrics=pb2.ToolMetrics(),
            )

    def _resolve_tool_or_refuse(
        self,
        *,
        trial_context: TrialContextRuntime,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        executor: ToolExecutorIdentity,
    ) -> tuple[Any, None] | tuple[None, pb2.ExecuteToolResponse]:
        """The tool that will serve this call, or the recorded refusal instead.

        Two ways a registered trial holds no tool able to answer: the name
        resolves to nothing in this executor's registry, or the tool's session
        could not be rebuilt after the backstop fired and there is nothing left
        to serve from.
        """
        tool = trial_context.get_tool(tool_name, executor)
        if tool is None:
            logger.warning(f"ExecuteTool: Tool not found: {tool_name} ({executor.value})")
            return None, self._reject_tool_call(
                trial_context=trial_context,
                call_id=call_id,
                tool_name=tool_name,
                arguments=arguments,
                executor=executor,
                status=pb2.EXECUTION_STATUS_TOOL_NOT_FOUND,
                error_message=f"Tool '{tool_name}' not found",
            )

        unusable = trial_context.unusable_reason(tool_name, executor)
        if unusable is None:
            return tool, None
        logger.warning(f"ExecuteTool: refusing {tool_name} ({executor.value}): {unusable}")
        return None, self._reject_tool_call(
            trial_context=trial_context,
            call_id=call_id,
            tool_name=tool_name,
            arguments=arguments,
            executor=executor,
            status=pb2.EXECUTION_STATUS_ERROR,
            error_message=unusable,
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

    async def _invoke_tool(
        self,
        tool: Any,
        tool_name: str,
        arguments: dict[str, Any],
        timeout_seconds: float,
    ) -> Any:
        """Call *tool* under the runner's band, whatever shape it takes.

        Raises :class:`asyncio.TimeoutError` when the band elapses. The band is
        a backstop: it cancels the await, but a tool running on a worker thread
        keeps running there, which is why a timed-out lifecycle tool has its
        session rebuilt by :meth:`_reset_backstopped_tool`.
        """
        if hasattr(tool, "execute"):
            return await asyncio.wait_for(tool.execute(arguments), timeout=timeout_seconds)
        if not callable(tool):
            raise TypeError(f"Tool {tool_name} is not callable")
        if inspect.iscoroutinefunction(tool):
            return await asyncio.wait_for(tool(arguments), timeout=timeout_seconds)
        loop = asyncio.get_event_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(None, lambda: tool(arguments)),
            timeout=timeout_seconds,
        )

    async def _reset_backstopped_tool(
        self,
        trial_context: TrialContextRuntime,
        tool: Any,
        tool_name: str,
        executor: ToolExecutorIdentity,
        timeout_seconds: float,
    ) -> str:
        """Rebuild a backstopped lifecycle tool's session, and say so in the call's message.

        The backstop abandons the worker thread rather than killing it, so a
        tool holding a session across calls is left with a reader still draining
        its pipe — and the next call on that session can read what it drains.
        Rebuilding before this call returns is what keeps that from happening;
        the agent's next call finds a clean session, so the transcript says so.

        A rebuild that fails leaves nothing usable behind, so the tool is marked
        for the trial and every later call on it is refused by name rather than
        served from a pipe nobody owns.

        Dispatch is by capability, never by adapter identity: a tool rebuilding
        into the same configuration it already held clears nothing, and is left
        alone rather than told the agent its session was reset.

        ``stop()`` / ``start()`` are synchronous and can take seconds — a
        SIGKILL wait plus, on the compose engine, reopening an exec — so they go
        through the executor. Running them on the event loop would stall every
        other trial this runner is serving.
        """
        timed_out = f"Tool execution timed out after {timeout_seconds}s"
        if not getattr(tool, "has_lifecycle", False) or not getattr(
            tool, "rebuild_clears_backstopped_state", False
        ):
            return timed_out
        try:
            lifecycle_ctx = trial_context.lifecycle_ctx
            if lifecycle_ctx is None:
                raise RuntimeError("the trial stored no ToolLifecycleContext at registration")
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._rebuild_session, tool, lifecycle_ctx)
        except Exception as e:
            reason = (
                f"Tool '{tool_name}' is unusable for the rest of this trial: its session "
                f"could not be rebuilt after a timeout ({type(e).__name__}: {e})"
            )
            logger.error(f"ExecuteTool: {reason}")
            logger.error(traceback.format_exc())
            trial_context.mark_tool_unusable(tool_name, executor, reason)
            return f"{timed_out}. {reason}"
        return f"{timed_out}. The tool's session was reset, so the next call starts clean."

    @staticmethod
    def _rebuild_session(tool: Any, lifecycle_ctx: ToolLifecycleContext) -> None:
        tool.stop()
        tool.start(lifecycle_ctx)

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
            try:
                result = await self._invoke_tool(tool, tool_name, arguments, timeout_seconds)

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
                logger.warning(f"ExecuteTool: {tool_name} timed out after {timeout_seconds}s")
                error_message = await self._reset_backstopped_tool(
                    trial_context, tool, tool_name, executor, timeout_seconds
                )

            except Exception as e:
                # Catch all exceptions from tool execution
                status = pb2.EXECUTION_STATUS_ERROR
                error_message = raised_tool_failure_text(e)
                logger.error(f"ExecuteTool: {tool_name} raised {type(e).__name__}: {e}")
                logger.error(traceback.format_exc())
        finally:
            # ``_reset_backstopped_tool`` awaits inside the try/except's
            # timeout window; ``_run_async`` cancels its slack-deadline
            # coroutine on RPC timeout, and cancellation cannot interrupt a
            # coroutine already blocked inside ``run_in_executor``. Without
            # this ``finally`` the ``trial_context.record(...)`` below is
            # skipped, and the runner-side ``GradeTrial`` reads a timeline
            # missing the call — a forbidden tool that ran shows as "never
            # called" and passes ``_check_disallowed_tool``. Latency is best-
            # effort even on cancellation; the write is not.
            latency_seconds = time.time() - start_time
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

    def _build_grading_substrate(
        self, trial_id: str, trial_context: TrialContextRuntime
    ) -> GradingSubstrate:
        """The single :class:`InProcessGradingSubstrate` the composite reads.

        Three factories carry the runner's DB / filesystem reads: STABLE for
        jsonpath, RAW for judge state-diff and custom_checks, and the
        agent-visible-filesystem walk for path-glob assertions. Each is
        memoised on first accessor call so a component the pack never
        reaches for costs no round-trip. ``initial_state`` rides
        ``TaskDescription.initial_state.tables`` — the pre-execution shape
        the judge diffs against.

        Filesystem reads are harness-aware: a trial whose adapter emitted
        ``agent_harness_command`` + ``agent_visible_dir`` gets its tree
        snapshotted from inside its own container via the exec-wrapper;
        every other trial reads back the runner's own ``AGENT_WORK_DIR``.
        """
        loop = self._loop
        db_client = self.db_client

        class _LoopBridgeDBReader:
            """Sync :class:`DBReader` seam bridging to the async DB client on ``loop``."""

            def get_state(self, tables: list[str] | None = None) -> dict[str, Any]:
                fut = asyncio.run_coroutine_threadsafe(db_client.get_state(trial_id, tables), loop)
                return fut.result(timeout=30.0).data

            def query(self, jsonpath: str) -> dict[str, Any]:
                fut = asyncio.run_coroutine_threadsafe(db_client.query(trial_id, jsonpath), loop)
                return {"results": fut.result(timeout=30.0).results}

        def _get_raw_state() -> dict[str, Any]:
            return self._run_async(self.db_client.get_state(trial_id)).data

        def _get_stable_state() -> dict[str, Any]:
            return self._run_async(self.db_client.get_stable_state(trial_id)).data

        def _get_filesystem_state() -> dict[str, str]:
            return self._read_filesystem_for_state(trial_id)

        return InProcessGradingSubstrate(
            db_reader=_LoopBridgeDBReader(),
            knowledge_search=trial_context.resolve_kb_search(),
            filesystem_root=self._judge_workspace_dir(trial_context),
            initial_state=trial_context.task_description.initial_state.tables or {},
            final_state_factory=_get_raw_state,
            final_state_stable_factory=_get_stable_state,
            filesystem_state_factory=_get_filesystem_state,
        )

    def _read_filesystem_for_state(self, trial_id: str) -> dict[str, str]:
        """Snapshot the trial's agent-visible files for jsonpath grading.

        Routes on task metadata: a trial whose adapter emitted
        ``agent_harness_command`` + ``agent_visible_dir`` runs its CLI inside a
        separate container, so ``/work/`` inside this runner is not the tree
        the assertions target. The exec-wrapper the runner already uses to
        drive the CLI is the same one used here to enumerate the tree under
        the container's agent-visible dir; every other trial reads back the
        runner's own ``AGENT_WORK_DIR``.

        Sync: the substrate's ``filesystem_state_factory`` is invoked from a
        worker thread inside :meth:`loop.run_in_executor`, so the blocking
        exec + workdir walk here land off-loop without further wrapping.
        """
        trial_context = self.trials.get(trial_id)
        if trial_context is not None:
            metadata = trial_context.task_description.metadata or {}
            agent_visible_dir = metadata.get("agent_visible_dir")
            if metadata.get("agent_harness_command") and isinstance(agent_visible_dir, str):
                bash_tool = _first_docker_compose_exec_tool(trial_context.agent_tools.values())
                if bash_tool is not None:
                    return snapshot_container_filesystem(bash_tool._exec_sync, agent_visible_dir)
                logger.warning(
                    f"GradeTrial: {trial_id} - harness trial has no exec-capable tool; "
                    "falling back to the runner's own /work/ walk"
                )
        return self._read_agent_visible_filesystem()

    def _read_agent_visible_filesystem(self) -> dict[str, str]:
        # Inverse of the RegisterTrial provisioner's
        # ``/env/fs/agent-visible/<rel>`` → ``/work/<rel>`` block: expose each
        # file back at its logical path so
        # ``$.filesystem['/env/fs/agent-visible/<rel>']`` resolves. Binary
        # files are skipped — ``contains:``/``equals:`` operators can only
        # match text. Symlinks are skipped too: the assertion vocabulary was
        # not designed to expose arbitrary container-readable paths reachable
        # via a link the agent dropped in ``/work/``.
        root = Path(AGENT_WORK_DIR)
        fs: dict[str, str] = {}
        if not root.is_dir():
            return fs
        for path in root.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            except OSError as exc:
                logger.warning(f"GradeTrial: could not read {path} for jsonpath state: {exc}")
                continue
            rel = path.relative_to(root)
            fs[f"/env/fs/agent-visible/{rel.as_posix()}"] = content
        return fs

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

        # One substrate carries every read the composite evaluators need. Its
        # three factories memoise on first accessor call so a component the
        # config never reaches for costs no DB round-trip and no filesystem
        # walk (jsonpath scoring reads STABLE, judge state-diff + custom_checks
        # read RAW, jsonpath reshaping merges the filesystem in).
        substrate = self._build_grading_substrate(trial_id, trial_context)

        # Get state_checks config (may name a hash source)
        state_checks_config = grading_config.state_checks

        if state_checks_config:
            state_checks_refusal = _unreachable_state_checks_refusal(
                state_checks_config, trial_context.task_description.initial_state
            )
            if state_checks_refusal is not None:
                logger.error(f"GradeTrial: {trial_id} - {state_checks_refusal}")
                return pb2.GradeTrialResponse(
                    success=False,
                    error=(
                        f"Trial {trial_id!r} cannot be graded as authored: {state_checks_refusal}"
                    ),
                )

        # A) HASH-BASED GRADING
        # Run hash grading when hash_enabled is set (even with no source, which
        # represents refusal tasks where the expected state == initial state).
        if state_checks_config and state_checks_config.hash_enabled:
            logger.info(
                f"GradeTrial: {trial_id} - Executing hash-based grading against "
                f"{state_checks_config.hash_comparison_basis().value}"
            )
            try:
                hash_result = await self._execute_hash_grading(
                    trial_id, trial_context, state_checks_config
                )
                components.hash_match = hash_result.hash_match
                components.hash_score = hash_result.hash_score
                state_diff = hash_result.state_diff
                accounted_keys.update(hash_family_accounting(hash_result.basis))
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
            accounted_keys.update(hash_family_skip_accounting(HASH_DISABLED_SKIP))

        # A.2/A.3) JSONPATH ASSERTIONS + DB PROBES.
        # The composite gates each read on the config's shape: a path-glob-only
        # pack fetches nothing; a DB-addressing pack fetches only STABLE DB
        # state; a filesystem-only-``path:`` pack fetches only the workspace
        # walk. A probe score is the state_checks component's only source —
        # a hash verdict or a jsonpath score declared beside a probe is
        # refused up front.
        if state_checks_config and (
            state_checks_config.jsonpath_checks or state_checks_config.db_probes
        ):
            # The composite is sync so its substrate reads (which bridge back to
            # this loop via ``run_coroutine_threadsafe``) land off-loop. Running
            # it directly on the loop would deadlock at the first factory call.
            state_reads_result = await self._loop.run_in_executor(
                None,
                lambda: composite.grade_state_checks_reads(
                    trial_id=trial_id,
                    config=state_checks_config,
                    substrate=substrate,
                    state_check_backends=self._state_check_backends,
                    logger=logger,  # type: ignore[arg-type]  # module logger, satisfies StructuredLogger protocol at runtime
                ),
            )
            if state_reads_result.jsonpath_score is not None:
                components.jsonpath_score = state_reads_result.jsonpath_score
                components.jsonpath_reasons = state_reads_result.jsonpath_reasons
            if state_reads_result.db_probe_score is not None:
                components.db_probe_score = state_reads_result.db_probe_score
                components.db_probe_reasons = state_reads_result.db_probe_reasons
            accounted_keys.update(state_reads_result.accounted_keys)

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
                    trial_id, llm_judge_config, llm_messages, trial_context, substrate=substrate
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
                    # The judge's raw aggregate; the required-criterion gate is applied
                    # by ``compose_runner_trial_verdict`` below.
                    components.llm_judge_score = judge_result.score
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
        (
            custom_checks_score,
            custom_check_wire_results,
            custom_checks_reasons,
        ) = await self._grade_custom_checks(
            trial_id, trial_context, llm_messages, substrate=substrate
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
            verdict = compose_runner_trial_verdict(
                components.model_dump(),
                grading_config.model_dump(),
                judge_gate_failed=judge_gate_failed,
                trace_gate_failed=trace_checks_result.gate_failed,
            )
        except ValueError as exc:
            logger.error(f"GradeTrial: {trial_id} - {exc}")
            return pb2.GradeTrialResponse(
                success=False,
                error=f"Trial {trial_id!r} is not gradeable: {type(exc).__name__}: {exc}",
            )
        # The gated component, not the judge's raw aggregate, is what the wire grade and
        # the reasons carry — so the write-back happens before either is built.
        components.llm_judge_score = verdict.judge_component
        components_dict = components.model_dump()
        score, binary_pass = verdict.score, verdict.binary_pass

        # Build reasons string
        state_diff_dict = state_diff.model_dump() if state_diff else None
        transcript_result_dict = transcript_result.model_dump() if transcript_result else None
        # Collected and joined once. The components' renderer contributes nothing for a
        # trial that scored nothing, and appending to its output would open that grade
        # with a separator.
        reason_segments = [
            build_grade_reasons(
                components_dict,
                state_diff_dict,
                transcript_result_dict,
                judge_reasons=judge_reasons or None,
                trace_checks_result=trace_checks_result.model_dump(mode="json"),
                golden_replay=hash_result.golden_replay if hash_result is not None else None,
                custom_checks_reasons=custom_checks_reasons,
            )
        ]
        if judge_status == pb2.JUDGE_STATUS_ERRORED:
            reason_segments.append(f"JUDGE ERRORED: {judge_reasons}")

        # A populated key whose evaluator was skipped scored nothing; say so on the
        # grade rather than letting the trial look fully evaluated.
        if audit.skip_notes:
            reason_segments.append("; ".join(audit.skip_notes))

        # The ledger's skip notes cover populated SCORED_CHECK keys; hash.weight is a
        # CONFIG_INPUT the fold can skip on its own, so it reports itself.
        if state_checks_slot.inert_weight_reason:
            reason_segments.append(state_checks_slot.inert_weight_reason)

        # A fold that counted nothing is not described by any component's reasons, so its
        # own sentence is what stops a 0.0 arriving beside components that all read as passing.
        if verdict.reason:
            reason_segments.append(verdict.reason)

        reasons = " | ".join(segment for segment in reason_segments if segment)

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
        """Runner-side wrapper for :func:`tolokaforge.core.grading.composite.grade_transcript_rules`.

        The composite owns the dispatch every deployment topology runs;
        this wrapper preserves the runner's internal callsite shape.
        """
        from tolokaforge.core.grading.composite import grade_transcript_rules

        return grade_transcript_rules(
            trial_id=trial_id,
            config=transcript_rules_config,
            timeline=timeline,
            matcher=self._transcript_rule_matcher,
            logger=logger,  # type: ignore[arg-type]  # module logger, satisfies StructuredLogger protocol at runtime
        )

    def _grade_trace_checks(
        self, trial_id: str, config: TraceChecksConfig, timeline: TrialTimeline
    ) -> TraceChecksResult:
        """Runner-side wrapper for :func:`tolokaforge.core.grading.composite.grade_trace_checks` (ADR-0039)."""
        from tolokaforge.core.grading.composite import grade_trace_checks

        return grade_trace_checks(
            trial_id=trial_id,
            config=config,
            timeline=timeline,
            logger=logger,  # type: ignore[arg-type]  # module logger, satisfies StructuredLogger protocol at runtime
        )

    async def _grade_llm_judge(
        self,
        trial_id: str,
        llm_judge_config: "LLMJudgeConfig",
        llm_messages: list[dict[str, Any]],
        trial_context: TrialContextRuntime,
        *,
        substrate: GradingSubstrate,
    ) -> "JudgeResult":
        """Delegate to :func:`composite.grade_llm_judge` over the runner's substrate.

        The composite owns the judge dispatch (rubric plumbing, forwarding to
        the resolved :class:`RubricEvaluator`); this wrapper collects the
        trial-context passthroughs (judge ``ModelConfig``, ``search_policy``
        connector reuse), constructs the ``RubricEvaluator`` from the run-level
        :attr:`_judge_model_provider` and per-trial customization flags,
        renders the ``initial → final`` state diff for the evaluator's opening
        message, and delegates. The ``InProcessGradingSubstrate`` built once
        by :meth:`_build_grading_substrate` at the outer level is shared
        here — the judge's read-only DB tools go through
        ``substrate.db_reader()``, the same ``_LoopBridgeDBReader`` closure the
        state-checks path uses.

        Sync-in-async: :func:`composite.grade_llm_judge` is a sync function whose
        substrate reads (and the evaluator's own DB tool calls) bridge back to
        this loop via ``run_coroutine_threadsafe``; driving it on the loop thread
        would deadlock at the first call. ``run_in_executor`` lands the work on
        a worker thread so the bridges resolve.
        """
        from tolokaforge.core.grading.composite import grade_llm_judge
        from tolokaforge.core.grading.rubric_evaluator import RubricEvaluatorContext

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

        initial_state = trial_context.task_description.initial_state
        id_fields = self._id_fields_for_trial(trial_id)
        unstable_fields = {(u.table_name, u.field_name) for u in initial_state.unstable_fields}
        # TypeSense KB plane: if the agent had the read-only ``search_policy`` tool
        # (the documented mcp_core TypeSense connector), let the judge reuse THAT
        # EXACT reconstructed tool via a read-only passthrough — same tool, query,
        # backend, and ranking the agent saw. Agent-tool coupling keeps this seam
        # runner-side; the composite receives the resolved list.
        extra_read_tools = self._build_judge_search_policy_tools(trial_context)

        customization = llm_judge_config.customization
        disable_knowledge_search = bool(customization and customization.disable_knowledge_search)
        custom_system_prompt = customization.system_prompt if customization else None
        include_agent_system_prompt = (
            customization.include_agent_system_prompt
            if customization and customization.include_agent_system_prompt is not None
            else True
        )
        rubric_evaluator = load_rubric_evaluator("llm_judge")(
            RubricEvaluatorContext(
                judge_model_provider=self._judge_model_provider,
                disable_knowledge_search=disable_knowledge_search,
                custom_system_prompt=custom_system_prompt,
                include_agent_system_prompt=include_agent_system_prompt,
            )
        )

        def _run() -> "JudgeResult":
            state_diff_text = composite.build_judge_state_diff(
                trial_id=trial_id,
                substrate=substrate,
                initial_state_schemas=list(initial_state.schemas),
                id_fields=id_fields,
                unstable_fields=unstable_fields,
                logger=logger,  # type: ignore[arg-type]  # module logger, satisfies StructuredLogger protocol at runtime
            )
            return grade_llm_judge(
                trial_id=trial_id,
                config=llm_judge_config,
                substrate=substrate,
                rubric_evaluator=rubric_evaluator,
                llm_messages=llm_messages,
                judge_model_config=judge_model_config,
                extra_read_tools=extra_read_tools,
                state_diff=state_diff_text,
                logger=logger,  # type: ignore[arg-type]  # module logger, satisfies StructuredLogger protocol at runtime
            )

        return await self._loop.run_in_executor(None, _run)

    async def _grade_custom_checks(
        self,
        trial_id: str,
        trial_context: TrialContextRuntime,
        llm_messages: list[dict[str, Any]],
        *,
        substrate: GradingSubstrate,
    ) -> tuple[float, list["pb2.CustomCheckResult"], str | None]:
        """Delegate to :func:`composite.grade_custom_checks` over the runner's substrate.

        The composite owns the custom-checks dispatch (config normalisation,
        artifacts-dir gating, degrade-to-empty on DB failure, executor drive,
        wire-result wrapping). This wrapper hands it the runner-owned pieces —
        the per-trial ``artifacts_dir``, the sync :class:`CheckExecutor`, the
        parsed ``TaskDescription``, and the ``InProcessGradingSubstrate`` built
        once by :meth:`_build_grading_substrate` at the outer level and shared
        with the state-checks + judge paths.

        Sync-in-async: :func:`composite.grade_custom_checks` is a sync function
        whose ``substrate.final_state`` bridge to :meth:`db_client.get_state`
        blocks via ``run_coroutine_threadsafe``, and whose ``check_executor.run``
        is itself blocking. ``run_in_executor`` lands both on a worker thread so
        the bridges resolve rather than deadlocking on this loop.
        """
        from tolokaforge.core.grading.composite import grade_custom_checks

        grading_config = trial_context.grading_config
        custom_config_raw = grading_config.custom_checks if grading_config else None
        return await self._loop.run_in_executor(
            None,
            lambda: grade_custom_checks(
                trial_id=trial_id,
                config=custom_config_raw,
                substrate=substrate,
                llm_messages=llm_messages,
                task_description=trial_context.task_description,
                artifacts_dir=self._artifact_dirs.get(trial_id),
                check_executor=self.check_executor,
                logger=logger,  # type: ignore[arg-type]  # module logger, satisfies StructuredLogger protocol at runtime
            ),
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
        state_checks: RunnerStateChecksConfig,
    ) -> HashGradingResult:
        """
        Execute hash-based grading algorithm.

        Steps:
        0. Select the comparison basis and resolve every golden-action name
        1. Get current trial stable hash
        2. Snapshot current state
        3. Reset to initial state
        4. Execute golden path actions
        5. Snapshot golden state (for diff if mismatch)
        6. Get golden stable hash
        7. Restore trial state
        8. Compare hashes
        9. If mismatch, compute state diff

        The basis is selected from ``state_checks`` once and returned on the result:
        only :attr:`HashComparisonBasis.GOLDEN_REPLAY` replays anything, so the other
        two compare the trial against the state step 3 restored — identically, since
        what separates them is which declaration asked for it, which is the runtime
        ledger's question rather than the verdict's.

        Args:
            trial_id: Trial identifier
            trial_context: Trial context with tools
            state_checks: The trial's state-check config, which names the source to
                compare against and the fields whose numeric-looking strings fold

        Returns:
            HashGradingResult with hash_match (the model derives hash_score from it),
            the basis the comparison was run against, an optional state_diff, and the
            record of how much of the golden path ran

        Raises:
            UnresolvableGoldenAction: an action names no tool registered for the trial,
                or names nothing at all. Raised before step 1, so the trial's database
                is untouched — steps 1-4 mutate it, and a trial whose grading failed on
                a pack defect must not be left holding the initial state.
        """
        # 0. Select the basis, then resolve every authored name
        basis = state_checks.hash_comparison_basis()
        golden_actions = (
            state_checks.golden_actions if basis is HashComparisonBasis.GOLDEN_REPLAY else []
        )
        numeric_string_fields = state_checks.numeric_string_fields
        resolved_tool_names = resolve_golden_action_names(
            [action.tool_name for action in golden_actions],
            candidates=trial_context.agent_tools.keys(),
            match=_tool_registered_for_trial,
        )

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
        replay_failures: list[FailedGoldenAction] = []

        for i, (registered_name, action) in enumerate(
            zip(resolved_tool_names, golden_actions, strict=True)
        ):
            tool_name = action.tool_name
            arguments = dict(action.arguments)  # copy

            tool = trial_context.agent_tools[registered_name]
            if registered_name != tool_name:
                logger.debug(
                    f"GradeTrial: Matched golden tool '{tool_name}' -> '{registered_name}'"
                )

            try:
                outcome = await _invoke_golden_tool(tool, arguments)
            except Exception as e:
                # Golden action failure — log with full traceback for debugging
                logger.error(f"GradeTrial: Golden action {i} ({tool_name}) failed: {e}")
                logger.error(traceback.format_exc())
                replay_failures.append(FailedGoldenAction.from_exception(i, tool_name, e))
                continue

            if outcome.declared_failure:
                logger.error(
                    f"GradeTrial: Golden action {i} ({tool_name}) was declared a failure by its "
                    f"substrate: {outcome.output}"
                )
                replay_failures.append(
                    FailedGoldenAction.from_substrate_failure(i, tool_name, outcome.output)
                )
                continue

            reported = declared_failure(outcome.output)
            if reported is None:
                logger.debug(f"GradeTrial: Golden action {i} executed: {tool_name}")
                continue

            logger.error(
                f"GradeTrial: Golden action {i} ({tool_name}) reported a failure: {reported}"
            )
            replay_failures.append(FailedGoldenAction.from_reported_failure(i, tool_name, reported))

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
            basis=basis,
            state_diff=state_diff,
            golden_replay=GoldenReplayRecord(
                authored=len(golden_actions), failures=tuple(replay_failures)
            ),
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

    def _id_fields_for_trial(self, trial_id: str) -> dict[str, str | list[str]]:
        """Return the id_fields map declared by the trial's grading config, or ``{}``.

        Callers always guard that the trial is registered before invoking, so
        indexed access here is deliberate — a missing trial is a bug in the caller.
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
        bash_tool = _first_docker_compose_exec_tool(trial_context.agent_tools.values())

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
            # Capability-driven (has_lifecycle), not adapter identity, and over both
            # registries because registration started both.
            for tool in (*trial_context.agent_tools.values(), *trial_context.user_tools.values()):
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

    def _read_docindex_snippets(
        self,
        artifacts_dir: Path | None,
        context: str,
    ) -> tuple[list[str], str | None]:
        """Read the trial's ``docindex/*.md`` corpus.

        Returns the snippets, or the reason the declared knowledge base cannot
        be used. The collection name is derived from every non-empty document,
        so a corpus read that skipped one would address a collection the
        host-side indexer never created.
        """
        docindex_dir = artifacts_dir / "docindex" if artifacts_dir else None
        snippets: list[str] = []
        if docindex_dir is not None and docindex_dir.is_dir():
            for md_file in sorted(docindex_dir.glob("*.md")):
                try:
                    content = md_file.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as e:
                    return [], (
                        f"{context} — the knowledge base is unusable: cannot read "
                        f"{md_file} ({e}). Every non-empty document feeds the collection "
                        f"name, so skipping it would address a collection the host-side "
                        f"index never created."
                    )
                if content.strip():
                    snippets.append(content)

        if not snippets:
            return [], (
                f"{context} — the knowledge base is unusable: the task declares one, but "
                f"no readable '*.md' arrived in the trial's artifacts (looked in "
                f"{docindex_dir}). The declared corpus did not survive bundling and "
                f"extraction."
            )
        return snippets, None

    def _register_search_plane(
        self, trial_id: str, search_config: SearchConfig, artifacts_dir: Path | None
    ) -> str | None:
        """Serve this task's corpus from the plane it declares, or say why nothing can.

        Returns ``None`` when the trial may proceed, which includes the common case
        of a task that declares no knowledge base at all.

        Three conditions decide whether a TypeSense client is registered: the plane
        serving this task's corpus is TypeSense, the task declares a corpus, and an
        address for that plane resolved. None of them is ``enabled`` — that flag
        means "this task needs rag-service" and gates the RAG indexing block, so a
        TypeSense-only domain sets ``enabled=False`` and still registers here.
        """
        try:
            binding = resolve_typesense_binding(search_config)
        except PartialTypeSenseAddressError as e:
            return str(e)

        resolved_plane = resolve_search_plane(search_config)
        served_by_typesense = (
            resolved_plane is not None and resolved_plane.plane is SearchPlane.TYPESENSE
        )
        task_declares_kb = search_config.documents_path is not None
        address_resolved = binding is not None

        if served_by_typesense and task_declares_kb and address_resolved:
            return self._init_typesense_for_trial(
                trial_id, search_config, resolved_plane, binding, artifacts_dir
            )
        # A KB-declaring task in a run with no TypeSense plane must refuse loudly:
        # otherwise nothing registers, every ``search_policy`` call fails on
        # paid turns, and the trial grades the agent for the misconfiguration.
        # Gating on ``address_resolved`` here was wrong — ``resolved_plane is
        # None`` already implies ``search_config.plane`` and ``search_config.host``
        # are both None, so ``address_resolved`` could only come from the stack
        # env, which is injected exclusively by a run that HAS TypeSense. The
        # KB-task-in-a-no-plane run therefore never triggered the refusal.
        # Dropping ``address_resolved`` from the predicate reaches the intended
        # cases; the refusal message names "no plane" independent of address
        # resolution.
        no_plane_serves_the_corpus = task_declares_kb and resolved_plane is None
        if no_plane_serves_the_corpus and not search_config.enabled:
            return _no_plane_refusal(trial_id, search_config, binding)
        if address_resolved and not task_declares_kb and _bundles_a_docindex(artifacts_dir):
            # A corpus arrived for a task declaring none: the gate above skipped
            # the plane, so registering would succeed with no search client.
            return (
                f"{_search_plane_context(trial_id, search_config, resolved_plane, binding)} — the "
                f"search declaration and the artifact bundle disagree: a 'docindex/' corpus "
                f"arrived in the trial's artifacts, but search.documents_path is unset, so no "
                f"search client is registered and every search_policy call would fail. Declare "
                f"search.documents_path for this task, or stop bundling the corpus."
            )
        return None

    def _init_typesense_for_trial(
        self,
        trial_id: str,
        search_config: SearchConfig,
        resolved_plane: ResolvedSearchPlane | None,
        binding: ResolvedTypeSenseBinding,
        artifacts_dir: Path | None,
    ) -> str | None:
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

        Returns ``None`` when the plane is usable, otherwise the reason
        registration must be refused: a trial whose search plane is dead spends
        paid turns on ``search_policy`` calls that cannot work, and grades the
        result as agent behaviour.
        """
        domain = search_config.domain_name or "default"
        context = _search_plane_context(trial_id, search_config, resolved_plane, binding)

        try:
            from mcp_core.search.typesense_registry import initialize_typesense_for_domain
        except ImportError as e:
            return (
                f"{context} — this runner image cannot provide a search client: "
                f"mcp_core.search.typesense_registry is not importable ({e})."
            )

        snippets, corpus_error = self._read_docindex_snippets(artifacts_dir, context)
        if corpus_error is not None:
            return corpus_error

        logger.info(
            f"Initialising TypeSense client for domain '{domain}': "
            f"host={binding.host}, port={binding.port}, basis={binding.basis.value}, "
            f"snippets={len(snippets)}"
        )

        try:
            client = initialize_typesense_for_domain(
                domain=domain,
                snippets=snippets,
                host=binding.host,
                port=binding.port,
                api_key=binding.api_key,
            )
        except Exception as e:
            return f"{context} — registering the search client failed: {e}"

        if client is None:
            return (
                f"{context} — the server is unreachable or refused the collection: "
                f"initialize_typesense_for_domain returned no client."
            )
        if not client.is_available:
            return f"{context} — the registered search client reports the server as unavailable."

        logger.info(f"TypeSense client registered for domain '{domain}'")
        return None

    # =========================================================================
    # RAG Document Indexing
    # =========================================================================

    async def _index_documents_for_trial(
        self,
        trial_id: str,
        search_config: SearchConfig,
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
