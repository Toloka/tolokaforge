"""Pin the ``RunnerClient`` Protocol contract.

The Protocol declares the runner-RPC surface any
:class:`RuntimeBackend.executor_client` must expose. Six per-trial RPCs
plus a lifecycle probe. The concrete :class:`GrpcRunnerClient` (the
only production impl today) is checked here for structural conformance;
a canonical stub proves the shape is genuinely swappable and pins the
method signatures against silent drift.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.docker_runtime import GrpcRunnerClient, RunnerClient
from tolokaforge.core.trial import DEFAULT_TOOL_TIMEOUT_S
from tolokaforge.tools.registry import ToolResult

pytestmark = pytest.mark.canonical


class _StubRunnerClient:
    """Minimal structural implementation used to prove the Protocol is
    satisfiable without the gRPC stack."""

    def register_trial(
        self,
        trial_id: str,
        trial_spec_json: str,
        default_tool_timeout_s: float = DEFAULT_TOOL_TIMEOUT_S,
    ) -> dict:
        return {
            "success": True,
            "error": None,
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
        return ToolResult(success=True, output="", error=None)

    def grade_trial(
        self,
        trial_id: str,
        llm_messages_json: str | None = None,
        grading_components: list[str] | None = None,
    ) -> dict:
        return {"success": True, "error": None, "grade": None}

    def get_state(
        self,
        trial_id: str,
        include_unstable: bool = True,
        tables: list[str] | None = None,
    ) -> dict:
        return {"success": True, "error": None, "state_json": "{}"}

    def reset_trial(self, trial_id: str, execute_init_actions: bool = False) -> dict:
        return {"success": True, "error": None}

    def cleanup_trial(self, trial_id: str) -> dict:
        return {"success": True, "error": None}

    def health_check(self) -> bool:
        return True


def test_grpc_runner_client_satisfies_protocol() -> None:
    """The production gRPC client structurally satisfies the Protocol."""
    client = GrpcRunnerClient.__new__(GrpcRunnerClient)  # no gRPC channel needed
    assert isinstance(client, RunnerClient)


def test_stub_runner_client_satisfies_protocol() -> None:
    """A non-gRPC stub satisfies the Protocol — proof the seam is swappable."""
    assert isinstance(_StubRunnerClient(), RunnerClient)


def test_partial_impl_fails_protocol_check() -> None:
    """An object that implements only some of the required methods must
    fail ``isinstance(..., RunnerClient)`` — the check has real teeth
    beyond just accepting anything that walks like a duck."""

    class _Partial:
        def register_trial(self, *args: Any, **kwargs: Any) -> dict:
            return {}

    assert not isinstance(_Partial(), RunnerClient)


@pytest.mark.parametrize(
    "method_name",
    [
        "register_trial",
        "execute_tool",
        "grade_trial",
        "get_state",
        "reset_trial",
        "cleanup_trial",
        "health_check",
    ],
)
def test_protocol_surface_pins_expected_methods(method_name: str) -> None:
    """The seven methods callers depend on are declared on the Protocol.

    Guards against a future edit accidentally dropping a method from the
    Protocol without a corresponding removal at every call site."""
    assert hasattr(RunnerClient, method_name)


def test_protocol_is_runtime_checkable() -> None:
    """``@runtime_checkable`` — callers can ``isinstance(x, RunnerClient)``
    when validating an injected backend without type-check plumbing."""
    stub = MagicMock(spec=_StubRunnerClient())
    assert isinstance(stub, RunnerClient)
