"""``ToolPolicy.timeout_s`` is the budget the tool applies to its own work.

The field names no band an executor enforces — ``ToolExecutor`` runs a tool to
completion, and the runner's backstop reads
``ToolWrapper.effective_timeout_s``. What it does mean is that a tool declaring
it reads it at its own I/O boundary, which is a claim about behaviour and so is
measured here rather than asserted about a field.

``http_request`` stands for that whole class of tool: it hands the declared
value to ``httpx``. The peer is a socket that accepts the connection and never
answers, so the wait is a read timeout with no network involved and no
dependence on how a blackhole address behaves in the test environment.

**Two budgets, not one.** Elapsed time is asserted at both, because a tool that
reported the declared number while enforcing something else would satisfy
either case alone. The small case caps elapsed below the large budget and the
large case floors it at the large budget, so no fixed band — decoupled from the
declaration, wherever it sat — can satisfy both.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Iterator

import pytest

from tolokaforge.tools.builtin.http_request import HTTPRequestTool
from tolokaforge.tools.registry import ToolExecutor, ToolRegistry

pytestmark = pytest.mark.unit

SMALL_BUDGET_S = 0.3
LARGE_BUDGET_S = 1.5

CEILING_MULTIPLE = 5
"""Headroom on the one-sided upper bound.

The worst observed overshoot is 1.26x the declared budget (a cold first call),
so this leaves ~4x for CI contention while still holding the small case below
:data:`LARGE_BUDGET_S`."""


@pytest.fixture
def silent_peer() -> Iterator[str]:
    """A listening socket that never answers, as ``host:port``."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        host, port = listener.getsockname()
        yield f"{host}:{port}"
    finally:
        listener.close()


def _tool_declaring(budget_s: float, peer: str) -> HTTPRequestTool:
    tool = HTTPRequestTool(allowed_hosts=[peer])
    tool.policy.timeout_s = budget_s
    return tool


def _assert_bounded_by(budget_s: float, elapsed: float, error: str | None) -> None:
    assert f"timed out after {budget_s}s" in (error or ""), (
        f"the tool did not report its declared {budget_s}s budget as what bounded "
        f"the call: {error!r}"
    )
    assert elapsed >= budget_s, (
        f"the call returned in {elapsed:.2f}s, sooner than the {budget_s}s declared — "
        "something tighter than the declaration bounded it"
    )
    assert elapsed < budget_s * CEILING_MULTIPLE, (
        f"the call took {elapsed:.2f}s against a declared {budget_s}s budget, so the "
        "declaration is not the number being enforced"
    )


@pytest.mark.parametrize("budget_s", [SMALL_BUDGET_S, LARGE_BUDGET_S], ids=["small", "large"])
def test_a_declared_budget_bounds_the_tools_own_io(silent_peer: str, budget_s: float) -> None:
    tool = _tool_declaring(budget_s, silent_peer)

    started = time.monotonic()
    result = tool.execute(method="GET", url=f"http://{silent_peer}/never-answers")
    elapsed = time.monotonic() - started

    assert result.success is False
    _assert_bounded_by(budget_s, elapsed, result.error)


def test_the_in_process_executor_adds_no_band_of_its_own(silent_peer: str) -> None:
    """Same tool, same budget, run through ``ToolExecutor``: the elapsed time is
    the tool's, because the executor applies none.

    The distinction is what makes the field's meaning falsifiable — an executor
    band would show up here as a different number or a different failure text.
    """
    tool = _tool_declaring(SMALL_BUDGET_S, silent_peer)
    registry = ToolRegistry()
    registry.register(tool)

    started = time.monotonic()
    result = ToolExecutor(registry).execute(
        "http_request",
        {"method": "GET", "url": f"http://{silent_peer}/never-answers"},
        call_id="toolu_http",
    )
    elapsed = time.monotonic() - started

    assert result.success is False
    _assert_bounded_by(SMALL_BUDGET_S, elapsed, result.error)
