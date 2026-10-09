"""``bash_batch`` runs an array of commands against one deadline.

Locks the properties the batching tool rests on at
:class:`~tolokaforge.runner.tool_factory.DockerComposeExecToolWrapper`: a
``commands`` array runs in order, each command is its own ``docker exec`` with
the same per-command budget the one-shot tool applies, every command's output is
labelled with the command that produced it, a spent budget reports the remainder
as unrun rather than dropping it, and a malformed argument runs nothing and is a
failed call, not a successful one carrying an error string.

The single-``command`` path is covered here too, because the array is additive:
a caller that sends ``command`` must reach the same argv it always did.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from tolokaforge.runner.models import ToolSchema, ToolSource
from tolokaforge.runner.tool_factory import DockerComposeExecToolWrapper, ToolExecutionError

pytestmark = pytest.mark.unit


def _wrapper(timeout_s: float = 30.0, max_items: int | None = None) -> DockerComposeExecToolWrapper:
    commands_schema: dict[str, object] = {"type": "array", "items": {"type": "string"}}
    if max_items is not None:
        commands_schema["maxItems"] = max_items
    schema = ToolSchema(
        name="bash_batch",
        description="stub",
        parameters={"type": "object", "properties": {"commands": commands_schema}},
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

    def fake_run(argv: list[str], timeout_s: float, **_: object) -> str:
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

    def slow_first(argv: list[str], timeout_s: float, **_: object) -> str:
        calls.append(argv[-1])
        time.sleep(0.15)
        return "ok"

    monkeypatch.setattr(
        "tolokaforge.runner.tool_factory._run_argv_preserving_partial_output", slow_first
    )
    # A real clock against a budget the first command outlives, so the deadline
    # is exercised rather than simulated — patching ``time.monotonic`` here
    # would rebind the stdlib clock for everything else in the process.
    wrapper = _wrapper(timeout_s=0.1)

    result = wrapper._exec_batch_in_env(["slow", "never", "also-never"], 0.1)

    assert calls == ["slow"]
    assert "[not run" in result
    assert "2 command(s) remain" in result
    # The commands that never ran are still named, so the model can retry them.
    assert "$ never" in result and "$ also-never" in result


def test_each_command_gets_the_per_command_ceiling_not_a_share(monkeypatch) -> None:
    budgets: list[float] = []

    def record_budget(argv: list[str], timeout_s: float, **_: object) -> str:
        budgets.append(timeout_s)
        return "ok"

    monkeypatch.setattr(
        "tolokaforge.runner.tool_factory._run_argv_preserving_partial_output", record_budget
    )
    # Declared band is the per-command ceiling times maxItems, so a batch of
    # three must not squeeze three commands into one command's worth of time:
    # the point of the tool is to replace N single-command calls, not to
    # handicap them.
    wrapper = _wrapper(timeout_s=1200.0, max_items=10)

    asyncio.run(wrapper.execute({"commands": ["a", "b", "c"]}))

    assert budgets == [120.0, 120.0, 120.0]


def test_a_non_array_commands_argument_runs_nothing_and_says_so(monkeypatch) -> None:
    def fail(argv: list[str], timeout_s: float, **_: object) -> str:  # pragma: no cover
        raise AssertionError("a malformed commands argument must not reach docker exec")

    monkeypatch.setattr("tolokaforge.runner.tool_factory._run_argv_preserving_partial_output", fail)
    wrapper = _wrapper()

    # A string would otherwise be iterated into one exec per character. The
    # wrapper raises rather than returning a string: a returned string is a
    # successful call to the recorder (``tool_status: success``, nothing in
    # ``parser_errors``), so the model's mistake would cost a turn and leave
    # no mark in the metrics.
    for bad in ("ls -la", 7, [{"cmd": "ls"}], [None]):
        with pytest.raises(ToolExecutionError) as excinfo:
            asyncio.run(wrapper.execute({"commands": bad}))
        assert excinfo.value.tool_name == "bash_batch"
        assert excinfo.value.message.startswith("`commands` must be an array of strings")
        assert "Nothing was run" in excinfo.value.message
        assert type(bad).__name__ in excinfo.value.message


def test_a_note_argument_is_recorded_input_not_a_command(monkeypatch) -> None:
    seen: list[str] = []

    def fake_run(argv: list[str], timeout_s: float, **_: object) -> str:
        seen.append(argv[-1])
        return "ok"

    monkeypatch.setattr(
        "tolokaforge.runner.tool_factory._run_argv_preserving_partial_output", fake_run
    )
    wrapper = _wrapper()

    # The schema requires ``note`` beside ``commands``; it rides the call for
    # the record and must neither run nor change what does.
    result = asyncio.run(
        wrapper.execute(
            {"note": "tests passed; running the linter next", "commands": ["ruff check ."]}
        )
    )

    assert seen == ["ruff check ."]
    assert result == "$ ruff check .\nok"


def test_single_command_path_is_unchanged(monkeypatch) -> None:
    seen: list[list[str]] = []

    def fake_run(argv: list[str], timeout_s: float, **_: object) -> str:
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
    def fail(argv: list[str], timeout_s: float, **_: object) -> str:  # pragma: no cover
        raise AssertionError("no docker exec should be issued for an empty array")

    monkeypatch.setattr("tolokaforge.runner.tool_factory._run_argv_preserving_partial_output", fail)
    wrapper = _wrapper()

    assert asyncio.run(wrapper.execute({"commands": []})) == ""
