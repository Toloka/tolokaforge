"""The runner folds its own hash verdict with its JSONPath score, over real services.

``tests/canonical/test_grading_substrate_parity.py`` drives the same fold at the
canonical tier, but it *hands* the runner's composer a hash score: the runner's hash
evaluator replays golden actions against db-service over HTTP, so no in-process
differential can reach it, and mocking the DB client on the one test whose job is to
detect substrate drift would defeat that test (#687). This suite closes the gap on the
production path — ``RegisterTrial`` over real gRPC, golden replay against the real
db-service, and the ``state_checks`` component read back off the wire — with no LLM.

Across the four fold cells the trial's database is the registered ``initial_state``:
nothing runs an agent, so the only thing that moves between cells is what the golden
replay leaves behind. One golden action sets the single order's status — to the value
the trial already holds (the hashes match) or to a different one (they diverge). Two
JSONPath assertions on that same field, of which exactly one holds either way, keep the
assertion half at a partial ``0.5`` in both cases, so the hash verdict is the only
variable. The unresolvable-name case drives one real ``ExecuteTool`` first, because
what it asserts is that the trial's own state survived a failed grade, and a database
still holding ``initial_state`` cannot tell a reset apart from no reset.

Two defective golden paths sit beside those cells, and they answer differently on
purpose: a name resolving to nothing fails the whole ``GradeTrial``, while an action that
resolved and raised leaves a partial world the runner still hashes against and names in
the grade's reasons.

Both weights are strictly inside ``(0, 1)``. At ``0.0`` and ``1.0`` the blend collapses
onto a single source, and a rule that merely *selects* the dominant source reproduces
it exactly there; only an interior weight tells the two apart.

The four cells are ``0.625`` and ``0.8`` for a matching hash at weights ``0.25`` and
``0.6``, and ``0.375`` and ``0.2`` for a diverging one. A rule that multiplied the two
sources rather than blending them returns ``0.5``, ``0.5``, ``0.0``, ``0.0`` — no cell
agrees, so every cell discriminates.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from tolokaforge.core.models import ModelConfig
from tolokaforge.core.shared_stack_runtime import GrpcRunnerClient
from tolokaforge.core.trial import EnvEndpoints, TrialSpec
from tolokaforge.runner.models import TaskDescription

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]

_TOOL_ARTIFACT_PATH = "hash_composition_tools/order_status.py"
_TOOL_SOURCE = Path(__file__).resolve().parents[1] / "data" / "tool_artifacts" / _TOOL_ARTIFACT_PATH

_TASK_ID = "hash_composition_wire"
_TOOL_NAME = "set_order_status"
_ORDER_ID = "O-1"

# The status the registered initial_state holds, and therefore the status the trial's
# database still holds at grade time.
_TRIAL_STATUS = "new"
_DIVERGING_STATUS = "fulfilled"

# A third status, and a golden action naming a tool ``RegisterTrial`` never registered.
# The typo is a suffix of nothing registered, so the runner's ``…_<name>`` rule cannot
# rescue it either.
_MOVED_STATUS = "picking"
_UNREGISTERED_TOOL = "set_order_statuss"

# An order the initial state does not hold, so the golden action resolves, runs, and
# raises — the tool's own ``ValueError`` reaches the replay loop through the tau wrapper.
_ABSENT_ORDER = "O-404"

# (case name, the status its golden action replays, the hash verdict that produces).
_GOLDEN_CASES: tuple[tuple[str, str, float], ...] = (
    ("golden_matching", _TRIAL_STATUS, 1.0),
    ("golden_diverging", _DIVERGING_STATUS, 0.0),
)

_HASH_WEIGHTS: tuple[float, ...] = (0.25, 0.6)

_SATISFIED_ASSERTION = "status is still new"
_VIOLATED_ASSERTION = "status was advanced to fulfilled"

# Exactly one of the two assertions holds in either case, on the one state both cases
# share. Asserted by observation on every cell rather than assumed.
_JSONPATH_SCORE = 0.5

_JSONPATH_CHECKS: list[dict[str, Any]] = [
    {
        "path": "$.db.orders[0].status",
        "equals": _TRIAL_STATUS,
        "description": _SATISFIED_ASSERTION,
    },
    {
        "path": "$.db.orders[0].status",
        "equals": _DIVERGING_STATUS,
        "description": _VIOLATED_ASSERTION,
    },
]


def _tool_artifacts() -> dict[str, str]:
    """The golden-action tool, base64'd onto the wire field the runner extracts.

    No ``__init__.py``: the runner puts the extraction directory on ``sys.path``, and
    an implicit namespace package is enough to import ``hash_composition_tools``.
    """
    return {_TOOL_ARTIFACT_PATH: base64.b64encode(_TOOL_SOURCE.read_bytes()).decode()}


def _task_description(
    *,
    golden_status: str,
    hash_weight: float,
    golden_tool: str = _TOOL_NAME,
    golden_order: str = _ORDER_ID,
) -> dict[str, Any]:
    """A hash-graded trial whose assertion half is partial and whose weight is explicit."""
    return {
        "task_id": _TASK_ID,
        "name": "Hash / JSONPath composition over gRPC",
        "category": "test",
        "description": "Fold a real golden-replay hash verdict with a partial JSONPath score",
        "adapter_type": "native",
        "system_prompt": "You are a test assistant.",
        "initial_state": {
            "tables": {"orders": [{"id": _ORDER_ID, "status": _TRIAL_STATUS, "quantity": 3}]},
            "schemas": [
                {
                    "table_name": "orders",
                    "fields": {"id": "string", "status": "string", "quantity": "integer"},
                    "primary_key": "id",
                }
            ],
            "unstable_fields": [],
        },
        "agent_tools": [
            {
                "name": _TOOL_NAME,
                "description": "Set an order's status",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "order_id": {"type": "string"},
                        "status": {"type": "string"},
                    },
                    "required": ["order_id", "status"],
                    "additionalProperties": False,
                },
                "category": "write",
                "source": {
                    "toolset": "hash_composition_tools",
                    "module_path": "order_status",
                    "class_name": "SetOrderStatus",
                    "invocation_style": "tau_sync",
                },
            }
        ],
        "user_tools": [],
        "tool_artifacts": _tool_artifacts(),
        "grading": {
            "combine_method": "weighted",
            "weights": {"state_checks": 1.0},
            "pass_threshold": 0.99,
            "state_checks": {
                "hash_enabled": True,
                "hash_weight": hash_weight,
                "golden_actions": [
                    {
                        "tool_name": golden_tool,
                        "arguments": {"order_id": golden_order, "status": golden_status},
                    }
                ],
                "jsonpath_checks": _JSONPATH_CHECKS,
            },
        },
    }


def _trial_spec_json(trial_id: str, task: dict[str, Any]) -> str:
    """A complete ``TrialSpec``, which is what ``RegisterTrial`` validates."""
    return TrialSpec(
        trial_id=trial_id,
        run_id="hash_composition_wire_run",
        task=TaskDescription.model_validate(task),
        agent_model_config=ModelConfig(name="test-model", provider="test"),
        env_endpoints=EnvEndpoints(
            db_url="http://db.test:8000",
            runner_url="http://runner.test:50051",
        ),
    ).model_dump_json()


def _state(client: GrpcRunnerClient, trial_id: str) -> dict[str, Any]:
    """The trial's tables as db-service holds them right now."""
    response = client.get_state(trial_id)
    assert response["success"] is True, response["error"]
    return json.loads(response["state_json"])


