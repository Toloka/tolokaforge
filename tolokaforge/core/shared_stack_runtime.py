"""Docker runtime client for orchestrator

Provides a thin wrapper around the Runner gRPC service when running
Tolokaforge inside Docker. The agent loop stays inside the orchestrator
process, so only Runner connectivity is handled here.

This module provides:
- RunnerClient: Protocol declaring the seven-method runner-RPC surface
  (six per-trial RPCs plus a lifecycle health probe) callers depend on.
- GrpcRunnerClient: concrete gRPC implementation of RunnerClient.
- SharedStackRuntimeBackend: composer-driven runtime backend. Delegates
  every compose-mode operation to a :class:`SubstrateComposer` seam;
  built-in-stack mode wires straight to a :class:`GrpcRunnerClient`.

See docs/GRPC_PROTOCOL.md for the full protocol specification.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import grpc

from tolokaforge.core.compose_materialisation import LogCaptureConfig
from tolokaforge.core.grading.grade_components import GRADE_COMPONENTS
from tolokaforge.core.health import HealthLevel, HealthReport
from tolokaforge.core.models import SeedRef, TraceConstraintSeverity
from tolokaforge.core.run_display_events import (
    _NULL_EVENTS,
    ComponentSnapshot,
    ContainerSnapshot,
    RunDisplayEvents,
    build_component_id,
)
from tolokaforge.core.runtime import EnvHandle, IsolationMode
from tolokaforge.core.trial import DEFAULT_TOOL_TIMEOUT_S, EnvEndpoints, EnvironmentManifest
from tolokaforge.runner import (
    ExecutionStatus,
    runner_pb2,
    runner_pb2_grpc,
)
from tolokaforge.runner.models import PlanShape
from tolokaforge.runner.protocol import (
    ENGINE_PROTOCOL_VERSION,
    TrialNotRegisteredError,
    recorded_status,
)
from tolokaforge.tools.registry import ToolResult

if TYPE_CHECKING:  # pragma: no cover — type-only imports for provisioning surface
    from tolokaforge.core.composition_runtime import (
        ComposedEnvHandle,
        RunSubstrate,
        SubstrateComposer,
    )
    from tolokaforge.core.plugin_registry import RuntimeBackendBuildContext
    from tolokaforge.core.trial import TrialSpec

logger = logging.getLogger(__name__)


_DEFAULT_DB_SERVICE_URL = "http://tolokaforge-db-service:8000"
"""Runner-perspective DB service URL the docker stack injects into the runner
container at start (`tolokaforge/docker/stacks/core.py`). The orchestrator
mirrors the value on ``TrialSpec.env_endpoints`` so a future out-of-process
runner reads its service URLs from the spec instead of its own env."""

RUNNER_RESOLVES_TOOL_TIMEOUT = 0.0
"""The ``ExecuteToolRequest.timeout_seconds`` the engine sends on every call.

