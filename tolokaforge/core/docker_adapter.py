"""Per-trial :class:`~tolokaforge.tools.registry.ToolExecutor` for the docker runtime.

:class:`DockerRunnerAdapter` binds a ``trial_id`` and an executor identity
(``"agent"`` / ``"user"``) to a :class:`RunnerClient`, exposing the
``ToolExecutor`` protocol (``execute()`` + ``tool_logs`` bookkeeping) that
:class:`TrialRunner` speaks to.

Every non-tool-execution RPC (``register_trial``, ``grade_trial``,
``get_state``, ``reset_trial``, ``cleanup_trial``) has moved onto
:class:`~tolokaforge.core.runtime.RuntimeBackend` per ADR-0013; call sites
use ``runtime.method(trial_id, ...)`` directly. This module is now scoped
strictly to the ``ToolExecutor`` shape.
"""

import logging
from typing import Any

from tolokaforge.core.docker_runtime import RunnerClient
from tolokaforge.tools.registry import ToolResult

logger = logging.getLogger(__name__)


class DockerRunnerAdapter:
    """Per-trial ``ToolExecutor`` backed by a :class:`RunnerClient`.

    Binds ``trial_id`` and executor identity (``"agent"`` / ``"user"``)
    to a :class:`RunnerClient` so :class:`TrialRunner` can call
    ``.execute(tool_name, arguments)`` without threading trial identity
    through every call. Records every tool execution on :attr:`tool_logs`
    so the metrics pipeline and the stuck-detector can read the per-trial
    tool history back via :meth:`get_logs`.

    All other per-trial RPCs are on :class:`RuntimeBackend` — see ADR-0013.
    """

    def __init__(self, runner_client: RunnerClient, trial_id: str, executor: str = "agent"):
        """
        Initialize adapter.

        Args:
            runner_client: RPC client for the runner service.
            trial_id: Trial identifier bound to every ``execute()`` call.
            executor: Environment identity for the runner-side routing
                (``"agent"`` or ``"user"``).
        """
        self.runner_client = runner_client
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
        """Execute a tool via the runner service under the bound
        ``trial_id`` / ``executor`` and record the call on
        :attr:`tool_logs`.

        Matches the :class:`~tolokaforge.tools.registry.ToolExecutor.execute`
        contract :class:`~tolokaforge.core.runner.TrialRunner` calls.
        """
        if arguments is None:
            arguments = {}
        arguments.update(kwargs)

        result = self.runner_client.execute_tool(
            trial_id=self.trial_id,
            tool_name=tool_name,
            arguments=arguments,
            timeout_seconds=timeout_seconds,
            executor=self.executor,
        )

        self.tool_logs.append(
            {
                "tool_name": tool_name,
                "tool": tool_name,
                "arguments": arguments,
                "executor": self.executor,
                "success": result.success,
                "output": result.output if result.success else None,
                "error": result.error if not result.success else None,
            }
        )

        return result

    def get_logs(self) -> list[dict[str, Any]]:
        """Return the per-trial tool execution history."""
        return self.tool_logs

    def clear_logs(self):
        """Reset the per-trial tool execution history."""
        self.tool_logs = []


# Backward compatibility alias
DockerExecutorAdapter = DockerRunnerAdapter
