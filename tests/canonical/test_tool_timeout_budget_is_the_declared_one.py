"""The budget bounding a tool call is the one that tool declares.

Two cases over one registered trial holding one tool that declares a 0.2 s
budget and sleeps far past it. The first drives the production host path
(:class:`DockerRunnerAdapter` over the real ``GrpcRunnerClient`` mappings into
the real servicer); the second hands the same servicer a request naming no
budget. Both assert elapsed wall time and the status the trial recorded, never
a field value — a declared number nothing enforces is exactly the defect these
lock against.

Together they localise, and today they disagree: the runner's resolution is
correct and unreachable, because the host names a budget of its own on every
call and the runner only resolves when the request names none. #691.

Timing assertions are one-sided and bounded *above* by a wide multiple of the
budget. CI contention can stretch a 0.2 s sleep; it cannot make a 1.5 s sleep
finish early.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from tests.utils.runner_requests import (
    execute_request,
    register_request,
    simple_task_description,
    trial_spec_json,
)
from tests.utils.servicer_runtime import ServicerBackend
from tolokaforge.core.docker_adapter import DockerRunnerAdapter
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.models import ToolSchema
from tolokaforge.runner.tool_factory import ToolWrapper
from tolokaforge.tools.registry import ToolExecutionStatus

pytestmark = pytest.mark.canonical

DECLARED_BUDGET_S = 0.2
"""The budget the tool under test declares on its own ``ToolSchema``."""

SLEEP_S = 1.5
"""How long that tool's single call sleeps — far past what it declared."""

TRIAL_DEFAULT_BUDGET_S = 25.0
"""The trial's fallback budget.

Distinct from both the declared budget and the 30.0 the host used to send, so
a call bounded at :data:`DECLARED_BUDGET_S` can only have been bounded by the
tool's own declaration.
"""


class _SlowTool(ToolWrapper):
    """Declares :data:`DECLARED_BUDGET_S` and outlives it on every call."""

    def __init__(self) -> None:
        super().__init__(
            ToolSchema(
                name="slow",
                description="sleeps past its declared budget",
                parameters={"type": "object", "properties": {}},
                timeout_s=DECLARED_BUDGET_S,
            )
        )

    async def execute(self, arguments: dict[str, Any]) -> str:
        await asyncio.sleep(SLEEP_S)
        return "the sleeper returned"


@pytest.fixture
def slow_trial(runner_service, mock_grpc_context, request) -> str:
    """A registered trial whose only tool declares a budget it outlives."""
    trial_id = f"{request.node.name}:0"
    registered = runner_service.RegisterTrial(
        register_request(
            trial_spec_json(simple_task_description(), trial_id=trial_id),
            trial_id=trial_id,
            default_tool_timeout_s=TRIAL_DEFAULT_BUDGET_S,
        ),
        mock_grpc_context,
    )
    assert registered.success is True, registered.error
    runner_service.trials[trial_id].agent_tools["slow"] = _SlowTool()
    return trial_id


def test_the_budget_a_tool_declares_bounds_the_call_the_host_makes(
    runner_service, mock_grpc_context, slow_trial
) -> None:
    adapter = DockerRunnerAdapter(
        runtime=ServicerBackend(runner_service, mock_grpc_context),
        trial_id=slow_trial,
        executor="agent",
    )

    started = time.monotonic()
    result = adapter.execute("slow", {}, call_id="toolu_host")
    elapsed = time.monotonic() - started

    recorded = runner_service.trials[slow_trial].tool_call_history[-1]
    assert result.status is ToolExecutionStatus.SUCCESS, (
        "DEFECT #691: the host sends its own hardcoded budget on every call, so the "
        f"tool's declared {DECLARED_BUDGET_S}s never bounds anything and a call that "
        f"outlives it is recorded {result.status}"
    )
    assert recorded.status is ToolExecutionStatus.SUCCESS, (
        f"DEFECT #691: the trial recorded {recorded.status} for a call that outlived "
        "its declared budget"
    )
    assert elapsed >= SLEEP_S, (
        f"DEFECT #691: the call took {elapsed:.2f}s — it ran to completion rather "
        f"than being bounded by the {DECLARED_BUDGET_S}s the tool declares"
    )


def test_the_runner_resolves_the_declared_budget_when_the_request_names_none(
    runner_service, mock_grpc_context, slow_trial
) -> None:
    started = time.monotonic()
    response = runner_service.ExecuteTool(
        execute_request(slow_trial, "slow", call_id="toolu_sentinel", timeout_seconds=0.0),
        mock_grpc_context,
    )
    elapsed = time.monotonic() - started

    assert response.status == pb2.EXECUTION_STATUS_TIMEOUT, (
        "a request naming no budget must fall to the tool's own declaration; the "
        "runner did not time the call out"
    )
    assert elapsed < SLEEP_S, (
        f"the call took {elapsed:.2f}s — the runner resolved something other than "
        f"the {DECLARED_BUDGET_S}s the tool declares (the trial's fallback is "
        f"{TRIAL_DEFAULT_BUDGET_S}s)"
    )
