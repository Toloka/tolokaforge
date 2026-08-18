"""Per-trial :class:`~tolokaforge.tools.registry.ToolExecuting` backed by a
:class:`~tolokaforge.core.runtime.RuntimeBackend`.

:class:`DockerRunnerAdapter` binds a ``trial_id`` and an executor identity
(``"agent"`` / ``"user"``) to a runtime backend, satisfying the
:class:`~tolokaforge.tools.registry.ToolExecuting` protocol (``execute()``) that
:class:`TrialRunner` speaks to. The runner service records the call on its side
of the wire; the host records it against the trial's
:class:`~tolokaforge.core.runner.TrialToolCallRecorder`. The adapter keeps
no history of its own.

Every non-tool-execution RPC (``register_trial``, ``grade_trial``,
``get_state``, ``reset_trial``, ``cleanup_trial``) lives on
:class:`~tolokaforge.core.runtime.RuntimeBackend` per ADR-0013; call sites
use ``runtime.method(trial_id, ...)`` directly. Tool execution routes
through the runtime backend too (``runtime.execute_tool(...)``), so this
module has no dependency on the concrete ``RunnerClient`` type — every
call flows through the ``RuntimeBackend`` seam.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from tolokaforge.tools.registry import ToolResult

if TYPE_CHECKING:  # pragma: no cover — type-only imports
    from tolokaforge.core.runtime import RuntimeBackend

logger = logging.getLogger(__name__)


class DockerRunnerAdapter:
    """Per-trial ``ToolExecuting`` backed by a :class:`RuntimeBackend`.

    Binds ``trial_id`` and executor identity (``"agent"`` / ``"user"``)
    to a runtime backend so :class:`TrialRunner` can call
    ``.execute(tool_name, arguments)`` without threading trial identity
    through every call.

    All other per-trial RPCs are on :class:`RuntimeBackend` — see ADR-0013.
    """

    def __init__(self, runtime: RuntimeBackend, trial_id: str, executor: str = "agent"):
        """
        Initialize adapter.

        Args:
            runtime: Runtime backend the tool call routes through.
            trial_id: Trial identifier bound to every ``execute()`` call.
            executor: Environment identity for the runner-side routing
                (``"agent"`` or ``"user"``).
        """
        self.runtime = runtime
        self.trial_id = trial_id
        self.executor = executor
        logger.info(f"DockerRunnerAdapter initialized for trial {trial_id} (executor={executor})")

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        call_id: str,
        **kwargs,
    ) -> ToolResult:
        """Execute a tool via the runtime backend under the bound
        ``trial_id`` / ``executor``.

        Matches the :class:`~tolokaforge.tools.registry.ToolExecuting.execute`
        contract :class:`~tolokaforge.core.runner.TrialRunner` calls.

        ``call_id`` is named explicitly rather than left to ``**kwargs``: this
        method folds ``kwargs`` into the tool's own arguments, so an id arriving
        that way would be sent to the tool as a parameter.
        """
        if arguments is None:
            arguments = {}
        arguments.update(kwargs)

        return self.runtime.execute_tool(
            trial_id=self.trial_id,
            tool_name=tool_name,
            arguments=arguments,
            executor=self.executor,
            call_id=call_id,
        )