@pytest.fixture(scope="module")
def runner_client(runner_container) -> GrpcRunnerClient:
    """RunnerClient connected to the testcontainer Runner over gRPC."""
    host = runner_container.get_container_host_ip()
    port = runner_container.get_exposed_port(50051)
    client = GrpcRunnerClient(runner_address=f"{host}:{port}")
    client.connect()
    yield client
    client.close()


@pytest.fixture(scope="module")
def graded_cells(runner_client: GrpcRunnerClient) -> dict[tuple[str, float], dict[str, Any]]:
    """(case, weight) -> the grade the runner returned, one registered trial per cell."""
    grades: dict[tuple[str, float], dict[str, Any]] = {}
    for case_index, (case, golden_status, _) in enumerate(_GOLDEN_CASES):
        for weight_index, weight in enumerate(_HASH_WEIGHTS):
            trial_id = f"{_TASK_ID}_{case_index}_{weight_index}:0"
            task = _task_description(golden_status=golden_status, hash_weight=weight)
            spec_json = _trial_spec_json(trial_id, task)
            registered = runner_client.register_trial(trial_id=trial_id, trial_spec_json=spec_json)
            assert registered["success"] is True, registered["error"]
            try:
                result = runner_client.grade_trial(trial_id=trial_id)
            finally:
                runner_client.cleanup_trial(trial_id=trial_id)
            assert result["success"] is True, result["error"]
            assert result["grade"] is not None, result
            grades[(case, weight)] = result["grade"]
    return grades


