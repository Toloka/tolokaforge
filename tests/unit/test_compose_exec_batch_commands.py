"""``bash_batch`` runs an array of commands against one deadline.

Locks the four properties the batching tool rests on at
:class:`~tolokaforge.runner.tool_factory.DockerComposeExecToolWrapper`: a
``commands`` array runs in order, each command is its own ``docker exec``, every
command's output is labelled with the command that produced it, and a spent
budget reports the remainder as unrun rather than dropping it.

The single-``command`` path is covered here too, because the array is additive:
a caller that sends ``command`` must reach the same argv it always did.
"""

from __future__ import annotations

import asyncio

import pytest

from tolokaforge.runner.models import ToolSchema, ToolSource
from tolokaforge.runner.tool_factory import DockerComposeExecToolWrapper

pytestmark = pytest.mark.unit


def _wrapper(timeout_s: float = 30.0) -> DockerComposeExecToolWrapper:
    schema = ToolSchema(
        name="bash_batch",
        description="stub",
        parameters={"type": "object", "properties": {}},
        category="compute",
        timeout_s=timeout_s,
        source=ToolSource(
            toolset="terminal_bench",
            module_path="",
            class_name="bash_batch",
            invocation_style="docker_compose_exec",
            extra={"service": "agent", "compose_project_prefix": "tbench"},
        ),
    )
    wrapper = DockerComposeExecToolWrapper(schema, service="agent", compose_project_prefix="tbench")
    wrapper._container = "tbench_task_0_agent"
    return wrapper


def test_commands_array_runs_in_order_and_labels_each_output(monkeypatch) -> None:
    seen: list[list[str]] = []

    def fake_run(argv: list[str], timeout_s: float) -> str:
        seen.append(argv)
        return f"out::{argv[-1]}"

    monkeypatch.setattr(
        "tolokaforge.runner.tool_factory._run_argv_preserving_partial_output", fake_run
    )
    wrapper = _wrapper()

    result = asyncio.run(wrapper.execute({"commands": ["echo one", "echo two", "echo three"]}))

    # One docker exec per command, in the order the model asked for.
    assert [argv[-1] for argv in seen] == ["echo one", "echo two", "echo three"]
    assert all(argv[0] == "docker" for argv in seen)
    # Each output is attributed to the command that produced it.
    assert result == (
        "$ echo one\nout::echo one\n\n$ echo two\nout::echo two\n\n$ echo three\nout::echo three"
    )


def test_spent_budget_reports_the_remainder_as_unrun(monkeypatch) -> None:
    calls: list[str] = []

    def fake_run(argv: list[str], timeout_s: float) -> str:
        calls.append(argv[-1])
        return "ok"

    # First command consumes the whole budget; the clock is past the deadline by
    # the time the second is considered. The clock is driven through a list the
    # batch walks itself — patching ``time.monotonic`` globally would also move
    # the event loop's clock, so the sync body is exercised directly.
    ticks = [0.0, 0.0, 100.0]
    monkeypatch.setattr(
        "tolokaforge.runner.tool_factory._run_argv_preserving_partial_output", fake_run
    )
    monkeypatch.setattr(
        "tolokaforge.runner.tool_factory.time.monotonic",
        lambda: ticks.pop(0) if ticks else 100.0,
    )
    wrapper = _wrapper(timeout_s=10.0)

    result = wrapper._exec_batch_in_env(["slow", "never", "also-never"], 10.0)

    assert calls == ["slow"]
    assert "[not run" in result
    assert "2 command(s) remain" in result
    # The commands that never ran are still named, so the model can retry them.
    assert "$ never" in result and "$ also-never" in result


def test_single_command_path_is_unchanged(monkeypatch) -> None:
    seen: list[list[str]] = []

    def fake_run(argv: list[str], timeout_s: float) -> str:
        seen.append(argv)
        return "plain output"

    monkeypatch.setattr(
        "tolokaforge.runner.tool_factory._run_argv_preserving_partial_output", fake_run
    )
    wrapper = _wrapper()

    result = asyncio.run(wrapper.execute({"command": "echo hi"}))

    assert len(seen) == 1
    assert seen[0][-1] == "echo hi"
    # No labelling on the single-command path: the output is the output.
    assert result == "plain output"


def test_empty_commands_array_runs_nothing(monkeypatch) -> None:
    def fail(argv: list[str], timeout_s: float) -> str:  # pragma: no cover - must not run
        raise AssertionError("no docker exec should be issued for an empty array")

    monkeypatch.setattr("tolokaforge.runner.tool_factory._run_argv_preserving_partial_output", fail)
    wrapper = _wrapper()

    assert asyncio.run(wrapper.execute({"commands": []})) == ""
