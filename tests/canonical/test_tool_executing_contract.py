"""Pin the ``ToolExecuting`` Protocol contract.

Two implementations must satisfy it, and they share no base class:
:class:`ToolExecutor` runs the tool in-process against a
:class:`~tolokaforge.tools.registry.ToolRegistry`, and
:class:`DockerRunnerAdapter` forwards the call to the runner container under a
bound trial id and executor identity. Both are handed to
:class:`~tolokaforge.core.runner.TrialRunner` and
:class:`~tolokaforge.core.loop.ToolCallingLoop` through parameters annotated
with this Protocol.
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.docker_adapter import DockerRunnerAdapter
from tolokaforge.tools.registry import ToolExecuting, ToolExecutor, ToolRegistry, ToolResult

pytestmark = pytest.mark.canonical


class TestProtocolRuntimeCheck:
    """The Protocol is ``@runtime_checkable``; both implementations satisfy it
    via ``isinstance`` (not just by structural type-hint compatibility)."""

    def test_tool_executor_passes_isinstance(self) -> None:
        assert isinstance(ToolExecutor(ToolRegistry()), ToolExecuting)

    def test_docker_runner_adapter_passes_isinstance(self) -> None:
        adapter = DockerRunnerAdapter(runtime=MagicMock(), trial_id="t:0", executor="user")
        assert isinstance(adapter, ToolExecuting)

    def test_random_object_does_not_pass_isinstance(self) -> None:
        class _NotAnExecutor:
            pass

        assert not isinstance(_NotAnExecutor(), ToolExecuting)

    def test_object_with_matching_shape_passes_isinstance(self) -> None:
        class _DuckExecutor:
            def execute(
                self, tool_name: str, arguments: dict[str, Any], *, call_id: str
            ) -> ToolResult:  # pragma: no cover — never called
                return ToolResult(success=True, output="")

        assert isinstance(_DuckExecutor(), ToolExecuting)


class TestExecuteSignature:
    """``call_id`` is keyword-only on the Protocol and on every implementation:
    the adapter folds unrecognised keyword arguments into the tool's own
    arguments, so an id arriving positionally or by ``**kwargs`` would be sent
    to the tool as a parameter."""

    @pytest.mark.parametrize(
        "surface",
        [ToolExecuting.execute, ToolExecutor.execute, DockerRunnerAdapter.execute],
        ids=["protocol", "tool_executor", "docker_runner_adapter"],
    )
    def test_call_id_is_keyword_only(self, surface) -> None:
        call_id = inspect.signature(surface).parameters["call_id"]
        assert call_id.kind is inspect.Parameter.KEYWORD_ONLY
        assert call_id.default is inspect.Parameter.empty

    @pytest.mark.parametrize(
        "surface",
        [ToolExecuting.execute, ToolExecutor.execute, DockerRunnerAdapter.execute],
        ids=["protocol", "tool_executor", "docker_runner_adapter"],
    )
    def test_tool_name_and_arguments_lead_positionally(self, surface) -> None:
        positional = [
            name
            for name, param in inspect.signature(surface).parameters.items()
            if param.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        ]
        assert positional[:3] == ["self", "tool_name", "arguments"]
