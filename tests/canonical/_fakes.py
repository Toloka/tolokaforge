"""Recording fakes shared by canonical tests."""

from __future__ import annotations

from typing import Any

from tolokaforge.tools.registry import ToolResult


class RecordingRunnerClient:
    """A ``RunnerClient`` that records every call and answers with canned success.

    ``tool_schemas`` is what ``register_trial`` advertises to the agent; ``grade``
    is the wire-shaped grade ``grade_trial`` returns.
    """

    def __init__(
        self,
        runner_address: str,
        max_message_mb: int,
        *,
        tool_schemas: list[dict[str, Any]] | None = None,
        tool_output: str = "ok",
    ) -> None:
        self.runner_address = runner_address
        self.max_message_mb = max_message_mb
        self.tool_schemas = tool_schemas or []
        self.tool_output = tool_output
        self.connect_calls: list[dict[str, float]] = []
        self.closed = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def connect(self, timeout: float = 30.0, retry_interval: float = 1.0) -> None:
        self.connect_calls.append({"timeout": timeout, "retry_interval": retry_interval})

    def close(self) -> None:
        self.closed = True

    def health_check(self) -> bool:
        return bool(self.connect_calls) and not self.closed

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def register_trial(
        self, trial_id: str, trial_spec_json: str, default_tool_timeout_s: float = 30.0
    ) -> dict[str, Any]:
        self.calls.append(("register_trial", {"trial_id": trial_id}))
        return {
            "success": True,
            "error": None,
            "tool_schemas": self.tool_schemas,
            "num_agent_tools": len(self.tool_schemas),
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
        self.calls.append(
            (
                "execute_tool",
                {
                    "trial_id": trial_id,
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "executor": executor,
                    "call_id": call_id,
                },
            )
        )
        return ToolResult(success=True, output=self.tool_output)

    def grade_trial(
        self,
        trial_id: str,
        llm_messages_json: str | None = None,
        grading_components: list[str] | None = None,
        termination_reason: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("grade_trial", {"trial_id": trial_id}))
        return {
            "success": True,
            "error": None,
            "grade": {
                "binary_pass": True,
                "score": 1.0,
                "components": {},
                "reasons": f"graded on {self.runner_address}",
            },
        }

    def get_state(
        self, trial_id: str, include_unstable: bool = True, tables: list[str] | None = None
    ) -> dict[str, Any]:
        self.calls.append(("get_state", {"trial_id": trial_id}))
        return {"success": True, "error": None, "state_json": "{}"}

    def reset_trial(self, trial_id: str, execute_init_actions: bool = False) -> dict[str, Any]:
        self.calls.append(("reset_trial", {"trial_id": trial_id}))
        return {"success": True, "error": None}

    def cleanup_trial(self, trial_id: str) -> dict[str, Any]:
        self.calls.append(("cleanup_trial", {"trial_id": trial_id}))
        return {"success": True, "error": None}


class RecordingClientFactory:
    """A ``runner_client_factory`` that remembers every client it built."""

    def __init__(self, **client_kwargs: Any) -> None:
        self._client_kwargs = client_kwargs
        self.clients: list[RecordingRunnerClient] = []

    def __call__(self, runner_address: str, max_message_mb: int) -> RecordingRunnerClient:
        client = RecordingRunnerClient(runner_address, max_message_mb, **self._client_kwargs)
        self.clients.append(client)
        return client
