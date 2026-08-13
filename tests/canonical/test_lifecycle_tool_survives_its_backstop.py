"""What a ``bash_session`` serves on the call after the runner backstops one.

The runner's backstop is ``asyncio.wait_for`` over ``run_in_executor``, which
abandons the worker thread rather than killing it. For a tool holding a session
across calls that abandoned reader would keep ``select()``ing the same pty and
race the next command for its sentinel line, so the runner rebuilds the session
before the backstopped call returns. Every follow-up therefore reads a pipe
nobody else is draining.

The assertion is a **property over repeats**, never a single-shot equality: the
race the rebuild closes has several forms and two of ten follow-ups came back
correct even without it, so one round proves nothing in either direction.

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

import asyncio
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
from tolokaforge.runner.models import ToolExecutorIdentity, ToolSchema
from tolokaforge.runner.tool_factory import (
    DockerComposeExecToolWrapper,
    PersistentShellToolWrapper,
    ToolLifecycleContext,
    ToolWrapper,
)
from tolokaforge.tools.persistent_shell import _SENTINEL_PREFIX
from tolokaforge.tools.registry import ToolExecutionStatus

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
    assert not unhealthy, (
        "a follow-up after a backstopped command did not return its own output, so "
        "the session it ran on was not rebuilt and an abandoned reader is still on "
        "the pty:\n" + "\n".join(f.describe() for f in unhealthy)
    )


def test_no_call_after_a_backstop_is_recorded_as_a_success_with_empty_output(
    runner_service, mock_grpc_context, shell_trial
) -> None:
    """A stolen empty result recorded as a success is a grading defect, not just
    an agent-experience one.

    ``required_tools`` is satisfied only by a call whose status is ``success``,
    and trace-check ``result`` matchers read the same string — so a follow-up
    that recorded success while the genuine call before it recorded a timeout
    would credit work nothing did.
    """
    _drive_rounds(runner_service, mock_grpc_context, shell_trial)

    empty_successes = [
        call
        for call in runner_service.trials[shell_trial].tool_call_history
        if call.status is ToolExecutionStatus.SUCCESS and not _without_ansi(call.output).strip()
    ]
    assert not empty_successes, (
        "a call recorded success with no output — a required-tool check reading this "
        f"record would be satisfied by work that never happened: {empty_successes}"
    )


def test_the_rebuild_uses_the_context_registration_stored(
    runner_service, mock_grpc_context, shell_trial
) -> None:
    """Identity, not equality.

    A context rebuilt from the runner's own constants would compare equal for
    today's two lifecycle tools and still be wrong: it would carry no
    ``artifacts_dir``, which the next lifecycle tool to need one would lose
    silently.
    """
    wrapper = _new_session_wrapper()
    started_with: list[ToolLifecycleContext] = []
    real_start = wrapper.start

    def _recording_start(ctx: ToolLifecycleContext) -> None:
        started_with.append(ctx)
        real_start(ctx)

    wrapper.start = _recording_start
    wrapper.start(ToolLifecycleContext(trial_id=shell_trial, work_dir=None))
    runner_service.trials[shell_trial].agent_tools["bash_session"] = wrapper
    try:
        runner_service.ExecuteTool(
            execute_request(
                shell_trial,
                "bash_session",
                f'{{"command": "{_RUNAWAY_COMMAND}"}}',
                call_id="toolu_ctx_probe",
                timeout_seconds=BACKSTOP_BAND_S,
            ),
            mock_grpc_context,
        )
    finally:
        wrapper.stop()

    stored = runner_service.trials[shell_trial].lifecycle_ctx
    assert stored is not None, "registration stored no lifecycle context to rebuild against"
    assert len(started_with) == 2, f"the session was not rebuilt: started {len(started_with)}x"
    assert started_with[-1] is stored, (
        "the rebuild used a context registration did not store, so anything "
        f"registration resolved for this trial is lost: {started_with[-1]}"
    )


def test_a_session_that_cannot_be_rebuilt_refuses_every_later_call_by_name(
    runner_service, mock_grpc_context, shell_trial
) -> None:
    """Nothing usable is left behind, so nothing may be served from it.

    The rebuild is the only thing standing between the next call and a pipe an
    abandoned reader still holds. When it fails there is no degraded mode to
    fall back to, so the tool is refused by name for the rest of the trial.
    """
    wrapper = _new_session_wrapper()
    wrapper.start(ToolLifecycleContext(trial_id=shell_trial, work_dir=None))
    runner_service.trials[shell_trial].agent_tools["bash_session"] = wrapper

    def _refuse_to_open(ctx: ToolLifecycleContext) -> None:
        raise RuntimeError("no pty available")

    wrapper.start = _refuse_to_open
    try:
        backstopped = runner_service.ExecuteTool(
            execute_request(
                shell_trial,
                "bash_session",
                f'{{"command": "{_RUNAWAY_COMMAND}"}}',
                call_id="toolu_unrebuildable",
                timeout_seconds=BACKSTOP_BAND_S,
            ),
            mock_grpc_context,
        )
        later = runner_service.ExecuteTool(
            execute_request(
                shell_trial,
                "bash_session",
                '{"command": "echo AFTER"}',
                call_id="toolu_after_failed_rebuild",
                timeout_seconds=FOLLOWUP_BAND_S,
            ),
            mock_grpc_context,
        )
    finally:
        wrapper.stop()

    assert backstopped.status == pb2.EXECUTION_STATUS_TIMEOUT
    assert "no pty available" in backstopped.error_message, backstopped.error_message

    assert later.status == pb2.EXECUTION_STATUS_ERROR, (
        "a call landed on a session the runner could not rebuild, so it read a pipe "
        f"nobody owns: status={later.status} output={later.output!r}"
    )
    assert "unusable" in later.error_message, later.error_message
    assert "no pty available" in later.error_message, (
        "the refusal does not name why the tool is unusable, so an operator reading "
        f"the record cannot tell: {later.error_message!r}"
    )


def test_a_tool_with_no_session_to_rebuild_does_not_claim_one_was_reset(
    runner_service, mock_grpc_context, shell_trial
) -> None:
    """``docker_compose_exec`` is a lifecycle tool that holds no session.

    Its ``start()`` resolves a container name and nothing else — the compose
    stack belongs to the per-trial runtime — so closing and reopening it clears
    nothing an abandoned worker could serve. Telling the agent its session was
    reset would be a claim the runner cannot honour.
    """
    wrapper = DockerComposeExecToolWrapper(
        ToolSchema(
            name="run_command",
            description="exec into a running service",
            parameters={"type": "object", "properties": {"command": {"type": "string"}}},
        ),
        service="app",
        compose_project_prefix="p",
    )
    wrapper.start(ToolLifecycleContext(trial_id=shell_trial, work_dir=None))

    def _hangs_past_the_band(command: str, timeout: float) -> str:
        time.sleep(BACKSTOP_BAND_S * 4)
        return ""

    wrapper._exec_sync = _hangs_past_the_band
    runner_service.trials[shell_trial].agent_tools["run_command"] = wrapper

    response = runner_service.ExecuteTool(
        execute_request(
            shell_trial,
            "run_command",
            '{"command": "sleep 30"}',
            call_id="toolu_compose_exec",
            timeout_seconds=BACKSTOP_BAND_S,
        ),
        mock_grpc_context,
    )

    assert response.status == pb2.EXECUTION_STATUS_TIMEOUT
    assert "session was reset" not in response.error_message, (
        "the call told the agent its session was reset, but this tool holds no "
        f"session to reset: {response.error_message!r}"
    )


async def test_rebuilding_a_session_does_not_stall_the_runners_event_loop(
    runner_service, mock_grpc_context, shell_trial
) -> None:
    """The runner serves every concurrent trial from one loop.

    ``stop()`` and ``start()`` are synchronous and slow — a SIGKILL wait, and on
    the compose engine a reopened exec — so a rebuild on the loop thread would
    freeze every other trial for its duration.
    """
    blocked_for = BACKSTOP_BAND_S

    class _BlockingRebuild(ToolWrapper):
        has_lifecycle = True

        def stop(self) -> None:
            time.sleep(blocked_for)

        def start(self, ctx: ToolLifecycleContext) -> None:
            pass

        async def execute(self, arguments: dict) -> str:  # pragma: no cover — never called
            return ""

    tool = _BlockingRebuild(
        ToolSchema(name="wedged", description="blocks on rebuild", parameters={})
    )
    trial_context = runner_service.trials[shell_trial]

    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    ticker = asyncio.create_task(tick())
    try:
        await runner_service._reset_backstopped_tool(
            trial_context, tool, "wedged", ToolExecutorIdentity.AGENT, 1.0
        )
    finally:
        ticker.cancel()

    assert ticks > 10, (
        f"the loop ticked {ticks} times while a {blocked_for}s rebuild ran — the "
        "rebuild is holding the event loop, so every concurrent trial is stalled"
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
