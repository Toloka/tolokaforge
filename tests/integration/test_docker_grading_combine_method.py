"""The author's ``combine.method`` survives gRPC, and a retired alias never lands.

``tests/canonical/test_grading_substrate_parity.py`` drives the same three
aggregations through both substrates in-process. That is a claim about the
composer, not about the shipped score: the runner decodes ``combine_method`` off
an ``extra="forbid"`` wire model inside a built image, so a field whose declared
domain changed can be right in the tree and wrong in the container. This suite
closes that gap on the production path — real ``RegisterTrial`` + ``GradeTrial``
against the runner image, with no LLM.

One trial, two deterministic components it splits: a JSONPath assertion the
registered DB contradicts scores ``0.0``, and a ``must_contain`` rule the sent
transcript satisfies scores ``1.0``. Their min, mean and max are three different
numbers, so the three aggregations are distinguishable; on equal components every
method returns the same score and nothing here could fail. Both components carry a
declared weight and neither is judge- nor probe-graded, so the trial is one both
substrates score the same way and the method is the only variable.

The components, the threshold and the expected verdicts come from
``tests/utils/combine_method_verdicts.py``, which the in-process differential reads
too: one hand-written table, so the wire is held to the prediction the canonical tier
makes rather than to a copy of it that can drift green. The sweep below is over
``COMBINE_METHODS``, so a fourth declared method arrives here graded and without an
answer rather than quietly unswept.

**``binary_pass`` is asserted alongside the score but is not separate evidence.**
``min(s) >= t`` is ``all(s >= t)`` and ``max(s) >= t`` is ``any(s >= t)``, so a
composer comparing the score to the threshold uniformly is indistinguishable from
the per-method predicates. No patch reddens the flag half alone.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.utils.combine_method_verdicts import (
    COMBINE_METHOD_COMPONENTS,
    COMBINE_METHOD_PASS_THRESHOLD,
    COMBINE_METHOD_VERDICTS,
)
from tolokaforge.core.grading.combine_method import (
    COMBINE_METHODS,
    RETIRED_COMBINE_METHOD_ALIASES,
)
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.shared_stack_runtime import GrpcRunnerClient
from tolokaforge.core.trial import EnvEndpoints, TrialSpec
from tolokaforge.runner.models import TaskDescription

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]

_TASK_ID = "combine_method_wire"

_RETIRED_ALIAS = "all_pass"
_ALIAS_REPLACEMENT = RETIRED_COMBINE_METHOD_ALIASES[_RETIRED_ALIAS]

_ORDER_ID = "O1"
_TRIAL_STATUS = "pending"
_ASSERTED_STATUS = "shipped"
_REFUND_SENTENCE = "Refund issued"

# No tool calls, so the timeline is a message view with no records to join.
_TRANSCRIPT: list[dict[str, Any]] = [
    {"role": "user", "content": "Please refund my order."},
    {"role": "assistant", "content": f"{_REFUND_SENTENCE} for order {_ORDER_ID}."},
]


def _task_description() -> dict[str, Any]:
    """A trial whose two weighted components score ``0.0`` and ``1.0``."""
    return {
        "task_id": _TASK_ID,
        "name": "Combine algebra over gRPC",
        "category": "test",
        "description": "Aggregate two split deterministic components by the declared method",
        "adapter_type": "native",
        "system_prompt": "You are a test assistant.",
        "initial_state": {
            "tables": {"orders": [{"id": _ORDER_ID, "status": _TRIAL_STATUS}]},
            "schemas": [
                {
                    "table_name": "orders",
                    "fields": {"id": "string", "status": "string"},
                    "primary_key": "id",
                }
            ],
            "unstable_fields": [],
        },
        "agent_tools": [],
        "user_tools": [],
        "grading": {
            "combine_method": "weighted",
            "weights": {"state_checks": 1.0, "transcript_rules": 1.0},
            "pass_threshold": COMBINE_METHOD_PASS_THRESHOLD,
            "state_checks": {
                "hash_enabled": False,
                "golden_actions": [],
                "jsonpath_checks": [
                    {
                        "path": "$.db.orders[0].status",
                        "equals": _ASSERTED_STATUS,
                        "description": f"order {_ORDER_ID} is {_ASSERTED_STATUS}",
                    }
                ],
            },
            "transcript_rules": {"must_contain": [_REFUND_SENTENCE]},
        },
    }


def _trial_spec_json(trial_id: str, *, combine_method: str) -> str:
    """A complete ``TrialSpec`` carrying ``combine_method``, written onto the dump.

    A retired alias cannot be constructed through ``TaskDescription`` — the field's
    domain rejects it host-side, which is the narrowing this suite drives the runner
    to apply independently. Writing the value onto the serialised payload puts it on
    the wire the runner decodes, so ``RegisterTrial`` is what answers for it.
    """
    spec = TrialSpec(
        trial_id=trial_id,
        run_id=f"{_TASK_ID}_run",
        task=TaskDescription.model_validate(_task_description()),
        agent_model_config=ModelConfig(name="test-model", provider="test"),
        env_endpoints=EnvEndpoints(
            db_url="http://db.test:8000",
            runner_url="http://runner.test:50051",
        ),
    )
    payload = spec.model_dump(mode="json")
    payload["task"]["grading"]["combine_method"] = combine_method
    return json.dumps(payload)


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
def graded_by_method(runner_client: GrpcRunnerClient) -> dict[str, dict[str, Any]]:
    """method -> the grade the runner returned, one registered trial per method."""
    grades: dict[str, dict[str, Any]] = {}
    for index, method in enumerate(COMBINE_METHODS):
        trial_id = f"{_TASK_ID}_{index}:0"
        spec_json = _trial_spec_json(trial_id, combine_method=method)
        registered = runner_client.register_trial(trial_id=trial_id, trial_spec_json=spec_json)
        assert registered["success"] is True, registered["error"]
        try:
            result = runner_client.grade_trial(
                trial_id=trial_id, llm_messages_json=json.dumps(_TRANSCRIPT)
            )
        finally:
            runner_client.cleanup_trial(trial_id=trial_id)
        assert result["success"] is True, result["error"]
        assert result["grade"] is not None, result
        grades[method] = result["grade"]
    return grades


def test_a_retired_combine_method_is_rejected_at_register_trial(
    runner_client: GrpcRunnerClient,
) -> None:
    """The narrowed wire field refuses an alias before the trial exists.

    The assertion is on what only ``validate_combine_method`` writes — the rule the
    alias meant, and that it never worked. A bare ``Literal`` rejects the value too,
    with a ``literal_error`` naming the value and the supported set, so asserting
    only those would pass against a runner carrying no alias-aware message at all.
    """
    trial_id = f"{_TASK_ID}_alias:0"
    spec_json = _trial_spec_json(trial_id, combine_method=_RETIRED_ALIAS)

    registered = runner_client.register_trial(trial_id=trial_id, trial_spec_json=spec_json)

    assert registered["success"] is False, (
        f"the runner registered a trial declaring combine_method {_RETIRED_ALIAS!r}: "
        f"{registered}. The alias reaches GradeTrial, where it is graded by a rule "
        "its author did not choose"
    )
    error = registered["error"]
    assert _RETIRED_ALIAS in error, error
    assert f"Use {_ALIAS_REPLACEMENT!r}" in error, error
    assert "never worked" in error, error


@pytest.mark.parametrize(("method", "verdict"), sorted(COMBINE_METHOD_VERDICTS.items()))
def test_the_declared_combine_method_aggregates_the_wire_trial(
    method: str, verdict: tuple[float, bool], graded_by_method: dict[str, dict[str, Any]]
) -> None:
    """The runner returns the verdict the canonical differential owes for ``method``.

    Every input to that verdict is observed rather than assumed: the two components
    come back off the wire, so a cell agreeing for the wrong reason — one component
    silently unscored, say — fails on the component assertion first. ``binary_pass``
    is checked alongside the score and is the same evidence, not a second piece.
    """
    grade = graded_by_method[method]
    components = {name: grade["components"][name] for name in sorted(COMBINE_METHOD_COMPONENTS)}
    assert components == pytest.approx(COMBINE_METHOD_COMPONENTS), (
        f"the runner scored the components {components}, not {COMBINE_METHOD_COMPONENTS} — "
        f"the verdict {verdict} owed for {method!r} is the answer to different inputs"
    )

    expected_score, expected_pass = verdict
    assert grade["score"] == pytest.approx(expected_score), (
        f"the runner aggregated {COMBINE_METHOD_COMPONENTS} by {method!r} into "
        f"{grade['score']}, not {expected_score}"
    )
    assert grade["binary_pass"] is expected_pass, (
        f"{method!r} scored {grade['score']} against pass_threshold {COMBINE_METHOD_PASS_THRESHOLD} "
        f"and reported binary_pass {grade['binary_pass']}, not {expected_pass}"
    )


def test_the_declared_methods_score_the_same_wire_trial_differently(
    graded_by_method: dict[str, dict[str, Any]],
) -> None:
    """The method the author declared reaches the fold, not merely the wire model.

    Survives this module's answer table being wrong: no method-independent
    aggregation satisfies it, however the expected verdicts above drift. That is
    what the equality half cannot carry — one shared dispatch makes both substrates
    agree however wrong it is, and a runner that decoded ``combine_method`` and then
    ignored it would still return three identical, plausible scores.
    """
    scores = [graded_by_method[method]["score"] for method in COMBINE_METHODS]
    assert len(set(scores)) == len(COMBINE_METHODS), (
        f"the runner scored the same trial {scores} across methods {COMBINE_METHODS}. "
        "The author's combine.method survives the wire but not the aggregation"
    )
