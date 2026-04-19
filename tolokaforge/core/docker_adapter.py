"""Adapters that wrap Docker runtime clients as standard tool executors.

This module provides adapter classes that wrap the RunnerClient to provide
a consistent interface for tool execution via Docker containers.

Classes:
    - DockerRunnerAdapter: Makes RunnerClient look like ToolExecutor
    - DockerExecutorAdapter: Backward compatibility alias
"""

import logging
from typing import Any

from tolokaforge.core.docker_runtime import RunnerClient
from tolokaforge.tools.registry import ToolResult

logger = logging.getLogger(__name__)


class DockerRunnerAdapter:
    """Adapter that makes RunnerClient look like ToolExecutor.

    This allows TrialRunner to use Docker Runner service transparently,
    delegating tool execution to the gRPC-based RunnerClient.

    Attributes:
        runner_client: The underlying gRPC client
        trial_id: Trial identifier for this adapter instance
        executor: Which environment is making calls ("agent" or "user")
        tool_logs: History of tool executions for metrics
    """

    def __init__(self, runner_client: RunnerClient, trial_id: str, executor: str = "agent"):
        """
        Initialize adapter

        Args:
            runner_client: gRPC client for Runner service
            trial_id: Trial ID
            executor: Which environment is making calls ("agent" or "user")
        """
        self.runner_client = runner_client
        # Keep executor_client as alias for backward compatibility
        self.executor_client = runner_client
        self.trial_id = trial_id
        self.executor = executor
        self.tool_logs: list[dict[str, Any]] = []
        logger.info(f"DockerRunnerAdapter initialized for trial {trial_id} (executor={executor})")

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        timeout_seconds: float = 30.0,
        **kwargs,
    ) -> ToolResult:
        """
        Execute tool via Docker Runner service

        This mimics the interface of ToolExecutor.execute()

        Args:
            tool_name: Name of tool to execute
            arguments: Tool arguments
            timeout_seconds: Execution timeout
            **kwargs: Additional arguments (merged into arguments)

        Returns:
            ToolResult with success, output, and error
        """
        # Merge arguments
        if arguments is None:
            arguments = {}
        arguments.update(kwargs)

        # Call Docker Runner service
        result = self.runner_client.execute_tool(
            trial_id=self.trial_id,
            tool_name=tool_name,
            arguments=arguments,
            timeout_seconds=timeout_seconds,
            executor=self.executor,
        )

        # Log for metrics (include duration_s for tool_usage aggregation)
        self.tool_logs.append(
            {
                "tool_name": tool_name,
                "tool": tool_name,
                "arguments": arguments,
                "executor": self.executor,
                "success": result.success,
                "output": result.output if result.success else None,
                "error": result.error if not result.success else None,
                "duration_s": result.duration_s,
            }
        )

        return result

    def get_logs(self) -> list[dict[str, Any]]:
        """Get tool execution logs"""
        return self.tool_logs

    def clear_logs(self):
        """Clear tool execution logs"""
        self.tool_logs = []

    def register_trial(
        self, task_description_json: str, default_tool_timeout_s: float = 30.0
    ) -> dict:
        """
        Register trial with Runner service

        Args:
            task_description_json: Full TaskDescription as JSON string
            default_tool_timeout_s: Default timeout for tool execution

        Returns:
            dict with success, error, tool_schemas, num_agent_tools, num_user_tools
        """
        return self.runner_client.register_trial(
            trial_id=self.trial_id,
            task_description_json=task_description_json,
            default_tool_timeout_s=default_tool_timeout_s,
        )

    def grade_trial(
        self,
        llm_messages_json: str | None = None,
        grading_components: list[str] | None = None,
    ) -> dict:
        """
        Grade the completed trial

        Args:
            llm_messages_json: Optional LLM messages for transcript rules grading
            grading_components: Which components to compute (empty = all)

        Returns:
            dict with success, error, grade
        """
        return self.runner_client.grade_trial(
            trial_id=self.trial_id,
            llm_messages_json=llm_messages_json,
            grading_components=grading_components,
        )

    def get_state(self, include_unstable: bool = True, tables: list[str] | None = None) -> dict:
        """
        Get current state snapshot

        Args:
            include_unstable: Whether to include unstable fields
            tables: Specific tables to return (empty = all)

        Returns:
            dict with success, error, state_json, stable_hash, full_hash
        """
        return self.runner_client.get_state(
            trial_id=self.trial_id, include_unstable=include_unstable, tables=tables
        )

    def reset_trial(self, execute_init_actions: bool = False) -> dict:
        """
        Reset trial state to initial

        Args:
            execute_init_actions: Whether to re-execute initialization_actions

        Returns:
            dict with success, error, state_hash
        """
        return self.runner_client.reset_trial(
            trial_id=self.trial_id, execute_init_actions=execute_init_actions
        )

    # Backward compatibility method
    def register_tools(self, tools: list[dict[str, Any]], env_config: dict[str, str]) -> bool:
        """
        DEPRECATED: Use register_trial() instead.

        This method is kept for backward compatibility but will raise
        a deprecation warning. The new architecture uses TaskDescription
        JSON instead of separate tools and env_config.

        Args:
            tools: List of tool definitions (ignored)
            env_config: Environment configuration (ignored)

        Returns:
            False (always fails - use register_trial instead)
        """
        import warnings

        warnings.warn(
            "register_tools() is deprecated. Use register_trial() with TaskDescription JSON instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        logger.warning(
            "register_tools() called but is deprecated. "
            "Use register_trial() with TaskDescription JSON instead."
        )
        return False


# Backward compatibility alias
DockerExecutorAdapter = DockerRunnerAdapter
