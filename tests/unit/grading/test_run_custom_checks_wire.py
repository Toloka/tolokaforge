"""Behaviour lock for :func:`run_custom_checks` tool-call handling (issue #706).

A grade-time wire ``tool_calls`` entry is ``{"id": …, "function": {"name": …,
"arguments": "<json>"}}``. The helper used to read ``tc.get("name", "")`` /
``tc.get("arguments", {})`` off that shape, so every call arrived empty and a
tool-call-sequence check judged an empty view with no error anywhere. The fix
is dual-shape: an entry carrying ``"function"`` decodes as wire (real name,
parsed arguments, malformed entries raise naming the message), while the flat
``{"name", "arguments", "result"}`` shape keeps its long-standing behaviour so
existing callers of this public API are untouched.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tolokaforge.core.grading.check_runner import run_custom_checks
from tolokaforge.core.grading.checks_interface import CheckStatus
from tolokaforge.core.grading.transcript_wire import encode_transcript_wire
from tolokaforge.core.models import Message, MessageRole, ToolCall, Trajectory

pytestmark = pytest.mark.unit

_CHECKS = """
from tolokaforge.core.grading.checks_interface import (
    CheckContext,
    CheckFailed,
    CheckPassed,
    check,
    init,
)

_seen = {}


@init(interface_version="1.0")
def setup(ctx: CheckContext):
    _seen["calls"] = [
        (tc.name, tc.arguments)
        for m in ctx.transcript.messages
        for tc in m.tool_calls
    ]


@check
def tool_calls_carry_names_and_arguments():
    if _seen["calls"] == [("update_counter", {"delta": 7})]:
        return CheckPassed("saw the call with its name and arguments")
    return CheckFailed(f"saw {_seen['calls']!r}")
"""


@pytest.fixture
def checks_file(tmp_path: Path) -> Path:
    path = tmp_path / "checks.py"
    path.write_text(_CHECKS)
    return path


def _wire_payload() -> list[dict[str, Any]]:
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    trajectory = Trajectory(
        task_id="wire_lock",
        trial_index=0,
        start_ts=ts,
        end_ts=ts,
        messages=[
            Message(
                role=MessageRole.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id="toolu_1", name="update_counter", arguments={"delta": 7})],
            ),
            Message(role=MessageRole.TOOL, content="ok", tool_call_id="toolu_1"),
        ],
    )
    return json.loads(encode_transcript_wire(trajectory, "agent policy"))


def _run(checks_file: Path, tmp_path: Path, transcript_messages: list[dict[str, Any]]):
    return run_custom_checks(
        checks_file=checks_file,
        task_dir=tmp_path,
        initial_state={},
        final_state={},
        transcript_messages=transcript_messages,
        task_id="wire_lock",
    )


def test_wire_tool_calls_reach_checks_with_names_and_arguments(
    checks_file: Path, tmp_path: Path
) -> None:
    result = _run(checks_file, tmp_path, _wire_payload())

    assert result.error is None
    [check_result] = result.results
    assert check_result.status == CheckStatus.PASSED, check_result.message


def test_flat_tool_calls_keep_their_long_standing_behaviour(
    checks_file: Path, tmp_path: Path
) -> None:
    flat = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"name": "update_counter", "arguments": {"delta": 7}}],
        }
    ]

    result = _run(checks_file, tmp_path, flat)

    assert result.error is None
    [check_result] = result.results
    assert check_result.status == CheckStatus.PASSED, check_result.message


def test_a_malformed_wire_entry_raises_naming_the_message(
    checks_file: Path, tmp_path: Path
) -> None:
    malformed = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "toolu_1", "function": {"name": "update_counter"}}],
        }
    ]

    with pytest.raises(ValueError, match="wire message 0"):
        _run(checks_file, tmp_path, malformed)


def test_a_wire_entry_without_id_raises_naming_the_message(
    checks_file: Path, tmp_path: Path
) -> None:
    without_id = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "update_counter", "arguments": json.dumps({"delta": 7})}}
            ],
        }
    ]

    with pytest.raises(ValueError, match="wire message 0"):
        _run(checks_file, tmp_path, without_id)
