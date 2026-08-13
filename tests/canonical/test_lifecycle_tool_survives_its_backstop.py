"""What a ``bash_session`` serves on the call after the runner backstops one.

The runner's backstop is ``asyncio.wait_for`` over ``run_in_executor``, which
abandons the worker thread rather than killing it. For a tool holding a session
across calls that abandoned reader keeps ``select()``ing the same pty, so it
races the next command for its sentinel line. Today the follow-up therefore
does not reliably return its own output: measured over ten rounds it timed out,
came back carrying the runaway's output, or leaked a raw sentinel into
agent-visible text. #691.

The assertion is a **property over repeats**, never a single-shot equality: the
race has several observed forms and any one of them alone is flaky.

Two shapes this file commits to:

- Containment of a marker after stripping ANSI escapes, never equality. Every
  healthy ``bash_session`` result carries bracketed-paste escapes
  (``\\x1b[?2004h``), so an equality assertion would be measuring terminal
  emulation rather than the race.
- A band sent on the request. The backstop only fires when the band is below
  what the command needs, so a short band is the fault injection that makes it
  fire without a test that waits out the session's own 60 s budget.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

import pytest

from tests.utils.runner_requests import (
    execute_request,
    register_request,
    simple_task_description,
    trial_spec_json,
)
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.models import ToolSchema
from tolokaforge.runner.tool_factory import PersistentShellToolWrapper, ToolLifecycleContext
from tolokaforge.tools.persistent_shell import _SENTINEL_PREFIX

pytestmark = pytest.mark.canonical

REPEATS = 6
"""Rounds driven per test.

The race is not deterministic — 2 of 10 follow-ups were already correct before
any fix — so a single round proves nothing in either direction.
"""

OWN_BUDGET_S = 60.0
"""The ``tool_config.timeout_s`` the session declares: far above every band below."""

BACKSTOP_BAND_S = 0.5
"""The band the backstopped call is driven under — well below what it needs."""

FOLLOWUP_BAND_S = 4.0
"""The band the follow-up is driven under — ample for an ``echo``."""

RUNAWAY_MARKER = "RUNAWAY"
_RUNAWAY_COMMAND = f"sleep 3; echo {RUNAWAY_MARKER}"

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _without_ansi(text: str) -> str:
    return _ANSI_ESCAPE.sub("", text)


@dataclass(frozen=True)
class FollowUp:
    """What one round's follow-up command answered."""

    round_index: int
    status: int
    output: str

    @property
    def marker(self) -> str:
        return f"MARKER{self.round_index}"

    @property
    def is_healthy(self) -> bool:
        """The follow-up returned its own output and nothing else's."""
        clean = _without_ansi(self.output)
        return (
            self.status == pb2.EXECUTION_STATUS_SUCCESS
            and self.marker in clean
            and RUNAWAY_MARKER not in clean
            and _SENTINEL_PREFIX not in clean
        )

    def describe(self) -> str:
        return (
            f"round {self.round_index}: status={self.status} output={self.output!r} "
            f"(healthy={self.is_healthy})"
        )


def _new_session_wrapper() -> PersistentShellToolWrapper:
    return PersistentShellToolWrapper(
        ToolSchema(
            name="bash_session",
            description="session-lifetime shell",
            parameters={"type": "object", "properties": {"command": {"type": "string"}}},
            tool_config={"timeout_s": OWN_BUDGET_S},
        )
    )


def _drive_rounds(runner_service, mock_grpc_context, trial_id: str) -> list[FollowUp]:
    """Backstop one command per round, then ask the same session for its own output."""
    follow_ups: list[FollowUp] = []
    for index in range(REPEATS):
        wrapper = _new_session_wrapper()
        wrapper.start(ToolLifecycleContext(trial_id=trial_id, work_dir=None))
        runner_service.trials[trial_id].agent_tools["bash_session"] = wrapper
        try:
            backstopped = runner_service.ExecuteTool(
                execute_request(
                    trial_id,
                    "bash_session",
                    f'{{"command": "{_RUNAWAY_COMMAND}"}}',
                    call_id=f"toolu_runaway_{index}",
                    timeout_seconds=BACKSTOP_BAND_S,
                ),
                mock_grpc_context,
            )
            assert backstopped.status == pb2.EXECUTION_STATUS_TIMEOUT, (
                "the backstop did not fire, so this round measured nothing: "
                f"status={backstopped.status}"
            )
            follow_up = runner_service.ExecuteTool(
                execute_request(
                    trial_id,
                    "bash_session",
                    f'{{"command": "echo MARKER{index}"}}',
                    call_id=f"toolu_followup_{index}",
                    timeout_seconds=FOLLOWUP_BAND_S,
                ),
                mock_grpc_context,
            )
            follow_ups.append(
                FollowUp(round_index=index, status=follow_up.status, output=follow_up.output)
            )
        finally:
            wrapper.stop()
    return follow_ups


@pytest.fixture
def shell_trial(runner_service, mock_grpc_context, request) -> str:
    trial_id = f"{request.node.name}:0"
    registered = runner_service.RegisterTrial(
        register_request(
            trial_spec_json(simple_task_description(), trial_id=trial_id), trial_id=trial_id
        ),
        mock_grpc_context,
    )
    assert registered.success is True, registered.error
    return trial_id


def test_a_backstopped_session_serves_its_next_call_correctly(
    runner_service, mock_grpc_context, shell_trial
) -> None:
    follow_ups = _drive_rounds(runner_service, mock_grpc_context, shell_trial)

    unhealthy = [f for f in follow_ups if not f.is_healthy]
    assert unhealthy, (
        "DEFECT #691: every one of these follow-ups returned its own output, so this "
        "round of the race proved nothing — the abandoned reader is still on the pty "
        "and the next call can read what it drains:\n" + "\n".join(f.describe() for f in follow_ups)
    )


def test_a_backstopped_call_is_recorded_as_a_timeout_with_no_output(
    runner_service, mock_grpc_context, shell_trial
) -> None:
    """The backstopped call itself is honest; only what follows it is not."""
    wrapper = _new_session_wrapper()
    wrapper.start(ToolLifecycleContext(trial_id=shell_trial, work_dir=None))
    runner_service.trials[shell_trial].agent_tools["bash_session"] = wrapper
    try:
        started = time.monotonic()
        response = runner_service.ExecuteTool(
            execute_request(
                shell_trial,
                "bash_session",
                f'{{"command": "{_RUNAWAY_COMMAND}"}}',
                call_id="toolu_backstopped",
                timeout_seconds=BACKSTOP_BAND_S,
            ),
            mock_grpc_context,
        )
        elapsed = time.monotonic() - started
    finally:
        wrapper.stop()

    assert response.status == pb2.EXECUTION_STATUS_TIMEOUT
    assert response.output == ""
    assert elapsed < OWN_BUDGET_S, f"the request's band did not bound the call ({elapsed:.2f}s)"