Zero tells the runner to resolve the budget the tool itself declares — the
runner is the only layer that knows which tool is about to run. The field stays
on the wire for engine/image skew: an older engine naming a positive budget is
still honoured by a new image, and a new engine's zero still resolves on an
older one."""


NETWORK_CAPABILITIES: frozenset[str] = frozenset(
    {"network_isolation:no_internet", "network_isolation:limited_internet"}
)
"""The two network-isolation capability names every local-docker backend
advertises — the run-wide default-deny + allowlist forwarders the compose
transform layer wires up regardless of plan shape."""


RESET_RECIPE_CAPABILITIES: frozenset[str] = frozenset(
    {
        "reset_recipes:sql_dump",
        "reset_recipes:filesystem_dir",
        "reset_recipes:redis_dump",
        "reset_recipes:bare",
    }
)
"""The four shipped reset-recipe capability names. Advertised whenever the
plan admits a ``reset``-labelled service — the "reset" dispatcher delegates
to :data:`RECIPE_REGISTRY` for these four kinds."""


def _normalise_runner_url(runner_address: str) -> str:
    """Prepend ``http://`` to a bare ``host:port`` runner address, leaving
    fully-qualified URLs untouched."""
    if runner_address.startswith(("http://", "https://")):
        return runner_address
    return f"http://{runner_address}"


def _build_env_endpoints(runner_address: str) -> EnvEndpoints:
    """Resolve the per-trial service URLs for inclusion in :class:`TrialSpec`.

    Field semantics:

    * ``runner_url`` — derived from the orchestrator's known runner
      address (the value passed to :class:`SharedStackRuntimeBackend`). Always set.
    * ``db_url`` — populated in built-in-stack mode from
      ``DB_SERVICE_URL`` in the environment (or the default the docker
      stack injects, ``_DEFAULT_DB_SERVICE_URL``). Env_manifest mode
      resolves it best-effort from the task-declared compose stack; a
      missing ``db-service`` leaves it ``None`` — see
      :class:`EnvEndpoints`.
    * ``rag_url`` — optional. Reads ``RAG_SERVICE_URL`` from the
      environment if set, otherwise stays ``None``. ``rag-service``
      ships in ``full_stack`` only, so a ``core_stack`` run with no
      override resolves to ``None`` — carrying a hardcoded RAG URL
      would point at a service that isn't running.
    """
    return EnvEndpoints(
        db_url=os.environ.get("DB_SERVICE_URL", _DEFAULT_DB_SERVICE_URL),
        rag_url=os.environ.get("RAG_SERVICE_URL"),
        runner_url=_normalise_runner_url(runner_address),
    )


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
        executor: str = "agent",
        *,
        call_id: str,
    ) -> ToolResult:
        """Execute one tool call, under
        :meth:`~tolokaforge.core.runtime.RuntimeBackend.execute_tool`'s contract —
        including its ``TrialNotRegisteredError``."""
        ...

    def grade_trial(
        self,
        trial_id: str,
        llm_messages_json: str | None = None,
        grading_components: list[str] | None = None,
        termination_reason: str | None = None,
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


def _wire_components_to_scores(grade: runner_pb2.Grade) -> dict[str, float | None]:
    """Lower a wire ``Grade`` into one score per registered component.

    Three things mean "not evaluated" and all three must reach ``None``: the
    ``-1.0`` sentinel, an absent ``components`` submessage, and an
    explicit-presence field the sending runner never set. The latter two decode
    as proto3's ``0.0`` default, which is indistinguishable from a real zero
    score unless presence is read rather than the value.
    """
    if not grade.HasField("components"):
        return {spec.name: None for spec in GRADE_COMPONENTS}

    components = grade.components
    declared = components.DESCRIPTOR.fields_by_name
    scores: dict[str, float | None] = {}
    for spec in GRADE_COMPONENTS:
        if declared[spec.name].has_presence and not components.HasField(spec.name):
            scores[spec.name] = None
            continue
        scores[spec.name] = _proto_score_to_optional(getattr(components, spec.name))
    return scores


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
                engine_protocol_version=ENGINE_PROTOCOL_VERSION,
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
        executor: str = "agent",
        *,
        call_id: str,
    ) -> ToolResult:
        """
        Execute a tool call

        Args:
            trial_id: Trial ID
            tool_name: Tool name to execute
            arguments: Tool arguments as dict
            executor: Which environment is making the call ("agent" or "user")
            call_id: The trial's episode-unique tool-call id, which the runner
                records with the call

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
                timeout_seconds=RUNNER_RESOLVES_TOOL_TIMEOUT,
                executor=executor,
                call_id=call_id,
            )

            response = self.stub.ExecuteTool(request)

            if response.status == ExecutionStatus.EXECUTION_STATUS_TRIAL_NOT_FOUND:
                raise TrialNotRegisteredError(trial_id, tool_name)

            # The fine-grained status the runner reported, carried through rather
            # than collapsed to ``success`` — it is what makes TIMEOUT /
            # TOOL_NOT_FOUND / INVALID_ARGUMENTS recordable on the docker path.
            # Raises for a status no trial records, so a status added to the proto
            # cannot arrive here and be recorded as an ordinary failure.
            status = recorded_status(response.status)

            success = response.status == ExecutionStatus.EXECUTION_STATUS_SUCCESS
            error = None
            if not success:
                error = response.error_message or self._status_to_error(response.status)

            return ToolResult(
                success=success,
                output=response.output,
                error=error,
                status=status,
            )

        except grpc.RpcError as e:
            logger.error(f"gRPC error in execute_tool: {e}")
            return ToolResult(success=False, output="", error=f"gRPC error: {str(e)}")

    def _status_to_error(self, status: int) -> str:
        """The sentence a failed call reports when the runner sent no message.

        Keyed by the recordable failure statuses and nothing else: the caller has
        already refused every status outside :data:`RECORDED_STATUS_BY_PROTO`, so
        a miss here is a status that reached a recorder without being mappable.
        """
        status_messages = {
            ExecutionStatus.EXECUTION_STATUS_ERROR: "Tool execution error",
            ExecutionStatus.EXECUTION_STATUS_TIMEOUT: "Tool execution timed out",
            ExecutionStatus.EXECUTION_STATUS_TOOL_NOT_FOUND: "Tool not found",
            ExecutionStatus.EXECUTION_STATUS_INVALID_ARGUMENTS: "Invalid arguments",
        }
        return status_messages[status]

    def grade_trial(
        self,
        trial_id: str,
        llm_messages_json: str | None = None,
        grading_components: list[str] | None = None,
        termination_reason: str | None = None,
    ) -> dict:
        """
        Grade a completed trial

        Args:
            trial_id: Trial ID
            llm_messages_json: Optional LLM messages for transcript rules grading
            grading_components: Which components to compute (empty = all)
            termination_reason: TerminationReason value naming how the trial
                ended; empty on the wire when the caller reports none

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
                termination_reason=termination_reason or "",
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
                    "components": _wire_components_to_scores(grade),
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
                    # Per-constraint trace-check verdicts, carried through to the
                    # Pydantic Grade.trace_check_results so grade.yaml shows which
                    # constraint failed and on which timeline positions.
                    "trace_checks": [
                        {
                            "id": tc.id,
                            "kind": tc.kind,
                            "passed": tc.passed,
                            "weight": tc.weight,
                            "message": tc.message,
                            "matched_positions": list(tc.matched_positions),
                            # A proto3 string carries no presence and "" is not a
                            # severity, so an empty one is a runner predating the
                            # field. Such a runner rejects a pack declaring severity
                            # at RegisterTrial, so its verdicts are all scored.
                            "severity": tc.severity or TraceConstraintSeverity.SCORED.value,
                            "undecided": tc.undecided,
                            "withheld": tc.withheld,
                        }
                        for tc in grade.trace_checks
                    ],
                    # Which route won and whether a gate shut. Absent — not empty —
                    # from a runner predating the field, and the host keeps that
                    # apart from a summary saying no gate failed.
                    "trace_checks_summary": (
                        {
                            "winning_path": grade.trace_checks_summary.winning_path,
                            "gate_failed": grade.trace_checks_summary.gate_failed,
                            "failed_gate_ids": list(grade.trace_checks_summary.failed_gate_ids),
                            "paths": [
                                {
                                    "id": path.id,
                                    "score": path.score,
                                    "gate_failed": path.gate_failed,
                                }
                                for path in grade.trace_checks_summary.paths
                            ],
                        }
                        if grade.HasField("trace_checks_summary")
                        else None
                    ),
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
                            "consistency_rejections": grade.judge_report.consistency_rejections,
                            "transcript_json": grade.judge_report.transcript_json,
                            "knowledge_search_disabled": grade.judge_report.knowledge_search_disabled,
                            "kb_tools_offered": list(grade.judge_report.kb_tools_offered),
                            "kb_tools_withheld": list(grade.judge_report.kb_tools_withheld),
                            "state_diff_text": grade.judge_report.state_diff_text,
                            "read_tools_offered": list(grade.judge_report.read_tools_offered),
                            "custom_system_prompt": grade.judge_report.custom_system_prompt,
                            # Omitted when the sending runner has no field 15 (legacy
                            # or version-skewed), so the parse-side include default
                            # fires rather than proto3's false wire default.
                            **(
                                {
                                    "include_agent_system_prompt": grade.judge_report.include_agent_system_prompt
                                }
                                if grade.judge_report.HasField("include_agent_system_prompt")
                                else {}
                            ),
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
                logger.warning(f"Failed to get state for trial {trial_id}: {response.error}")

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

    def health_report(self) -> HealthReport:
        """Query the runner's HealthCheck RPC and return a semantic
        :class:`~tolokaforge.core.health.HealthReport`.

        Primary API. Callers should ask domain questions via
        :meth:`~tolokaforge.core.health.HealthReport.is_reachable` /
        :meth:`~tolokaforge.core.health.HealthReport.is_fully_operational`
        rather than inspecting the raw protocol status string. The mapping
        from protocol strings (``healthy`` / ``degraded`` / ``unhealthy``,
        per ``docs/GRPC_PROTOCOL.md`` § HealthCheck) to
        :class:`~tolokaforge.core.health.HealthLevel` lives once in
        :meth:`HealthReport.from_status`; unknown status strings map to
        ``UNHEALTHY`` (fail-loud on protocol drift).

        A failed probe (``RpcError``) returns a ``HealthReport`` at
        ``UNHEALTHY`` carrying the exception message in ``detail`` and
        logs at ``ERROR`` — the record's real level, so ``-v`` inspection,
        external log processors, and post-mortem artifacts see it. The
        log record is tagged with ``extra={"component_id": ...}``;
        ``_LogSink`` recognises that tag and routes the record into the
        component's tail widget instead of ``print_above``-ing over the
        Rich panel. Visualisation stays compact; signal is preserved.
        """
        if not self.stub:
            self.connect()

        try:
            response = self.stub.HealthCheck(runner_pb2.HealthCheckRequest())
            return HealthReport.from_status(
                status=response.status,
                version=response.version,
            )
        except grpc.RpcError as e:
            logger.error(
                f"Health check failed: {e}",
                extra={"component_id": build_component_id("engine", "grpc.client", "runner")},
            )
            return HealthReport(level=HealthLevel.UNHEALTHY, detail=str(e))

    def health_check(self) -> bool:
        """Backwards-compat facade — True iff the runner is reachable for RPCs.

        Delegates to :meth:`health_report` and applies
        :meth:`~tolokaforge.core.health.HealthReport.is_reachable`. New
        callers should prefer :meth:`health_report` directly and use
        the semantic predicates, so the domain decision lives on the
        response wrapper rather than at each call site.
        """
        return self.health_report().is_reachable()

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
    """Runtime backend for compose-mode runs.

    Two mutually exclusive modes selected at construction:

    * **Built-in-stack mode** (``env_manifest is None``). The
      orchestrator's :class:`EngineStack` has already brought up the
      shared runner + db-service; :meth:`connect` wires a
      :class:`GrpcRunnerClient` to the injected ``runner_address``.
      Per-trial ``provision`` / ``teardown`` are no-ops. ``endpoints``
      returns the run-wide URLs snapshot at construction.
    * **env_manifest mode** (``env_manifest`` set). The backend delegates
      every substrate operation to a :class:`SubstrateComposer`:
      :meth:`connect` materialises run-scope stacks and any run-owned
      runner via :meth:`SubstrateComposer.materialise_run`;
      :meth:`provision` materialises task-scope and trial-scope stacks
      via :meth:`SubstrateComposer.provision_trial`; per-trial RPCs
      route through :meth:`SubstrateComposer.runner_client_for` with a
      deferred-connect gate for trial-owned runners.

    A third mode — ``_per_trial_mode=True`` — is set by
    :class:`PerTrialRuntimeBackend`'s ``__post_init__``. It routes
    :meth:`provision` through the composer with an empty
    :class:`RunSubstrate` so trial-scope-plan runs work without a
    materialised run scope.

    :attr:`isolation_mode` is computed from :attr:`EnvironmentManifest.plan_shape`
    when ``env_manifest`` is set; the built-in-stack and per-trial-delegate
    modes fall back on the ``_per_trial_mode`` flag:

    * ``env_manifest=None``, ``_per_trial_mode=False`` → ``SHARED_STACK``
      (built-in-stack mode; every trial shares the injected runner).
    * ``env_manifest=None``, ``_per_trial_mode=True`` → ``PER_TRIAL_STACK``
      (the per-trial delegate mode :class:`PerTrialRuntimeBackend` sets).
    * ``plan_shape=SINGLE_RUN`` → ``SHARED_STACK`` (one ``run``-scope stack
      lives for the whole run).
    * ``plan_shape=TRIAL_SCOPED_ONLY`` → ``PER_TRIAL_STACK`` (every stack is
      ``trial``-scope; the composer materialises fresh per trial).
    * ``plan_shape=TASK_SCOPED_ONLY`` / ``MULTI_SCOPE`` → ``COMPOSED_STACK``
      (the plan spans more than one scope; each scope's stacks stay live for
      their bracket).

    :attr:`advertised_capabilities` mirrors the same branching: each posture
    unions the scope-mode capability name with :data:`NETWORK_CAPABILITIES`;
    the ``PER_TRIAL_STACK`` and ``COMPOSED_STACK`` postures also union
    :data:`RESET_RECIPE_CAPABILITIES`. Read by
    :func:`tolokaforge.core.backend_capabilities.check_admission`.
    """

    def __init__(
        self,
        runner_address: str = "runner:50051",
        endpoints: EnvEndpoints | None = None,
        env_manifest: EnvironmentManifest | None = None,
        run_id: str = "run",
        seeds: Mapping[str, SeedRef] | None = None,
        log_capture: LogCaptureConfig | None = None,
        *,
        mount_docker_socket: bool = False,
        events: RunDisplayEvents | None = None,
        composer: SubstrateComposer | None = None,
        connect_timeout: float = 30.0,
        connect_retry_interval: float = 1.0,
    ):
        from tolokaforge.core.default_substrate_composer import DefaultSubstrateComposer

        self._env_manifest = env_manifest
        self._run_id = run_id
        self.seeds: dict[str, SeedRef] = dict(seeds or {})
        self.log_capture = log_capture
        self._mount_docker_socket = mount_docker_socket
        self._events: RunDisplayEvents = events if events is not None else _NULL_EVENTS
        self.composer: SubstrateComposer = (
            composer if composer is not None else DefaultSubstrateComposer()
        )
        self.connect_timeout = connect_timeout
        self.connect_retry_interval = connect_retry_interval
        self._per_trial_mode: bool = False
        self._run_substrate: RunSubstrate | None = None
        self._env_handles: dict[str, ComposedEnvHandle] = {}
        self._connected_trials: set[str] = set()

        self.runner_client: GrpcRunnerClient | None
        self._endpoints: EnvEndpoints | None
        if env_manifest is None:
            self.runner_client = GrpcRunnerClient(runner_address, events=self._events)
            if endpoints is None:
                endpoints = EnvEndpoints(
                    db_url=f"http://{runner_address}/db",
                    rag_url=None,
                    runner_url=f"http://{runner_address}",
                )
            self._endpoints = endpoints
        else:
            self.runner_client = None
            self._endpoints = None
            if endpoints is not None:
                raise ValueError(
                    "SharedStackRuntimeBackend: pass either env_manifest OR endpoints, "
                    "not both. env_manifest mode resolves endpoints from the "
                    "materialised compose stack at connect() time."
                )
        logger.info("Shared-stack runtime initialized")

    @property
    def isolation_mode(self) -> IsolationMode:
        if self._env_manifest is None:
            return (
                IsolationMode.PER_TRIAL_STACK
                if self._per_trial_mode
                else IsolationMode.SHARED_STACK
            )
        shape = self._env_manifest.plan_shape
        if shape is PlanShape.SINGLE_RUN:
            return IsolationMode.SHARED_STACK
        if shape is PlanShape.TRIAL_SCOPED_ONLY:
            return IsolationMode.PER_TRIAL_STACK
        return IsolationMode.COMPOSED_STACK

    @property
    def advertised_capabilities(self) -> frozenset[str]:
        mode = self.isolation_mode
        if mode is IsolationMode.SHARED_STACK:
            return frozenset({"shared_stack"}) | NETWORK_CAPABILITIES
        if mode is IsolationMode.PER_TRIAL_STACK:
            return frozenset({"per_trial_stack"}) | RESET_RECIPE_CAPABILITIES | NETWORK_CAPABILITIES
        return frozenset({"composed_stack"}) | RESET_RECIPE_CAPABILITIES | NETWORK_CAPABILITIES

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None:
        """Connect to the Runner service with health-check retry.

        In built-in-stack mode this delegates straight to the injected
        :class:`GrpcRunnerClient`. In env_manifest mode the composer
        materialises the run — bringing up every run-scope stack and
        (when a run-scope stack owns the runner) wiring the client —
        idempotently: a second call with :attr:`_run_substrate` set
        returns early without re-materialising.

        Args:
            timeout: Maximum time to wait for healthy service (seconds).
            retry_interval: Time between health-check attempts (seconds).

        Raises:
            ProvisionError: env_manifest mode; the composer fails to
                materialise a stack or the declared runner service does
                not expose its port.
            ConnectionError: built-in-stack mode; the runner is not
                healthy within ``timeout``.
        """
        if self._env_manifest is None and not self._per_trial_mode:
            self.runner_client.connect(timeout=timeout, retry_interval=retry_interval)
            logger.info("Shared-stack runtime connected")
            return

        if self._per_trial_mode:
            # No run scope to materialise for trial-scope-only plans; each
            # trial's stack comes up via provision() → composer.provision_trial().
            return

        if self._run_substrate is not None:
            return
        from tolokaforge.core.composition_runtime import RunCtx

        ctx = RunCtx(
            run_id=self._run_id,
            manifest=self._env_manifest,
            mount_docker_socket=self._mount_docker_socket,
            log_capture=self.log_capture,
            events=self._events,
            seeds=self.seeds,
        )
        self._run_substrate = self.composer.materialise_run(
            plan=list(self._env_manifest.stacks), ctx=ctx
        )
        if self._run_substrate.runner_client is not None:
            self._run_substrate.runner_client.connect(
                timeout=timeout, retry_interval=retry_interval
            )
        logger.info("Shared-stack runtime connected")

    def close(self) -> None:
        """Close the Runner connection and tear the composer state down.

        Built-in-stack mode closes the injected :class:`GrpcRunnerClient`.
        Per-trial mode + env_manifest mode hand any leftover trial handles
        to :meth:`SubstrateComposer.teardown_trial` first (a trial the
        orchestrator never explicitly tore down still needs its stack
        removed), then hand the run substrate to
        :meth:`SubstrateComposer.teardown_run` which owns runner-client
        close and run-scope stack teardown. Every composer-side teardown
        is wrapped so a single failure never masks the sibling cleanup.
        """
        if self._env_manifest is None and not self._per_trial_mode:
            if self.runner_client is not None:
                self.runner_client.close()
            logger.info("Shared-stack runtime closed")
            return

        try:
            for env_handle in list(self._env_handles.values()):
                try:
                    self.composer.teardown_trial(env_handle)
                except Exception:  # noqa: BLE001 — one trial's teardown must not mask siblings
                    logger.exception(
                        "SharedStackRuntimeBackend.close: composer teardown_trial failed for %s",
                        env_handle.trial_id,
                    )
            if self._run_substrate is not None:
                self.composer.teardown_run(self._run_substrate)
        except Exception:  # noqa: BLE001 — teardown must not raise past caller
            logger.exception("SharedStackRuntimeBackend.close: composer teardown failed")
        finally:
            self._run_substrate = None
            self._env_handles = {}
            self._connected_trials = set()
        logger.info("Shared-stack runtime closed")

    def health_check(self) -> bool:
        """Report whether the Runner is reachable via its gRPC channel.

        Per-trial mode aggregates over the trials whose runner client has
        already connected (via first per-trial RPC use); no connected
        trials means trivially healthy — the run has nothing that a
        health probe can reach yet, and reporting ``False`` would defeat
        lazy-connect. Every other mode probes the single runner client
        the run owns.
        """
        if self._per_trial_mode:
            for trial_id in self._connected_trials:
                env_handle = self._env_handles.get(trial_id)
                if env_handle is None:
                    continue
                client = self.composer.runner_client_for(self._run_substrate_or_empty(), env_handle)
                if not client.health_check():
                    return False
            return True
        client = self._resolve_health_client()
        if client is None:
            return False
        return client.health_check()

    def _resolve_health_client(self) -> RunnerClient | None:
        """Pick the runner client the health probe should reach.

        Built-in-stack mode returns the injected :class:`GrpcRunnerClient`;
        env_manifest mode reads the run substrate's runner client (a
        materialise that has not yet run returns ``None`` — the probe
        answers ``False`` rather than raising).
        """
        if self._env_manifest is None and not self._per_trial_mode:
            return self.runner_client
        if self._run_substrate is None:
            return None
        return self._run_substrate.runner_client

    # ------------------------------------------------------------------
    # Per-trial provisioning (ADR-0010)
    # ------------------------------------------------------------------

    def provision(self, spec: TrialSpec) -> EnvHandle:
        """Provision the per-trial substrate.

        Built-in-stack mode returns an inert :class:`_SharedStackHandle`
        — the shared stack is already running from :meth:`connect`.
        env_manifest and per-trial modes hand the spec's plan to
        :meth:`SubstrateComposer.provision_trial`, cache the returned
        :class:`ComposedEnvHandle` under ``spec.trial_id``, and return
        it. Per-trial mode passes an empty :class:`RunSubstrate` (no
        run scope) so trial-scope-plan runs work without
        :meth:`materialise_run` having run.
        """
        if self._env_manifest is None and not self._per_trial_mode:
            return _SharedStackHandle(trial_id=spec.trial_id)

        manifest = spec.task.environment_manifest
        # An absent manifest travels into the composer, which raises the
        # canonical ProvisionError with stage="provision" via
        # ``_require_manifest`` — the backend does not pre-empt that refusal.
        if manifest is not None and not manifest.stacks and manifest.compose_file is not None:
            # Scalar-form manifest that has not been through
            # ``project_loader.resolve``: synthesise the composition plan
            # in place so ``composer.provision_trial`` sees at least one
            # StackDecl. Preserves byte-identical behaviour for pre-ADR-0044
            # task packs whose adapters (or tests) construct manifests
            # directly rather than through the loader.
            from tolokaforge.core.project_loader import _synthesise_composition_plan

            _synthesise_composition_plan(manifest, {})
        plan = list(manifest.stacks) if manifest is not None else []
        env_handle = self.composer.provision_trial(
            plan=plan,
            spec=spec,
            run_sub=self._run_substrate_or_empty(),
        )
        self._env_handles[spec.trial_id] = env_handle
        return env_handle

    def await_ready(self, handle: EnvHandle) -> None:  # noqa: ARG002 — Protocol conformance
        """No-op: readiness is enforced by the composer at provision time
        (trial-scope stacks) or by :meth:`connect`'s health-check loop
        (run-scope runner). The Protocol keeps this method as an
        explicit lifecycle affordance for backends whose readiness lags
        provision."""

    def endpoints(self, handle: EnvHandle) -> EnvEndpoints:
        """Return the per-trial endpoint bundle.

        Built-in-stack mode returns the run-wide bundle snapshot at
        construction (identical for every trial). Otherwise the
        composer resolves per-trial endpoints from the handle it
        produced at :meth:`provision`.
        """
        if self._env_manifest is None and not self._per_trial_mode:
            assert self._endpoints is not None  # narrowed by mode invariant
            return self._endpoints
        from tolokaforge.core.composition_runtime import ComposedEnvHandle

        if not isinstance(handle, ComposedEnvHandle):
            raise TypeError(
                f"SharedStackRuntimeBackend.endpoints: composer path requires a "
                f"ComposedEnvHandle; got {type(handle).__name__}"
            )
        return self.composer.endpoints_for(self._run_substrate_or_empty(), handle)

    def teardown(self, handle: EnvHandle) -> None:
        """Tear the per-trial substrate down.

        Built-in-stack mode is a no-op — the shared stack lives for the
        whole run. Otherwise hands the :class:`ComposedEnvHandle` to
        :meth:`SubstrateComposer.teardown_trial`, drops the cached
        entry, and forgets any deferred-connect record for the trial.
        """
        if self._env_manifest is None and not self._per_trial_mode:
            return
        from tolokaforge.core.composition_runtime import ComposedEnvHandle

        if not isinstance(handle, ComposedEnvHandle):
            raise TypeError(
                f"SharedStackRuntimeBackend.teardown: composer path requires a "
                f"ComposedEnvHandle; got {type(handle).__name__}"
            )
        self.composer.teardown_trial(handle)
        self._env_handles.pop(handle.trial_id, None)
        self._connected_trials.discard(handle.trial_id)

    def capture_service_logs(self, handle: EnvHandle, *, capture_worthy: bool) -> dict[str, int]:
        """Capture per-service compose logs for a still-live trial stack.

        Built-in-stack mode: no-op (the shared stack is run-wide, so
        per-trial capture would duplicate the same containers on every
        trial). Composer path: reads the trial-scope handles from the
        :class:`ComposedEnvHandle` and dispatches to the materialiser's
        :meth:`ComposeMaterialiser.capture_logs`; a run-scope-only plan
        (empty ``trial_stack_handles``) skips capture because the
        run-scope handles are torn down at :meth:`close`, not
        :meth:`teardown`.
        """
        del capture_worthy  # signal only — presence in kwargs is the trigger
        if self._env_manifest is None and not self._per_trial_mode:
            return {}
        from tolokaforge.core.composition_runtime import ComposedEnvHandle

        if not isinstance(handle, ComposedEnvHandle) or not handle.trial_stack_handles:
            return {}
        if self.log_capture is None:
            return {}
        from tolokaforge.core.compose_materialisation import trial_services_dir

        dest_dir = trial_services_dir(self.log_capture.output_root, handle.trial_id)
        totals: dict[str, int] = {}
        materialiser = getattr(self.composer, "materialiser", None)
        if materialiser is None:
            return {}
        for stack_handle in handle.trial_stack_handles:
            service_names = tuple(getattr(stack_handle, "service_names", ()))
            if not service_names:
                continue
            captured = materialiser.capture_logs(
                stack_handle, service_names, dest_dir, self.log_capture.tail
            )
            for name, size in captured.items():
                totals[name] = totals.get(name, 0) + size
        return totals

    def get_infrastructure_snapshot(self, handle: EnvHandle) -> list[ContainerSnapshot]:
        """Return the container snapshot for the display path.

        Built-in-stack mode returns ``[]`` (the display already covers
        the built-in :class:`EngineStack`). env_manifest mode walks
        the run-scope stack handles for a SINGLE_RUN plan; per-trial
        mode walks the trial-scope handles carried on the
        :class:`ComposedEnvHandle`. Never raises past this boundary.
        """
        if self._env_manifest is None and not self._per_trial_mode:
            return []
        materialiser = getattr(self.composer, "materialiser", None)
        if materialiser is None:
            return []
        stack_handles = self._snapshot_stack_handles(handle)
        snapshots: list[ContainerSnapshot] = []
        for stack_handle in stack_handles:
            try:
                snapshots.extend(materialiser.get_containers(stack_handle))
            except Exception:  # noqa: BLE001 — display must never raise past orchestrator
                logger.exception(
                    "SharedStackRuntimeBackend.get_infrastructure_snapshot: "
                    "materialiser.get_containers failed for stack %r",
                    getattr(stack_handle, "stack_id", "<unknown>"),
                )
        return snapshots

    def _snapshot_stack_handles(self, handle: EnvHandle) -> tuple[Any, ...]:
        """Pick the handles the infrastructure snapshot should walk."""
        from tolokaforge.core.composition_runtime import ComposedEnvHandle

        if isinstance(handle, ComposedEnvHandle) and handle.trial_stack_handles:
            return handle.trial_stack_handles
        if self._run_substrate is not None:
            return self._run_substrate.run_stack_handles
        return ()

    # ------------------------------------------------------------------
    # Per-trial RPC operations (ADR-0013)
    # ------------------------------------------------------------------

    def register_trial(
        self,
        trial_id: str,
        trial_spec_json: str,
        default_tool_timeout_s: float = DEFAULT_TOOL_TIMEOUT_S,
    ) -> dict:
        return self._runner_client_for(trial_id).register_trial(
            trial_id=trial_id,
            trial_spec_json=trial_spec_json,
            default_tool_timeout_s=default_tool_timeout_s,
        )

    def execute_tool(
        self,
        trial_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        executor: str = "agent",
        *,
        call_id: str,
    ) -> ToolResult:
        return self._runner_client_for(trial_id).execute_tool(
            trial_id=trial_id,
            tool_name=tool_name,
            arguments=arguments,
            executor=executor,
            call_id=call_id,
        )

    def grade_trial(
        self,
        trial_id: str,
        llm_messages_json: str | None = None,
        grading_components: list[str] | None = None,
        termination_reason: str | None = None,
    ) -> dict:
        return self._runner_client_for(trial_id).grade_trial(
            trial_id=trial_id,
            llm_messages_json=llm_messages_json,
            grading_components=grading_components,
            termination_reason=termination_reason,
        )

    def get_state(
        self,
        trial_id: str,
        include_unstable: bool = True,
        tables: list[str] | None = None,
    ) -> dict:
        return self._runner_client_for(trial_id).get_state(
            trial_id=trial_id,
            include_unstable=include_unstable,
            tables=tables,
        )

    def reset_trial(self, trial_id: str, execute_init_actions: bool = False) -> dict:
        return self._runner_client_for(trial_id).reset_trial(
            trial_id=trial_id,
            execute_init_actions=execute_init_actions,
        )

    def cleanup_trial(self, trial_id: str) -> dict:
        """Forget any prior registration of ``trial_id`` on the runner.

        The retry-cleanup path calls this *before* provision has run for
        a given trial, so a cleanup on an unprovisioned trial returns
        the idempotent success payload rather than raising — no runner
        client exists to talk to. Built-in-stack mode always has a
        run-owned client and delegates unconditionally.
        """
        if self._env_manifest is None and not self._per_trial_mode:
            return self.runner_client.cleanup_trial(trial_id)
        if trial_id not in self._env_handles:
            return {"success": True, "error": None}
        return self._runner_client_for(trial_id).cleanup_trial(trial_id)

    # ------------------------------------------------------------------
    # Composer-mode helpers
    # ------------------------------------------------------------------

    def _runner_client_for(self, trial_id: str) -> RunnerClient:
        """Resolve the runner client for a per-trial RPC.

        Built-in-stack mode returns the shared client. Composer mode
        asks the composer for the client (run-owned or trial-owned per
        the plan shape) and applies the deferred-connect gate: a
        trial-owned runner is not connected until its first per-trial
        RPC arrives, mirroring today's :class:`PerTrialRuntimeBackend`
        semantics; a run-owned runner was already connected at
        :meth:`connect` time and skips the gate.
        """
        if self._env_manifest is None and not self._per_trial_mode:
            return self.runner_client
        env_handle = self._env_handles[trial_id]
        client = self.composer.runner_client_for(self._run_substrate_or_empty(), env_handle)
        if env_handle.trial_runner_client is not None and trial_id not in self._connected_trials:
            client.connect(
                timeout=self.connect_timeout,
                retry_interval=self.connect_retry_interval,
            )
            self._connected_trials.add(trial_id)
        return client

    def _run_substrate_or_empty(self) -> RunSubstrate:
        """Return :attr:`_run_substrate` or an empty stand-in.

        Per-trial mode never runs :meth:`materialise_run` — the composer
        still requires a :class:`RunSubstrate` argument on the trial
        path so it can resolve seeds / policies / events. The
        stand-in carries the same run-wide values the backend was
        constructed with but no live handles.
        """
        if self._run_substrate is not None:
            return self._run_substrate
        from tolokaforge.core.composition_runtime import RunSubstrate

        return RunSubstrate(
            run_id=self._run_id,
            run_stack_handles=(),
            task_stack_handles={},
            runner_client=None,
            endpoints=None,
            seeds=self.seeds,
            mount_docker_socket=self._mount_docker_socket,
            log_capture=self.log_capture,
            events=self._events,
        )

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def shared_runtime_backend_factory(
    ctx: RuntimeBackendBuildContext,
) -> SharedStackRuntimeBackend:
    """Build a :class:`SharedStackRuntimeBackend` from a build context.

    Three modes:

    * env_manifest set — materialise the task-authored compose plan; the
      composer walks run-scope stacks at :meth:`connect` and per-scope
      stacks at :meth:`provision`.
    * ``per_trial_mode`` set (env_manifest absent) — pin the per-trial
      branch: :meth:`connect` no-ops ``materialise_run`` and every
      :meth:`provision` materialises the trial's own compose plan via
      ``composer.provision_trial``. The orchestrator sets this when
      short-circuiting the run-scope extract on a fully trial-scoped plan.
    * neither set — built-in engine mode: the gRPC client dials
      ``ctx.runner_address`` and service URLs resolve via
      :func:`_build_env_endpoints`.
    """
    if ctx.env_manifest is not None:
        return SharedStackRuntimeBackend(
            env_manifest=ctx.env_manifest,
            run_id=ctx.run_id,
            seeds=ctx.seeds,
            log_capture=ctx.log_capture,
            mount_docker_socket=ctx.mount_docker_socket,
            events=ctx.events,
        )
    if ctx.per_trial_mode:
        backend = SharedStackRuntimeBackend(
            env_manifest=None,
            seeds=ctx.seeds,
            log_capture=ctx.log_capture,
            mount_docker_socket=ctx.mount_docker_socket,
            events=ctx.events,
        )
        backend._per_trial_mode = True
        return backend
    return SharedStackRuntimeBackend(
        runner_address=ctx.runner_address,
        endpoints=_build_env_endpoints(ctx.runner_address),
        seeds=ctx.seeds,
        events=ctx.events,
    )