@pytest.mark.parametrize("weight", _HASH_WEIGHTS)
@pytest.mark.parametrize(
    ("case", "hash_score"), [(case, score) for case, _, score in _GOLDEN_CASES]
)
def test_the_runner_blends_its_golden_replay_verdict_by_the_authors_weight(
    case: str, hash_score: float, weight: float, graded_cells
) -> None:
    """The wire's ``state_checks`` is ``j(1 - w) + hw`` over the verdict the runner produced.

    Every input to that arithmetic is observed rather than assumed: the reasons string
    carries the hash verdict, and the two JSONPath verdicts pin ``j`` at ``0.5``. A
    golden replay that silently skipped an action would report a plausible hash over a
    partially replayed world (#734), so the absence of replay errors is asserted too.
    """
    grade = graded_cells[(case, weight)]
    reasons = grade["reasons"]

    assert "GOLDEN REPLAY ERRORS" not in reasons, reasons
    if hash_score == 1.0:
        assert "State: hash match" in reasons, reasons
    else:
        assert "State: hash match" not in reasons, reasons
    assert f"PASS: {_SATISFIED_ASSERTION}" in reasons, reasons
    assert f"FAIL: {_VIOLATED_ASSERTION}" in reasons, reasons

    expected = _JSONPATH_SCORE * (1.0 - weight) + hash_score * weight
    component = grade["components"]["state_checks"]
    assert component == pytest.approx(expected), (
        f"the runner folded hash {hash_score} with jsonpath {_JSONPATH_SCORE} at weight "
        f"{weight} into {component}, not the blend {expected}"
    )
    assert grade["score"] == pytest.approx(expected), (
        f"the runner reports state_checks {component} but scores the trial "
        f"{grade['score']} — the composite does not reach the final score"
    )


def test_an_unresolvable_golden_action_fails_the_grade_and_leaves_the_trial_alone(
    runner_client: GrpcRunnerClient,
) -> None:
    """A pack defect answers ``success=false`` without spending the trial's database.

    The trial is first driven off its registered ``initial_state`` through a real
    ``ExecuteTool``, because this module grades trials nothing ran an agent against:
    with the database still holding ``initial_state``, ``reset_trial`` is a no-op and a
    post-grade state check would pass even if the runner had snapshotted and reset
    before discovering the unresolvable name. Having moved it, the same check falsifies
    that ordering — a reset trial reads back ``_TRIAL_STATUS``.

    The error has to name both the action as written and the tools the trial registered:
    that pair is the whole of what tells an author the golden path is broken rather than
    the agent. A runner that skipped the action instead answers ``success=true`` with a
    ``state_checks`` computed against a golden world the action never touched, annotating
    the grade where an unbuildable world has to refuse one.
    """
    trial_id = f"{_TASK_ID}_unresolvable:0"
    task = _task_description(
        golden_status=_TRIAL_STATUS, hash_weight=_HASH_WEIGHTS[0], golden_tool=_UNREGISTERED_TOOL
    )
    registered = runner_client.register_trial(
        trial_id=trial_id, trial_spec_json=_trial_spec_json(trial_id, task)
    )
    assert registered["success"] is True, registered["error"]

    try:
        moved = runner_client.execute_tool(
            trial_id=trial_id,
            tool_name=_TOOL_NAME,
            arguments={"order_id": _ORDER_ID, "status": _MOVED_STATUS},
            call_id="move-the-trial-off-its-initial-state",
        )
        assert moved.success is True, moved.error
        before = _state(runner_client, trial_id)
        assert before["orders"][0]["status"] == _MOVED_STATUS, before

        result = runner_client.grade_trial(trial_id=trial_id)

        assert result["success"] is False, result
        assert _UNREGISTERED_TOOL in result["error"], result["error"]
        assert _TOOL_NAME in result["error"], result["error"]
        assert _state(runner_client, trial_id) == before, (
            f"grading a pack whose golden action names {_UNREGISTERED_TOOL!r} moved the "
            f"trial's database off {before} — it was snapshotted or reset on the way to "
            "an error it could have raised first"
        )
    finally:
        runner_client.cleanup_trial(trial_id=trial_id)


