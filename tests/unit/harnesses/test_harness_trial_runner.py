"""Harness trajectory import, retry classification, and command safety."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.models import AgentHarnessConfig, TerminationReason, TrialStatus
from tolokaforge.harnesses.trial_runner import HarnessTrialRunner

pytestmark = pytest.mark.unit


@pytest.fixture
def runner(tmp_path):
    return HarnessTrialRunner(
        AgentHarnessConfig(type="claude-code", version="2.1.203"),
        network=MagicMock(),
        workspace_root=tmp_path,
        episode_timeout_s=30,
    )


@pytest.fixture
def codex_runner(tmp_path):
    return HarnessTrialRunner(
        AgentHarnessConfig(
            type="codex",
            version="0.118.0",
            flags={"model": "gpt-5-codex", "sandbox_mode": "workspace-write"},
        ),
        network=MagicMock(),
        workspace_root=tmp_path,
        episode_timeout_s=30,
    )


@pytest.fixture
def acp_runner(tmp_path):
    return HarnessTrialRunner(
        AgentHarnessConfig(
            type="acp",
            version="1.0.0",
            flags={"command": ["python3", "/work/mock_agent.py"]},
        ),
        network=MagicMock(),
        workspace_root=tmp_path,
        episode_timeout_s=30,
    )


def test_claude_command_keeps_mcp_bearer_out_of_process_arguments(runner) -> None:
    command = runner._claude_command("Add a note", "Use the supplied tools.")

    assert "/tmp/tolokaforge-mcp.json" in command
    assert "--strict-mcp-config" in command
    assert command[command.index("--tools") + 1] == ""
    assert not any("Bearer" in argument for argument in command)


def test_claude_stream_imports_messages_tools_metrics_and_atif(runner) -> None:
    stdout = "\n".join(
        [
            '{"type":"assistant","session_id":"session-1","message":{"content":'
            '[{"type":"text","text":"I will add it."},{"type":"tool_use",'
            '"id":"toolu_1","name":"mcp__tolokaforge__add_note",'
            '"input":{"text":"first"}}]}}',
            '{"type":"user","message":{"content":[{"type":"tool_result",'
            '"tool_use_id":"toolu_1","content":"created"}]}}',
            '{"type":"assistant","message":{"content":[{"type":"text","text":"Done."}]}}',
            '{"type":"result","is_error":false,"num_turns":2,"total_cost_usd":0.04,'
            '"usage":{"input_tokens":120,"output_tokens":30}}',
        ]
    )

    trajectory, atif = runner._convert_claude_stream(
        task_id="notes",
        trial_index=0,
        instruction="Add the first note",
        stdout=stdout,
        stderr="",
        exit_code=0,
        started=datetime.now(timezone.utc),
        timed_out=False,
    )

    assert trajectory.status == TrialStatus.COMPLETED
    assert trajectory.termination_reason == TerminationReason.AGENT_DONE
    assert trajectory.metrics.cost_usd == 0.04
    assert trajectory.metrics.usage.prompt_tokens == 120
    assert trajectory.metrics.usage.completion_tokens == 30
    assert trajectory.messages[1].tool_calls[0].name == "mcp__tolokaforge__add_note"
    assert atif["schema_version"] == "ATIF-v1.7"
    assert atif["session_id"] == "session-1"
    assert atif["steps"][1]["observation"]["results"][0]["content"] == "created"


def test_codex_command_is_headless_and_keeps_bearer_out_of_argv(codex_runner) -> None:
    command = codex_runner._codex_command("Add a note", "Use MCP.")

    assert command[:3] == ["codex", "exec", "--json"]
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert "/tmp/codex-home/config.toml" not in command
    assert not any("Bearer" in argument for argument in command)


def test_codex_stream_imports_mcp_call_usage_and_atif(codex_runner) -> None:
    stdout = "\n".join(
        [
            '{"type":"thread.started","thread_id":"thread-1"}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"id":"item-1","type":"mcp_tool_call",'
            '"tool":"add_note","arguments":{"text":"first"},"result":"created"}}',
            '{"type":"item.completed","item":{"id":"item-2","type":"agent_message",'
            '"text":"Done."}}',
            '{"type":"turn.completed","usage":{"input_tokens":100,'
            '"cached_input_tokens":50,"output_tokens":20}}',
        ]
    )

    trajectory, atif = codex_runner._convert_codex_stream(
        task_id="notes",
        trial_index=0,
        instruction="Add the first note",
        stdout=stdout,
        stderr="",
        exit_code=0,
        started=datetime.now(timezone.utc),
        timed_out=False,
    )

    assert trajectory.status == TrialStatus.COMPLETED
    assert trajectory.metrics.usage.prompt_tokens == 100
    assert trajectory.metrics.usage.cache_read_input_tokens == 50
    assert trajectory.messages[1].tool_calls[0].name == "add_note"
    assert atif["session_id"] == "thread-1"
    assert atif["steps"][1]["observation"]["results"][0]["content"] == "created"


def test_acp_stream_imports_agent_chunks_and_session(acp_runner) -> None:
    stdout = "\n".join(
        [
            '{"event_type":"new_session","payload":{"sessionId":"acp-session"}}',
            '{"event_type":"session_update","payload":{"session_id":"acp-session",'
            '"update":{"sessionUpdate":"agent_message_chunk",'
            '"content":{"type":"text","text":"Done."}}}}',
        ]
    )

    trajectory, atif = acp_runner._convert_acp_stream(
        task_id="notes",
        trial_index=0,
        instruction="Add the first note",
        stdout=stdout,
        stderr="",
        exit_code=0,
        started=datetime.now(timezone.utc),
        timed_out=False,
    )

    assert trajectory.status == TrialStatus.COMPLETED
    assert trajectory.messages[-1].content == "Done."
    assert atif["session_id"] == "acp-session"


@pytest.mark.parametrize(
    ("stderr", "status", "reason"),
    [
        ("credit balance exhausted", TrialStatus.FAILED, TerminationReason.USAGE_EXHAUSTED),
        ("safety refusal", TrialStatus.FAILED, TerminationReason.SAFETY_REFUSAL),
        ("rate limit", TrialStatus.ERROR, TerminationReason.RATE_LIMIT),
        ("connection reset", TrialStatus.ERROR, TerminationReason.API_TIMEOUT),
    ],
)
def test_failure_classification(runner, stderr, status, reason) -> None:
    trajectory, _ = runner._convert_claude_stream(
        task_id="notes",
        trial_index=0,
        instruction="Add the first note",
        stdout="",
        stderr=stderr,
        exit_code=1,
        started=datetime.now(timezone.utc),
        timed_out=False,
    )

    assert trajectory.status == status
    assert trajectory.termination_reason == reason


def test_timeout_classification(runner) -> None:
    trajectory, _ = runner._convert_claude_stream(
        task_id="notes",
        trial_index=0,
        instruction="Add the first note",
        stdout="",
        stderr="timeout",
        exit_code=124,
        started=datetime.now(timezone.utc),
        timed_out=True,
    )

    assert trajectory.status == TrialStatus.TIMEOUT
    assert trajectory.termination_reason == TerminationReason.TIMEOUT
