"""Docker runtime client for orchestrator

Provides a thin wrapper around the Runner gRPC service when running
Tolokaforge inside Docker. The agent loop stays inside the orchestrator
process, so only Runner connectivity is handled here.

This module provides:
- RunnerClient: Protocol declaring the seven-method runner-RPC surface
  (six per-trial RPCs plus a lifecycle health probe) callers depend on.
- GrpcRunnerClient: concrete gRPC implementation of RunnerClient.
- SharedStackRuntimeBackend: High-level wrapper for Docker runtime management.

See docs/GRPC_PROTOCOL.md for the full protocol specification.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import grpc
from testcontainers.compose import DockerCompose

from tolokaforge.core.compose_materialisation import (
    DB_SERVICE_DEFAULT,
    DB_SERVICE_PORT_DEFAULT,
    LogCaptureConfig,
    apply_network_policy_to_compose_file,
    capture_compose_service_logs,
    cleanup_partial_materialisation,
    compose_container_to_snapshot,
    copy_compose_context,
    make_project_temp_dir,
    resolve_env_endpoints,
    resolve_runner_endpoint,
    run_services_dir,
    shutdown_compose,
    write_capture_manifest,
)
from tolokaforge.core.models import SeedRef
from tolokaforge.core.run_display_events import (
    _NULL_EVENTS,
    ComponentSnapshot,
    ContainerSnapshot,
    RunDisplayEvents,
    build_component_id,
)
from tolokaforge.core.runtime import EnvHandle, IsolationMode, ProvisionError
from tolokaforge.core.trial import DEFAULT_TOOL_TIMEOUT_S, EnvEndpoints, EnvironmentManifest
from tolokaforge.docker.logging import LogRouter
from tolokaforge.runner import (
    ExecutionStatus,
    runner_pb2,
    runner_pb2_grpc,
)
from tolokaforge.tools.registry import ToolResult

if TYPE_CHECKING:  # pragma: no cover — type-only imports for provisioning surface
    from tolokaforge.core.trial import TrialSpec

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class RunnerClient(Protocol):
    """The runner-RPC surface :class:`SharedStackRuntimeBackend` delegates to.

    Nine methods — six per-trial RPCs plus a lifecycle triplet
    (``connect`` / ``close`` / ``health_check``) — cover every
    runner-side call the docker runtime makes on behalf of a
    :class:`RuntimeBackend`. A non-gRPC caller (in-process subprocess,
    remote conductor over a different transport) can satisfy this
    Protocol structurally without pulling in the gRPC stack.
    :class:`GrpcRunnerClient` is the sole production implementation.
    """

    def connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None: ...

    def close(self) -> None: ...

    def register_trial(
        self,
        trial_id: str,
        trial_spec_json: str,
        default_tool_timeout_s: float = DEFAULT_TOOL_TIMEOUT_S,
    ) -> dict: ...

    def execute_tool(
        self,
        trial_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        timeout_seconds: float = 30.0,
        executor: str = "agent",
    ) -> ToolResult: ...

    def grade_trial(
        self,
        trial_id: str,
        llm_messages_json: str | None = None,
        grading_components: list[str] | None = None,
    ) -> dict: ...

    def get_state(
        self,
        trial_id: str,
        include_unstable: bool = True,
        tables: list[str] | None = None,
    ) -> dict: ...

    def reset_trial(self, trial_id: str, execute_init_actions: bool = False) -> dict: ...

    def cleanup_trial(self, trial_id: str) -> dict: ...

    def health_check(self) -> bool: ...


def _proto_score_to_optional(value: float) -> float | None:
    """Convert proto sentinel -1.0 to None for unconfigured grade components.

    In the gRPC protocol, unconfigured grade components use -1.0 as a sentinel
    (protobuf float fields default to 0.0, so we can't use None directly).
    Convert negative sentinels to proper None for the Python ``GradeComponents`` model.
    """
    return None if value < 0 else value


class GrpcRunnerClient:
    """Client for communicating with Runner service via gRPC

    This client implements the Host side of the Host ↔ Runner protocol
    defined in docs/GRPC_PROTOCOL.md.

    Methods:
        - register_trial(): Initialize a trial with TaskDescription
        - execute_tool(): Execute a tool call from the LLM
        - grade_trial(): Compute grade for completed trial
        - get_state(): Get current state snapshot (debugging)
        - reset_trial(): Reset trial state to initial
        - cleanup_trial(): Forget a trial's registration (for retry-after-failure)
        - health_check(): Check service health
    """

    def __init__(
        self,
        runner_address: str = "runner:50051",
        *,
        events: RunDisplayEvents | None = None,
    ):
        """
        Initialize Runner client

        Args:
            runner_address: gRPC address for Runner service (TCP)
            events: Optional display-events sink. When provided,
                :meth:`connect` reports its progress through the
                Components monitoring channel (one row per attempt,
                same row updated in place) instead of scrolling
                per-attempt log lines. Defaults to the null sink so
                out-of-tree callers keep pre-existing behaviour.
        """
        self.runner_address = runner_address
        self.channel: grpc.Channel | None = None
        self.stub: runner_pb2_grpc.RunnerServiceStub | None = None
        self._events: RunDisplayEvents = events if events is not None else _NULL_EVENTS
        logger.info(f"GrpcRunnerClient initialized with address: {runner_address}")

    def _component_snapshot(self, phase: str, detail: str | None = None) -> ComponentSnapshot:
        return ComponentSnapshot(
            id=build_component_id("engine", "grpc.client", "runner"),
            kind="grpc.client",
            phase=phase,  # type: ignore[typeddict-item]
            detail=detail,
            owner="engine",
        )

    def connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None:
        """Establish connection to Runner service with health check retry.

        Reports progress through the Components monitoring channel: a
        single component row for ``engine/grpc.client/runner`` transitions
        ``pending → starting`` (with per-attempt ``detail`` updates that
        do not scroll the log stream) → ``healthy`` on success or
        ``unhealthy`` on timeout. Per-attempt log records stay at DEBUG
        so ``-v`` still surfaces them for debugging without the panel
        showing them by default.

        Args:
            timeout: Maximum time to wait for healthy service (seconds)
            retry_interval: Time between health check attempts (seconds)

        Raises:
            ConnectionError: If Runner not healthy after timeout
        """
        if self.channel is None:
            self.channel = grpc.insecure_channel(self.runner_address)
            self.stub = runner_pb2_grpc.RunnerServiceStub(self.channel)
            logger.info("Channel created to Runner service")

        component_id = build_component_id("engine", "grpc.client", "runner")
        self._events.component_registered(
            snapshot=self._component_snapshot(
                "starting", detail=f"connecting to {self.runner_address}"
            )
        )

        start_time = time.time()
        attempt = 0
        while time.time() - start_time < timeout:
            attempt += 1
            elapsed = time.time() - start_time
            try:
                if self.health_check():
                    logger.info(
                        f"Runner service healthy after {attempt} attempt(s), elapsed={elapsed:.2f}s"
                    )
                    self._events.component_status_changed(
                        snapshot=self._component_snapshot(
                            "healthy",
                            detail=f"{self.runner_address} · {attempt} attempt(s), {elapsed:.1f}s",
                        )
                    )
                    return
            except grpc.RpcError as e:
                # Tagged with component_id so ``_LogSink`` routes the record
                # into the component's tail widget instead of print_above'ing
                # over the panel. The log level stays at its natural value —
                # signal preserved, visualisation moved to the right channel.
                logger.warning(
                    f"Health check attempt {attempt} failed: {e}",
                    extra={"component_id": component_id},
                )

            # Per-attempt progress: updates the SAME row's ``detail`` in place.
            # The INFO line is tagged with component_id so ``_LogSink`` routes
            # it to the component tail rather than the general ring / above-panel
            # channel.
            logger.info(
                f"Waiting for Runner service (attempt {attempt}, "
                f"elapsed={elapsed:.1f}s/{timeout}s)",
                extra={"component_id": component_id},
            )
            self._events.component_status_changed(
                snapshot=self._component_snapshot(
                    "starting",
                    detail=f"attempt {attempt}, elapsed={elapsed:.1f}s/{timeout:.0f}s",
                )
            )
            time.sleep(retry_interval)

        # Timeout reached
        elapsed = time.time() - start_time
        self._events.component_status_changed(
            snapshot=self._component_snapshot(
                "unhealthy",
                detail=f"timeout after {elapsed:.1f}s ({attempt} attempts)",
            )
        )
        self._events.component_log_appended(
            component_id=component_id,
            level="ERROR",
            message=f"Runner service at {self.runner_address} not healthy after {elapsed:.1f}s",
            ts=time.time(),
        )
        raise ConnectionError(
            f"Runner service at {self.runner_address} not healthy after {elapsed:.1f}s "
            f"({attempt} attempts). Check if the Runner container is running."
        )

    def close(self):
        """Close connection to Runner service"""
        if self.channel:
            self.channel.close()
            self.channel = None
            self.stub = None
            logger.info("Disconnected from Runner service")

    def __enter__(self):
        """Context manager entry"""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.close()

    def register_trial(
        self,
        trial_id: str,
        trial_spec_json: str,
        default_tool_timeout_s: float = DEFAULT_TOOL_TIMEOUT_S,
    ) -> dict:
        """
        Register a new trial by sending a serialised TrialSpec to the runner.

        Args:
            trial_id: Unique identifier for this trial (format: "{task_id}:{trial_index}").
            trial_spec_json: ``TrialSpec`` as JSON string (see
                ``tolokaforge/core/trial.py``). The runner reads ``spec.task``
                for tool reconstruction and uses the rest of the spec for
                per-trial execution context.
            default_tool_timeout_s: Default timeout for tool execution.

        Returns:
            dict with keys:
                - success: bool
                - error: str (if failed)
                - tool_schemas: list of tool schema dicts
                - num_agent_tools: int
                - num_user_tools: int
        """
        if not self.stub:
            self.connect()

        try:
            request = runner_pb2.RegisterTrialRequest(
                trial_id=trial_id,
                trial_spec_json=trial_spec_json,
                default_tool_timeout_s=default_tool_timeout_s,
            )

            response = self.stub.RegisterTrial(request)

            # Convert tool schemas to dicts
            tool_schemas = []
            for schema in response.tool_schemas:
                tool_schemas.append(
                    {
                        "name": schema.name,
                        "description": schema.description,
                        "parameters": (
                            json.loads(schema.parameters_json) if schema.parameters_json else {}
                        ),
                        "category": schema.category,
                        "timeout_s": schema.timeout_s,
                    }
                )

            result = {
                "success": response.success,
                "error": response.error if response.error else None,
                "tool_schemas": tool_schemas,
                "num_agent_tools": response.num_agent_tools,
                "num_user_tools": response.num_user_tools,
            }

            if response.success:
                logger.info(
                    f"Registered trial {trial_id}: "
                    f"{response.num_agent_tools} agent tools, "
                    f"{response.num_user_tools} user tools"
                )
            else:
                logger.error(f"Failed to register trial {trial_id}: {response.error}")

            return result

        except grpc.RpcError as e:
            logger.error(f"gRPC error in register_trial: {e}")
            return {
                "success": False,
                "error": f"gRPC error: {str(e)}",
                "tool_schemas": [],
                "num_agent_tools": 0,
                "num_user_tools": 0,
            }

    def execute_tool(
        self,
        trial_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        timeout_seconds: float = 30.0,
        executor: str = "agent",
    ) -> ToolResult:
        """
        Execute a tool call

        Args:
            trial_id: Trial ID
            tool_name: Tool name to execute
            arguments: Tool arguments as dict
            timeout_seconds: Execution timeout
            executor: Which environment is making the call ("agent" or "user")

        Returns:
            ToolResult with execution results
        """
        if not self.stub:
            self.connect()

        try:
            request = runner_pb2.ExecuteToolRequest(
                trial_id=trial_id,
                tool_name=tool_name,
                arguments_json=json.dumps(arguments),
                timeout_seconds=timeout_seconds,
                executor=executor,
            )

            response = self.stub.ExecuteTool(request)

            # Map ExecutionStatus to success/error
            success = response.status == ExecutionStatus.EXECUTION_STATUS_SUCCESS
            error = None
            if not success:
                error = response.error_message or self._status_to_error(response.status)

            return ToolResult(success=success, output=response.output, error=error)

        except grpc.RpcError as e:
            logger.error(f"gRPC error in execute_tool: {e}")
            return ToolResult(success=False, output="", error=f"gRPC error: {str(e)}")

    def _status_to_error(self, status: int) -> str:
        """Convert ExecutionStatus enum to error message"""
        status_messages = {
            ExecutionStatus.EXECUTION_STATUS_UNSPECIFIED: "Unknown error",
            ExecutionStatus.EXECUTION_STATUS_ERROR: "Tool execution error",
            ExecutionStatus.EXECUTION_STATUS_TIMEOUT: "Tool execution timed out",
            ExecutionStatus.EXECUTION_STATUS_TOOL_NOT_FOUND: "Tool not found",
            ExecutionStatus.EXECUTION_STATUS_INVALID_ARGUMENTS: "Invalid arguments",
            ExecutionStatus.EXECUTION_STATUS_TRIAL_NOT_FOUND: "Trial not found",
        }
        return status_messages.get(status, f"Unknown status: {status}")

    def grade_trial(
        self,
        trial_id: str,
        llm_messages_json: str | None = None,
        grading_components: list[str] | None = None,
    ) -> dict:
        """
        Grade a completed trial

        Args:
            trial_id: Trial ID
            llm_messages_json: Optional LLM messages for transcript rules grading
            grading_components: Which components to compute (empty = all)

        Returns:
            dict with keys:
                - success: bool
                - error: str (if failed)
                - grade: dict with binary_pass, score, components, reasons, etc.
        """
        if not self.stub:
            self.connect()

        try:
            request = runner_pb2.GradeTrialRequest(
                trial_id=trial_id,
                llm_messages_json=llm_messages_json or "",
                grading_components=grading_components or [],
            )

            response = self.stub.GradeTrial(request)

            result = {
                "success": response.success,
                "error": response.error if response.error else None,
                "grade": None,
            }

            if response.success and response.grade:
                grade = response.grade
                result["grade"] = {
                    "binary_pass": grade.binary_pass,
                    "score": grade.score,
                    "reasons": grade.reasons,
                    "state_diff_json": grade.state_diff_json if grade.state_diff_json else None,
                    "components": {
                        "state_checks": (
                            _proto_score_to_optional(grade.components.state_checks)
                            if grade.components
                            else None
                        ),
                        "transcript_rules": (
                            _proto_score_to_optional(grade.components.transcript_rules)
                            if grade.components
                            else None
                        ),
                        "llm_judge": (
                            _proto_score_to_optional(grade.components.llm_judge)
                            if grade.components
                            else None
                        ),
                        "custom_checks": (
                            _proto_score_to_optional(grade.components.custom_checks)
                            if grade.components
                            else None
                        ),
                    },
                    "custom_checks": [
                        {
                            "check_name": cc.check_name,
                            "status": cc.status,
                            "score": cc.score,
                            "message": cc.message,
                            "details_json": cc.details_json,
                        }
                        for cc in grade.custom_checks
                    ],
                    # Per-criterion rubric-judge breakdown. Carried through to the
                    # Pydantic Grade.criterion_results so reviewers see which
                    # criterion failed and why.
                    "criterion_results": [
                        {
                            "id": cr.id,
                            "met": cr.met,
                            "score": cr.score,
                            "justification": cr.justification,
                        }
                        for cr in grade.criterion_results
                    ],
                    "judge_status": grade.judge_status,
                    # Judge accounting + audit transcript. The judge runs its own
                    # LLM in the Runner; its usage/cost and message transcript are
                    # surfaced here so the host writes them to the trial bundle
                    # (grade.yaml usage + judge_trajectory.yaml sidecar).
                    "judge_report": (
                        {
                            "calls": grade.judge_report.calls,
                            "prompt_tokens": grade.judge_report.prompt_tokens,
                            "completion_tokens": grade.judge_report.completion_tokens,
                            "reasoning_tokens": grade.judge_report.reasoning_tokens,
                            "cost_usd": grade.judge_report.cost_usd,
                            "tool_calls": grade.judge_report.tool_calls,
                            "transcript_json": grade.judge_report.transcript_json,
                        }
                        if grade.HasField("judge_report")
                        else None
                    ),
                }

            if response.success:
                logger.info(
                    f"Graded trial {trial_id}: pass={result['grade']['binary_pass']}, score={result['grade']['score']}"
                )
            else:
                logger.error(f"Failed to grade trial {trial_id}: {response.error}")

            return result

        except grpc.RpcError as e:
            logger.error(f"gRPC error in grade_trial: {e}")
            return {"success": False, "error": f"gRPC error: {str(e)}", "grade": None}

    def get_state(
        self, trial_id: str, include_unstable: bool = True, tables: list[str] | None = None
    ) -> dict:
        """
        Get current state snapshot for debugging

        Args:
            trial_id: Trial ID
            include_unstable: Whether to include unstable fields
            tables: Specific tables to return (empty = all)

        Returns:
            dict with keys:
                - success: bool
                - error: str (if failed)
                - state_json: str (current state as JSON)
                - stable_hash: str
                - full_hash: str
        """
        if not self.stub:
            self.connect()

        try:
            request = runner_pb2.GetStateRequest(
                trial_id=trial_id, include_unstable=include_unstable, tables=tables or []
            )

            response = self.stub.GetState(request)

            result = {
                "success": response.success,
                "error": response.error if response.error else None,
                "state_json": response.state_json if response.state_json else None,
                "stable_hash": response.stable_hash if response.stable_hash else None,
                "full_hash": response.full_hash if response.full_hash else None,
            }

            if response.success:
                logger.debug(f"Got state for trial {trial_id}: stable_hash={response.stable_hash}")
            else:
                logger.error(f"Failed to get state for trial {trial_id}: {response.error}")

            return result

        except grpc.RpcError as e:
            logger.error(f"gRPC error in get_state: {e}")
            return {
                "success": False,
                "error": f"gRPC error: {str(e)}",
                "state_json": None,
                "stable_hash": None,
                "full_hash": None,
            }

    def reset_trial(self, trial_id: str, execute_init_actions: bool = False) -> dict:
        """
        Reset trial state to initial for retries

        Args:
            trial_id: Trial ID
            execute_init_actions: Whether to re-execute initialization_actions

        Returns:
            dict with keys:
                - success: bool
                - error: str (if failed)
                - state_hash: str (hash after reset)
        """
        if not self.stub:
            self.connect()

        try:
            request = runner_pb2.ResetTrialRequest(
                trial_id=trial_id, execute_init_actions=execute_init_actions
            )

            response = self.stub.ResetTrial(request)

            result = {
                "success": response.success,
                "error": response.error if response.error else None,
                "state_hash": response.state_hash if response.state_hash else None,
            }

            if response.success:
                logger.info(f"Reset trial {trial_id}: state_hash={response.state_hash}")
            else:
                logger.error(f"Failed to reset trial {trial_id}: {response.error}")

            return result

        except grpc.RpcError as e:
            logger.error(f"gRPC error in reset_trial: {e}")
            return {"success": False, "error": f"gRPC error: {str(e)}", "state_hash": None}

    def cleanup_trial(self, trial_id: str) -> dict:
        """
        Forget a trial's registration so the same ``trial_id`` can be re-registered.

        Used by the orchestrator's retry path to discard the prior attempt's
        runner-side state before re-attempting registration. Idempotent on
        the server side: succeeds when the trial is already absent.

        Args:
            trial_id: Trial ID to forget

        Returns:
            dict with keys:
                - success: bool
                - error: str | None (gRPC or server-side error message)
        """
        if not self.stub:
            self.connect()

        try:
            request = runner_pb2.CleanupTrialRequest(trial_id=trial_id)
            response = self.stub.CleanupTrial(request)

            result = {
                "success": response.success,
                "error": response.error if response.error else None,
            }

            if response.success:
                logger.info(f"Cleaned up trial {trial_id}")
            else:
                logger.warning(f"Cleanup of trial {trial_id} returned error: {response.error}")

            return result

        except grpc.RpcError as e:
            logger.error(f"gRPC error in cleanup_trial: {e}")
            return {"success": False, "error": f"gRPC error: {str(e)}"}

    def health_check(self) -> bool:
        """Check if Runner service is healthy

        Returns:
            True if service is healthy

        A failed probe logs at ``ERROR`` — the record's real level, so
        ``-v`` inspection, external log processors, and post-mortem
        artifacts see it correctly. The log record is tagged with
        ``extra={"component_id": ...}``; ``_LogSink`` recognises that tag
        and routes the record into the component's tail widget instead of
        ``print_above``-ing over the Rich panel. Visualisation stays
        compact; signal is preserved.
        """
        if not self.stub:
            self.connect()

        try:
            response = self.stub.HealthCheck(runner_pb2.HealthCheckRequest())
            return response.status == "healthy"
        except grpc.RpcError as e:
            logger.error(
                f"Health check failed: {e}",
                extra={"component_id": build_component_id("engine", "grpc.client", "runner")},
            )
            return False

    def health_check_detailed(self) -> dict:
        """Get detailed health check information

        Returns:
            dict with status, version, num_active_trials, db_service_connected, available_adapters
        """
        if not self.stub:
            self.connect()

        try:
            response = self.stub.HealthCheck(runner_pb2.HealthCheckRequest())
            return {
                "status": response.status,
                "version": response.version,
                "num_active_trials": response.num_active_trials,
                "db_service_connected": response.db_service_connected,
                "available_adapters": list(response.available_adapters),
            }
        except grpc.RpcError as e:
            logger.error(f"Health check failed: {e}")
            return {
                "status": "unhealthy",
                "version": "",
                "num_active_trials": 0,
                "db_service_connected": False,
                "available_adapters": [],
                "error": str(e),
            }


# Backward compatibility alias
ExecutorClient = GrpcRunnerClient


@dataclass(frozen=True)
class _SharedStackHandle:
    """Handle returned by :meth:`SharedStackRuntimeBackend.provision`.

    Points at the run-wide shared stack rather than a per-trial materialisation.
    Two trials in the same run receive equivalent handles.
    """

    trial_id: str


class SharedStackRuntimeBackend:
    """Docker runtime manager - coordinates Runner connectivity

    This is a high-level wrapper that manages the GrpcRunnerClient lifecycle.
    Use as a context manager for automatic connection management.

    Example:
        with SharedStackRuntimeBackend("runner:50051") as runtime:
            if runtime.health_check():
                # Use runtime.runner_client for operations
                pass
    """

    isolation_mode: IsolationMode = IsolationMode.SHARED_STACK
    """Every trial in the run talks to the same runner container. Read by
    the orchestrator's compatibility check to refuse runs whose tasks
    require per-trial substrate materialisation."""

    advertised_capabilities: frozenset[str] = frozenset(
        {"shared_stack", "network_isolation:no_internet", "network_isolation:limited_internet"}
    )
    """Local-docker shared-stack capability advertisement. Read by
    :func:`tolokaforge.core.backend_capabilities.check_admission`."""

    def __init__(
        self,
        runner_address: str = "runner:50051",
        endpoints: EnvEndpoints | None = None,
        env_manifest: EnvironmentManifest | None = None,
        run_id: str = "run",
        seeds: dict[str, SeedRef] | None = None,
        log_capture: LogCaptureConfig | None = None,
        *,
        events: RunDisplayEvents | None = None,
    ):
        """Initialize the shared-stack runtime.

        Two mutually exclusive modes:

        **Built-in-stack mode** (``env_manifest`` is ``None``, the default).
        The orchestrator's built-in ``EngineStack`` has already brought up
        the shared runner + db-service, and its address / endpoints are
        passed in. ``connect`` just wires the gRPC client to that address.

        **Task-declared-stack mode** (``env_manifest`` is set). The run's
        tasks declared a shared, task-authored compose file. The backend
        materialises it **once at ``connect`` time**, resolves endpoints
        from the materialised stack, and wires the gRPC client to the
        task-declared runner. Every trial in the run shares that
        substrate; ``close`` tears it down. ``run_id`` becomes the temp-
        dir slug so docker compose auto-generates a unique project name
        per run.

        In built-in mode ``endpoints=None`` derives placeholder URLs from
        ``runner_address`` alone — a backwards-compat path for callers
        that construct the backend outside the orchestrator (typically
        tests).
        """
        self._env_manifest = env_manifest
        self._run_id = run_id
        self._compose: DockerCompose | None = None
        self._temp_dir: Path | None = None
        self._compose_log_routers: list[LogRouter] = []
        self.seeds: dict[str, SeedRef] = dict(seeds or {})
        self.log_capture = log_capture
        self._events: RunDisplayEvents = events if events is not None else _NULL_EVENTS

        self.runner_client: GrpcRunnerClient | None
        self._endpoints: EnvEndpoints | None
        if env_manifest is None:
            # Built-in-stack mode: runner + endpoints known at construction.
            self.runner_client = GrpcRunnerClient(runner_address, events=self._events)
            if endpoints is None:
                endpoints = EnvEndpoints(
                    db_url=f"http://{runner_address}/db",
                    rag_url=None,
                    runner_url=f"http://{runner_address}",
                )
            self._endpoints = endpoints
        else:
            # Task-declared-stack mode: runner + endpoints resolved at connect() time
            # from the materialised compose. Both are populated inside
            # :meth:`_materialise_manifest`.
            self.runner_client = None
            self._endpoints = None
            if endpoints is not None:
                raise ValueError(
                    "SharedStackRuntimeBackend: pass either env_manifest OR endpoints, "
                    "not both. env_manifest mode resolves endpoints from the "
                    "materialised compose stack at connect() time."
                )
        logger.info("Docker runtime initialized")

    def connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None:
        """Connect to Runner service with health check retry.

        In task-declared-stack mode (``env_manifest`` is set) this also
        materialises the task-declared compose stack once for the whole
        run and resolves endpoints from it before the gRPC client is
        wired.

        Args:
            timeout: Maximum time to wait for healthy service (seconds)
            retry_interval: Time between health check attempts (seconds)

        Raises:
            ProvisionError: if the task-declared stack fails to materialise
                or its declared runner / db service is not exposed.
            ConnectionError: If Runner not healthy after timeout.
        """
        if self._env_manifest is not None:
            self._materialise_manifest()
        self.runner_client.connect(timeout=timeout, retry_interval=retry_interval)
        logger.info("Docker runtime connected")

    def close(self):
        """Close Runner connection and tear down the task-declared stack
        (if any). Idempotent: safe to call before ``connect`` or twice.

        A runner-client close failure (e.g. broken gRPC channel) must not
        prevent the compose stack + temp dir from being cleaned up — the
        downstream teardown runs in a ``try/finally`` so a leaked docker
        project doesn't outlive the run.
        """
        try:
            if self.runner_client is not None:
                self.runner_client.close()
        finally:
            for router in self._compose_log_routers:
                try:
                    router.stop()
                except Exception:  # noqa: BLE001 — teardown must never mask compose cleanup
                    logger.exception(
                        "SharedStackRuntimeBackend: log router stop failed for container %r",
                        router.container_name,
                    )
            self._compose_log_routers = []
            if self._compose is not None:
                shutdown_compose(self._compose)
                self._compose = None
            if self._temp_dir is not None:
                shutil.rmtree(self._temp_dir, ignore_errors=True)
                self._temp_dir = None
        logger.info("Docker runtime closed")

    def _materialise_manifest(self) -> None:
        """Bring up the task-declared compose stack once for the run and
        wire ``self.runner_client`` + ``self._endpoints`` to it. Called
        from :meth:`connect` when ``env_manifest`` is set.

        Idempotent: a second call while the stack is up returns without
        re-materialising, matching :class:`GrpcRunnerClient.connect`'s
        ``if self.channel is None`` guard. A double-materialisation would
        leak the first stack + temp dir.

        Failure at any stage cleans up any partial materialisation
        before surfacing a :class:`ProvisionError` — the shared-stack
        equivalent of PerTrialRuntimeBackend's provision path, but with
        run scope instead of trial scope.
        """
        if self._compose is not None:
            # Already materialised — a second connect() (e.g. after a
            # transient reconnect) must not clobber the running stack.
            return
        assert self._env_manifest is not None  # narrowed by caller
        manifest = self._env_manifest

        temp_dir = make_project_temp_dir(self._run_id)
        compose: DockerCompose | None = None
        try:
            copy_compose_context(manifest.compose_file, temp_dir)
            apply_network_policy_to_compose_file(
                temp_dir / manifest.compose_file.name,
                manifest.network_policy,
                manifest.runner_service,
                manifest.limited_internet_allowlist,
                restricted_services=manifest.restricted_services,
            )
            compose = DockerCompose(
                context=str(temp_dir),
                compose_file_name=manifest.compose_file.name,
                pull=False,
                build=False,
                wait=True,
            )
            compose.start()
        except Exception as exc:  # noqa: BLE001 — surface as typed ProvisionError
            self._capture_materialise_failure_logs(compose)
            cleanup_partial_materialisation(compose, temp_dir)
            raise ProvisionError(
                trial_id=self._run_id,
                stage="provision",
                reason=f"docker compose up failed for shared task-declared stack: {exc}",
            ) from exc

        runner_endpoint = resolve_runner_endpoint(
            compose, manifest.runner_service, manifest.runner_port
        )
        if runner_endpoint is None:
            self._capture_materialise_failure_logs(compose)
            cleanup_partial_materialisation(compose, temp_dir)
            raise ProvisionError(
                trial_id=self._run_id,
                stage="provision",
                reason=(
                    f"runner_service {manifest.runner_service!r} does not expose port "
                    f"{manifest.runner_port} in the shared task-declared compose stack"
                ),
            )
        runner_host, runner_host_port = runner_endpoint

        # resolve_env_endpoints is best-effort for db_url + rag_url — a task
        # compose file that omits `db-service:8000` gets endpoints with
        # `db_url=None`. The runner-side DBServiceClient reads DB_SERVICE_URL
        # from its container env, and `db_json.py` tools fall back to the
        # same env var, so a missing db_url is not a provisioning failure.
        endpoints = resolve_env_endpoints(
            compose,
            runner_host,
            runner_host_port,
            db_service=manifest.db_service or DB_SERVICE_DEFAULT,
            db_port=manifest.db_port or DB_SERVICE_PORT_DEFAULT,
            rag_service=manifest.rag_service,
            rag_port=manifest.rag_port,
        )

        # Preserve the materialised state on the backend so close() can tear it down.
        self._compose = compose
        self._temp_dir = temp_dir
        self._endpoints = endpoints
        self.runner_client = GrpcRunnerClient(
            runner_address=f"{runner_host}:{runner_host_port}",
            events=self._events,
        )

        # Attach a LogRouter per container in the task-declared stack so
        # container stdout/stderr reaches the component tail widget. The
        # compose stack is already up at this point; a per-container router
        # failure MUST NOT abort provisioning — log it and continue with the
        # remaining containers.
        for container in compose.get_containers():
            if not container.ID:
                continue
            try:
                router = LogRouter(
                    container_name=container.Name or container.Service or "unknown",
                    container_id=container.ID,
                    component_id=build_component_id(
                        "engine",
                        "docker.service",
                        container.Service or "unknown",
                    ),
                )
                router.start()
            except Exception:  # noqa: BLE001 — router failure must not abort provisioning
                logger.exception(
                    "SharedStackRuntimeBackend: failed to attach log router for "
                    "container %r (service=%r)",
                    container.Name,
                    container.Service,
                )
                continue
            self._compose_log_routers.append(router)

    def _capture_materialise_failure_logs(self, compose: DockerCompose | None) -> None:
        """Best-effort run-level capture of the shared stack's per-service
        compose logs on a materialise failure, before the partial stack is
        torn down.

        No-op when ``self.log_capture is None``. Writes the ``.log`` files
        plus a ``services/_capture.yaml`` record
        (``capture_reason: "materialise_error"``) under
        ``<output_root>/services/`` — the run-level counterpart to
        :class:`PerTrialRuntimeBackend`'s per-trial provision capture.

        Wrapped in a fail-safe boundary: any capture error (compose parse,
        manifest write) is logged and swallowed so it never masks the
        :class:`ProvisionError` the caller is about to raise."""
        if self.log_capture is None:
            return
        try:
            assert self._env_manifest is not None  # narrowed by caller
            service_names = tuple(self._env_manifest.load_compose()["services"])
            dest_dir = run_services_dir(self.log_capture.output_root)
            captured = capture_compose_service_logs(
                compose, service_names, dest_dir, self.log_capture.tail
            )
            if captured:
                write_capture_manifest(
                    dest_dir,
                    self.log_capture.tail,
                    captured,
                    capture_reason="materialise_error",
                )
        except Exception:  # noqa: BLE001 — fail-safe: must never mask the ProvisionError
            logger.exception(
                "SharedStackRuntimeBackend: run-level materialise-failure log capture failed"
            )

    def health_check(self) -> bool:
        """Check health of Runner service"""
        return self.runner_client.health_check()

    def cleanup_trial(self, trial_id: str) -> dict:
        """Forget any prior registration of ``trial_id`` on the runner.

        The retry-cleanup path needs to call this *before* a per-trial
        adapter exists, so the method lives on the runtime (delegating
        to the gRPC client). Idempotent on the runner side.
        """
        return self.runner_client.cleanup_trial(trial_id)

    # ---- Per-trial provisioning (ADR-0010) — shared-stack compat path ----
    #
    # SharedStackRuntimeBackend keeps the run-wide shared-stack semantics that existed
    # before ADR-0010. The new methods satisfy the extended
    # ``RuntimeBackend`` Protocol without changing behaviour: provision
    # returns a handle pointing at the shared stack; endpoints returns the
    # run-wide URLs the shared stack exposes; teardown is a no-op because
    # the shared stack lives for the whole run and is torn down at
    # ``close``. Per-trial isolation is a ``PerTrialRuntimeBackend`` concern,
    # not a ``SharedStackRuntimeBackend`` concern.

    def provision(self, spec: TrialSpec) -> EnvHandle:
        """Return a handle pointing at the run-wide shared stack.

        No per-trial containers are brought up — the shared stack is
        already running from ``connect()``. Every trial receives an
        equivalent handle referencing the same stack.
        """
        return _SharedStackHandle(trial_id=spec.trial_id)

    def await_ready(self, handle: EnvHandle) -> None:  # noqa: ARG002 — Protocol conformance
        """No-op: the shared stack becomes ready at ``connect`` time, not
        per-trial. Health probes for the shared services are already
        applied by :meth:`connect`'s health-check loop."""

    def endpoints(self, handle: EnvHandle) -> EnvEndpoints:  # noqa: ARG002 — Protocol conformance
        """Return the run-wide shared-stack URLs.

        Same value for every trial in the run — the shared stack exposes
        one set of service addresses that all trials share. In built-in
        mode the orchestrator resolves these via ``_build_env_endpoints``
        at construction and passes them via ``endpoints``; in
        task-declared-stack mode :meth:`_materialise_manifest` resolves
        them from the materialised compose stack at ``connect`` time.
        Either way the value is snapshot on the backend by the time this
        method is called. Callers that need per-trial URLs should use a
        per-trial backend (e.g. ``PerTrialRuntimeBackend``, which
        resolves real URLs from its per-trial substrate).
        """
        return self._endpoints

    def teardown(self, handle: EnvHandle) -> None:  # noqa: ARG002 — Protocol conformance
        """No-op: the shared stack lives for the whole run and is torn
        down at :meth:`close`, not per-trial. Idempotent by construction."""

    def capture_service_logs(  # noqa: ARG002 — Protocol conformance
        self, handle: EnvHandle, *, capture_worthy: bool
    ) -> dict[str, int]:
        """Documented no-op returning ``{}``.

        The shared stack is run-wide, not trial-scoped, and per-trial
        :meth:`teardown` is a no-op — so per-trial log capture has no
        stack to read and would capture the same run-wide containers on
        every trial. Run-level capture on a shared-stack materialise
        failure is a separate surface (see
        ``docs/RUNTIME_BACKENDS.md``)."""
        return {}

    def get_infrastructure_snapshot(
        self, handle: EnvHandle
    ) -> list[ContainerSnapshot]:  # noqa: ARG002 — Protocol conformance
        """Return the shared-stack container snapshot.

        In task-declared-stack mode (``env_manifest`` is set) reads
        ``self._compose.get_containers()`` — every trial sees the same
        snapshot for the run's shared substrate. In built-in-stack mode
        (``self._compose is None``) returns an empty list: the services
        widget already covers the built-in ``EngineStack``.
        """
        if self._compose is None:
            return []
        try:
            containers = self._compose.get_containers()
        except Exception:  # noqa: BLE001 — display must never raise past orchestrator
            logger.exception(
                "SharedStackRuntimeBackend.get_infrastructure_snapshot: docker ps failed"
            )
            return []
        return [compose_container_to_snapshot(c) for c in containers]

    # ---- Per-trial RPC operations (ADR-0013) ----
    # Thin delegates to ``self.runner_client``. Kept as explicit methods
    # (not ``__getattr__`` proxy magic) so the ``RuntimeBackend`` Protocol
    # surface is discoverable in the class definition.

    def register_trial(
        self,
        trial_id: str,
        trial_spec_json: str,
        default_tool_timeout_s: float = DEFAULT_TOOL_TIMEOUT_S,
    ) -> dict:
        return self.runner_client.register_trial(
            trial_id=trial_id,
            trial_spec_json=trial_spec_json,
            default_tool_timeout_s=default_tool_timeout_s,
        )

    def execute_tool(
        self,
        trial_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        timeout_seconds: float = 30.0,
        executor: str = "agent",
    ) -> ToolResult:
        return self.runner_client.execute_tool(
            trial_id=trial_id,
            tool_name=tool_name,
            arguments=arguments,
            timeout_seconds=timeout_seconds,
            executor=executor,
        )

    def grade_trial(
        self,
        trial_id: str,
        llm_messages_json: str | None = None,
        grading_components: list[str] | None = None,
    ) -> dict:
        return self.runner_client.grade_trial(
            trial_id=trial_id,
            llm_messages_json=llm_messages_json,
            grading_components=grading_components,
        )

    def get_state(
        self,
        trial_id: str,
        include_unstable: bool = True,
        tables: list[str] | None = None,
    ) -> dict:
        return self.runner_client.get_state(
            trial_id=trial_id,
            include_unstable=include_unstable,
            tables=tables,
        )

    def reset_trial(self, trial_id: str, execute_init_actions: bool = False) -> dict:
        return self.runner_client.reset_trial(
            trial_id=trial_id,
            execute_init_actions=execute_init_actions,
        )

    def __enter__(self):
        """Context manager entry"""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.close()
