"""Docker runtime client for orchestrator

Provides a thin wrapper around the Runner gRPC service when running
Tolokaforge inside Docker. The agent loop stays inside the orchestrator
process, so only Runner connectivity is handled here.

This module provides:
- RunnerClient: gRPC client for Host ↔ Runner communication
- DockerRuntime: High-level wrapper for Docker runtime management

See docs/GRPC_PROTOCOL.md for the full protocol specification.
"""

import json
import logging
import time
from typing import Any

import grpc

from tolokaforge.runner import (
    ExecutionStatus,
    runner_pb2,
    runner_pb2_grpc,
)
from tolokaforge.tools.registry import ToolResult

logger = logging.getLogger(__name__)


class RunnerClient:
    """Client for communicating with Runner service via gRPC

    This client implements the Host side of the Host ↔ Runner protocol
    defined in docs/GRPC_PROTOCOL.md.

    Methods:
        - register_trial(): Initialize a trial with TaskDescription
        - execute_tool(): Execute a tool call from the LLM
        - grade_trial(): Compute grade for completed trial
        - get_state(): Get current state snapshot (debugging)
        - reset_trial(): Reset trial state to initial
        - health_check(): Check service health
    """

    def __init__(self, runner_address: str = "runner:50051"):
        """
        Initialize Runner client

        Args:
            runner_address: gRPC address for Runner service (TCP)
        """
        self.runner_address = runner_address
        self.channel: grpc.Channel | None = None
        self.stub: runner_pb2_grpc.RunnerServiceStub | None = None
        logger.info(f"RunnerClient initialized with address: {runner_address}")

    def connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None:
        """Establish connection to Runner service with health check retry.

        Waits for the Runner service to become healthy before returning.
        This is important when starting containers, as the Runner may take
        time to initialize.

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

        # Wait for service to become healthy
        start_time = time.time()
        attempt = 0
        while time.time() - start_time < timeout:
            attempt += 1
            try:
                if self.health_check():
                    logger.info(
                        f"Runner service healthy after {attempt} attempt(s), "
                        f"elapsed={time.time() - start_time:.2f}s"
                    )
                    return
            except grpc.RpcError as e:
                logger.debug(f"Health check attempt {attempt} failed: {e}")

            logger.info(
                f"Waiting for Runner service (attempt {attempt}, "
                f"elapsed={time.time() - start_time:.1f}s/{timeout}s)"
            )
            time.sleep(retry_interval)

        # Timeout reached
        elapsed = time.time() - start_time
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
        self, trial_id: str, task_description_json: str, default_tool_timeout_s: float = 30.0
    ) -> dict:
        """
        Register a new trial with full TaskDescription

        Args:
            trial_id: Unique identifier for this trial (format: "{task_id}:{trial_index}")
            task_description_json: Full TaskDescription as JSON string
            default_tool_timeout_s: Default timeout for tool execution

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
                task_description_json=task_description_json,
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

            duration_s = 0.0
            if response.metrics:
                duration_s = response.metrics.latency_seconds

            return ToolResult(
                success=success, output=response.output, error=error, duration_s=duration_s
            )

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
                "judge_cost_usd": response.judge_cost_usd,
            }

            if response.success and response.grade:
                grade = response.grade
                result["grade"] = {
                    "binary_pass": grade.binary_pass,
                    "score": grade.score,
                    "reasons": grade.reasons,
                    "state_diff_json": grade.state_diff_json if grade.state_diff_json else None,
                    "components": {
                        "state_checks": grade.components.state_checks if grade.components else -1.0,
                        "transcript_rules": (
                            grade.components.transcript_rules if grade.components else -1.0
                        ),
                        "llm_judge": grade.components.llm_judge if grade.components else -1.0,
                        "custom_checks": (
                            grade.components.custom_checks if grade.components else -1.0
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

    def health_check(self) -> bool:
        """Check if Runner service is healthy

        Returns:
            True if service is healthy
        """
        if not self.stub:
            self.connect()

        try:
            response = self.stub.HealthCheck(runner_pb2.HealthCheckRequest())
            return response.status == "healthy"
        except grpc.RpcError as e:
            logger.debug(f"Health check failed (will retry): {e}")
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
            logger.debug(f"Health check failed (will retry): {e}")
            return {
                "status": "unhealthy",
                "version": "",
                "num_active_trials": 0,
                "db_service_connected": False,
                "available_adapters": [],
                "error": str(e),
            }


# Backward compatibility alias
ExecutorClient = RunnerClient


class DockerRuntime:
    """Docker runtime manager - coordinates Runner connectivity

    This is a high-level wrapper that manages the RunnerClient lifecycle.
    Use as a context manager for automatic connection management.

    Example:
        with DockerRuntime("runner:50051") as runtime:
            if runtime.health_check():
                # Use runtime.runner_client for operations
                pass
    """

    def __init__(self, runner_address: str = "runner:50051"):
        """
        Initialize Docker runtime

        Args:
            runner_address: gRPC address for Runner service
        """
        self.runner_client = RunnerClient(runner_address)
        # Keep executor_client as alias for backward compatibility
        self.executor_client = self.runner_client
        logger.info("Docker runtime initialized")

    def connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None:
        """Connect to Runner service with health check retry.

        Args:
            timeout: Maximum time to wait for healthy service (seconds)
            retry_interval: Time between health check attempts (seconds)

        Raises:
            ConnectionError: If Runner not healthy after timeout
        """
        self.runner_client.connect(timeout=timeout, retry_interval=retry_interval)
        logger.info("Docker runtime connected")

    def close(self):
        """Close Runner connection"""
        self.runner_client.close()
        logger.info("Docker runtime closed")

    def health_check(self) -> bool:
        """Check health of Runner service"""
        return self.runner_client.health_check()

    def __enter__(self):
        """Context manager entry"""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.close()