def test_a_golden_action_that_raised_is_named_on_the_grade_it_produced(
    runner_client: GrpcRunnerClient,
) -> None:
    """An action that resolved and raised still yields a verdict, and the wire says so.

    The action names an order the initial state does not hold, so it resolves, runs, and
    raises out of the tau wrapper — the one failure shape either substrate can see. The
    replay therefore leaves the trial's own database, which is what the trial holds too,
    so the hash *matches*: the grade is a full-marks hash verdict over a golden world
    that was never built. That wrong verdict is #816's question and it is asserted here
    as the current answer; what this case pins is that the sentence naming the missing
    action travels with it over gRPC, under the prefix #599's consumer matches.
    """
    trial_id = f"{_TASK_ID}_raising:0"
    weight = _HASH_WEIGHTS[0]
    task = _task_description(
        golden_status=_DIVERGING_STATUS, hash_weight=weight, golden_order=_ABSENT_ORDER
    )
    registered = runner_client.register_trial(
        trial_id=trial_id, trial_spec_json=_trial_spec_json(trial_id, task)
    )
    assert registered["success"] is True, registered["error"]

    try:
        result = runner_client.grade_trial(trial_id=trial_id)
    finally:
        runner_client.cleanup_trial(trial_id=trial_id)

    assert result["success"] is True, result["error"]
    grade = result["grade"]
    reasons = grade["reasons"]

    assert "GOLDEN REPLAY ERRORS: 1 of 1" in reasons, reasons
    assert _TOOL_NAME in reasons, reasons
    assert _ABSENT_ORDER in reasons, reasons
    assert "State: hash match" in reasons, reasons

    expected = _JSONPATH_SCORE * (1.0 - weight) + 1.0 * weight
    assert grade["components"]["state_checks"] == pytest.approx(expected), grade


def test_the_two_weights_score_the_same_trial_differently(graded_cells) -> None:
    """The weight the author wrote reaches the fold, not merely the config model.

    Survives this module's own arithmetic being wrong: no weight-independent rule
    satisfies it, however the expected blend above drifts. Driven at interior weights
    because the blend collapses onto one source at ``0.0`` and ``1.0``.
    """
    interior = [weight for weight in _HASH_WEIGHTS if 0.0 < weight < 1.0]
    assert len(set(interior)) >= 2, (
        f"_HASH_WEIGHTS {_HASH_WEIGHTS} holds fewer than two distinct weights strictly "
        "inside (0, 1), so this test compares nothing a selection rule would fail"
    )

    for case, _, _ in _GOLDEN_CASES:
        components = [
            graded_cells[(case, weight)]["components"]["state_checks"] for weight in interior
        ]
        assert len(set(components)) == len(components), (
            f"the runner scored {case} identically at weights {interior}: {components}. "
            "The author's state_checks.hash.weight does not reach the fold"
        )
