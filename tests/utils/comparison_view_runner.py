"""Drive the runner's real hash grading over two chosen states, through its own db-service.

Every comparison-view test that grades on the runner does the same four things: register
a task through ``RegisterTrial``, write the trial's final state into the trial's
db-service, give the golden replay an action that writes the golden state into the same
database, and call ``GradeTrial``. The evaluator then reads both states back the way it
reads them in production — full states over ``DBServiceClient.get_state``, snapshots,
reset, replay, restore — so what the test pins is the runner's own path, not a mapping a
hasher was handed.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from enum import Enum
from typing import Any

from tests.utils.runner_requests import register_request, trial_spec_json
from tolokaforge.runner import models as runner_models
from tolokaforge.runner import runner_pb2 as pb2

__all__ = [
    "GOLDEN_TOOL",
    "TRANSCRIPT",
    "Verdict",
    "grade_through_the_runner",
    "register",
    "verdict_of",
    "write_state",
]

GOLDEN_TOOL = "write_golden_state"
"""The one golden action a task built by these helpers replays: it writes the golden state."""

TRANSCRIPT = json.dumps([{"role": "assistant", "content": "Done."}])


class Verdict(str, Enum):
    """What a substrate made of one pair: a pass, a fail, or no grade at all."""

    PASS = "pass"
    FAIL = "fail"
    GRADING_ERROR = "grading_error"


def register(servicer: Any, context: Any, description: Mapping[str, Any], trial_id: str) -> None:
    """``RegisterTrial`` with ``description`` round-tripped through its JSON, asserted."""
    task = runner_models.TaskDescription.model_validate(dict(description))
    registered = servicer.RegisterTrial(
        register_request(
            trial_spec_json(task.model_dump(mode="json"), trial_id=trial_id), trial_id=trial_id
        ),
        context,
    )
    assert registered.success is True, registered.error


async def write_state(servicer: Any, trial_id: str, state: Mapping[str, list[dict]]) -> None:
    """Make the trial's database hold exactly ``state``, row order included.

    Each table is emptied and re-filled in order, so a permutation is a permutation. A
    table the database holds and ``state`` does not is emptied, not dropped: the
    db-service keeps a table once it exists, and the states these helpers write carry
    every table on both sides for that reason.
    """
    current = (await servicer.db_client.get_state(trial_id)).data
    for table in sorted(set(current) | set(state)):
        operations: list[dict[str, Any]] = []
        if table in current:
            operations.append({"op": "delete", "filter": {}})
        operations += [{"op": "insert", "record": dict(row)} for row in state.get(table, [])]
        if operations:
            await servicer.db_client.mutate(trial_id, table, operations)


def _golden_writer(
    servicer: Any, trial_id: str, golden: Mapping[str, list[dict]]
) -> Callable[[dict[str, Any]], Awaitable[str]]:
    async def write_golden_state(_arguments: dict[str, Any]) -> str:
        await write_state(servicer, trial_id, golden)
        return json.dumps({"written": sorted(golden)})

    return write_golden_state


def grade_through_the_runner(
    servicer: Any,
    context: Any,
    *,
    description: Mapping[str, Any],
    trial_id: str,
    trial: Mapping[str, list[dict]],
    golden: Mapping[str, list[dict]] | None = None,
) -> pb2.GradeTrialResponse:
    """Register ``description``, leave ``trial`` in its database, and grade it.

    With ``golden``, the task's one golden action writes it; ``description`` must then
    declare ``golden_actions: [{tool_name: GOLDEN_TOOL}]``. Without it, the task's own
    hash source decides the golden side (``expect_initial_state``).
    """
    register(servicer, context, description, trial_id)
    if golden is not None:
        servicer.trials[trial_id].agent_tools[GOLDEN_TOOL] = _golden_writer(
            servicer, trial_id, golden
        )
    servicer._run_async(write_state(servicer, trial_id, trial))
    return servicer.GradeTrial(
        pb2.GradeTrialRequest(trial_id=trial_id, llm_messages_json=TRANSCRIPT), context
    )


def verdict_of(response: pb2.GradeTrialResponse) -> Verdict:
    """The runner's answer as a :class:`Verdict`, read off the ``state_checks`` component."""
    if not response.success:
        return Verdict.GRADING_ERROR
    score = response.grade.components.state_checks
    assert score in (0.0, 1.0), f"a hash-only grade scored {score}"
    return Verdict.PASS if score == 1.0 else Verdict.FAIL
