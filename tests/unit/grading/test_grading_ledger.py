"""Runtime accounted-keys ledger — the guard against a scored key that no-ops.

``GradeTrial`` records which author-facing ``grading.yaml`` key each evaluator
call accounts for, then subtracts those records from the keys the config actually
populated. The ``GradeTrial`` tests here drive the real ``RunnerServiceImpl`` over
the real in-process DB service (the ``runner_service`` fixture), so the grades
come from production evaluators, and they pin both directions:

* a populated scored key with no record fails the RPC naming the key;
* the config shapes shipping today — ``id_fields`` packs, degenerate trials,
  ``hash:`` without ``enabled: true`` — still grade.
"""

import base64
import json
from typing import Any

import pytest

from tests.utils.recorded_calls import recorded_call
from tests.utils.runner_requests import register_request, trial_spec_json
from tests.utils.timelines import Turn, build_turn_timeline
from tolokaforge.core.grading.key_manifest import (
    Enforcement,
    GradingKey,
    KeyKind,
    SubstrateCoverage,
    entry,
)
from tolokaforge.core.grading.trace_checks import evaluate_trace_checks
from tolokaforge.core.grading.trace_timeline import TrialTimeline
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.grading_ledger import (
    CORE_ONLY_HASH_SKIP,
    EVALUATED,
    HASH_DISABLED_SKIP,
    TRACE_CONSTRAINT_KEY_BY_KIND,
    TRACE_CONSTRAINTS_KEY,
    accountable_author_keys,
    audit_accounted_keys,
    hash_family_accounting,
    reject_hash_members_read_by_another_evaluator,
    runner_dump_path,
)
from tolokaforge.runner.models import (
    TRACE_CONSTRAINT_KINDS,
    KeyAccounting,
    KeyAccountingRecord,
    RunnerGradingConfig,
    RunnerStateChecksConfig,
    RunnerTranscriptRulesConfig,
    TraceChecksConfig,
)

pytestmark = pytest.mark.unit

_JSONPATH_CHECK = {
    "path": "$.db.widgets[0].status",
    "equals": "shipped",
    "description": "widget shipped",
}


_CHECKS_PY = """\
from tolokaforge.core.grading.checks_interface import CheckFailed, CheckPassed, check, init

widgets: list[dict] = []


@init(interface_version="1.0")
def setup(ctx):
    global widgets
    widgets = ctx.final_state.data.get("widgets", [])


@check
def widget_was_shipped():
    if any(w.get("status") == "shipped" for w in widgets):
        return CheckPassed("widget is shipped")
    return CheckFailed("widget is not shipped")
"""


def _checks_artifacts() -> dict[str, str]:
    """``checks.py`` in the base64 ``tool_artifacts`` shape the adapter delivers."""
    return {"checks.py": base64.b64encode(_CHECKS_PY.encode("utf-8")).decode("ascii")}


def _task_dict(
    grading: dict[str, Any] | None, tool_artifacts: dict[str, str] | None
) -> dict[str, Any]:
    """A registrable task carrying ``grading`` verbatim."""
    task: dict[str, Any] = {
        "task_id": "ledger_task",
        "name": "Ledger Task",
        "category": "test",
        "description": "Drives the accounted-keys ledger through GradeTrial",
        "adapter_type": "native",
        "system_prompt": "You are a test assistant.",
        "initial_state": {
            "tables": {"widgets": [{"widget_id": "w1", "status": "shipped"}]},
            "schemas": [
                {
                    "table_name": "widgets",
                    "fields": {"widget_id": "string", "status": "string"},
                    "primary_key": "widget_id",
                }
            ],
        },
        "agent_tools": [],
        "user_tools": [],
        "tool_artifacts": tool_artifacts or {},
    }
    if grading is not None:
        task["grading"] = grading
    return task


def _grade(
    runner_service: Any,
    mock_grpc_context: Any,
    trial_id: str,
    grading: dict[str, Any],
    llm_messages: list[dict[str, Any]] | None = None,
    tool_artifacts: dict[str, str] | None = None,
) -> pb2.GradeTrialResponse:
    """Register a trial with ``grading`` and grade it through the real RPC handlers."""
    register = register_request(
        trial_spec_json(_task_dict(grading, tool_artifacts), trial_id=trial_id),
        trial_id=trial_id,
    )
    registered = runner_service.RegisterTrial(register, mock_grpc_context)
    assert registered.success is True, registered.error
    request = pb2.GradeTrialRequest(
        trial_id=trial_id,
        llm_messages_json=json.dumps(llm_messages) if llm_messages else "",
    )
    return runner_service.GradeTrial(request, mock_grpc_context)


# --------------------------------------------------------------------------
# The audit itself — a populated key with no record is never a score
# --------------------------------------------------------------------------


def test_populated_scored_key_with_no_record_names_the_expected_evaluator():
    config = RunnerGradingConfig(
        state_checks=RunnerStateChecksConfig(jsonpath_checks=[_JSONPATH_CHECK])
    )

    audit = audit_accounted_keys(config, {})

    assert audit.error is not None
    assert "state_checks.jsonpaths" in audit.error
    assert entry("state_checks.jsonpaths").runner_evaluator in audit.error


def test_core_only_key_arriving_populated_quotes_its_manifest_reason():
    item = entry("state_checks.hash.expected_state_hash")
    config = RunnerGradingConfig(state_checks=RunnerStateChecksConfig(expected_hash="deadbeef"))

    audit = audit_accounted_keys(config, {})

    assert audit.error is not None
    assert item.author_key in audit.error
    assert item.reason in audit.error


def test_an_explicitly_empty_check_is_not_populated():
    """``disallowed_tools: []`` written out is indistinguishable from unset."""
    config = RunnerGradingConfig(
        transcript_rules=RunnerTranscriptRulesConfig(
            tool_expectations={"required_tools": [], "disallowed_tools": []}
        )
    )

    assert audit_accounted_keys(config, {}).error is None


def test_config_inputs_are_outside_the_ledger_by_kind():
    """``numeric_string_fields`` reaches only hash grading, so it is never evaluated."""
    config = RunnerGradingConfig(
        state_checks=RunnerStateChecksConfig(
            numeric_string_fields=["amount"],
            id_fields={"widgets": "widget_id"},
            relaxed_validation=True,
        )
    )

    assert audit_accounted_keys(config, {}).error is None


def test_a_recorded_skip_becomes_a_visible_note_not_an_error():
    config = RunnerGradingConfig(state_checks=RunnerStateChecksConfig(expected_hash="deadbeef"))

    audit = audit_accounted_keys(
        config, {"state_checks.hash.expected_state_hash": HASH_DISABLED_SKIP}
    )

    assert audit.error is None
    assert audit.skip_notes == (
        "state_checks.hash.expected_state_hash skipped: hash grading not enabled",
    )


def test_a_skipped_record_must_say_why():
    """The detail is what a task author reads, so an empty one is not a skip."""
    with pytest.raises(ValueError, match="detail rendered into Grade.reasons"):
        KeyAccountingRecord(outcome=KeyAccounting.SKIPPED)


@pytest.mark.parametrize(
    "runner_outcome", [EVALUATED, HASH_DISABLED_SKIP], ids=["hash_ran", "hash_disabled"]
)
def test_the_core_only_hash_key_is_a_skip_whichever_way_hash_grading_went(runner_outcome):
    """`expected_state_hash` has no runner reader, so hash grading running is irrelevant.

    Sharing the family's outcome would report a populated, silently dead scored
    key as fully evaluated in `grade.reasons`.
    """
    records = hash_family_accounting(runner_outcome)

    assert records["state_checks.hash.expected_state_hash"] == CORE_ONLY_HASH_SKIP
    assert records["state_checks.hash.enabled"] == runner_outcome
    assert records["state_checks.hash.golden_actions"] == runner_outcome


def test_an_evaluated_key_is_fully_accounted():
    config = RunnerGradingConfig(
        state_checks=RunnerStateChecksConfig(jsonpath_checks=[_JSONPATH_CHECK])
    )

    audit = audit_accounted_keys(config, {"state_checks.jsonpaths": EVALUATED})

    assert audit.error is None
    assert audit.skip_notes == ()


# --------------------------------------------------------------------------
# trace_checks: the evaluator records what it decomposed, kind by kind
# --------------------------------------------------------------------------

# ``before`` and ``count`` are reachable only through the conjunction, so an
# evaluator that recorded its top-level kind alone would account for neither.
_NESTED_TRACE_BLOCK: dict[str, Any] = {
    "constraints": [
        {
            "id": "checked_the_widget_before_shipping_it",
            "description": "the widget is inspected before it ships, and not re-inspected",
            "require": {
                "all_of": [
                    {
                        "before": {
                            "left": {
                                "quantifier": "any",
                                "match": {
                                    "kind": "tool_call",
                                    "tool": {"equals": "inspect_widget"},
                                },
                            },
                            "right": {
                                "quantifier": "first",
                                "match": {"kind": "tool_call", "tool": {"equals": "ship_widget"}},
                            },
                        }
                    },
                    {
                        "count": {
                            "match": {"kind": "tool_call", "tool": {"equals": "inspect_widget"}},
                            "max": 1,
                        }
                    },
                ]
            },
        }
    ]
}

_NESTED_TRACE_KEYS = {
    "trace_checks.constraints",
    "trace_checks.constraints.all_of",
    "trace_checks.constraints.before",
    "trace_checks.constraints.count",
}


def _inspected_then_shipped() -> TrialTimeline:
    return build_turn_timeline(
        [
            Turn("user", "Ship widget w1 once you have checked it."),
            Turn(
                "assistant",
                "Inspecting it.",
                recorded=[recorded_call("inspect_widget", sequence=0)],
            ),
            Turn("assistant", "Shipping it.", recorded=[recorded_call("ship_widget", sequence=1)]),
        ]
    )


def test_the_ledger_names_an_accountable_key_for_every_constraint_kind():
    """A kind with no key, or a key no site records, is a check that could no-op.

    The mapping is hand-written and the vocabulary is declared beside the models,
    so this compares two sources rather than a comprehension against itself.
    """
    assert set(TRACE_CONSTRAINT_KEY_BY_KIND) == TRACE_CONSTRAINT_KINDS

    accountable = accountable_author_keys()
    unclaimed = sorted(set(TRACE_CONSTRAINT_KEY_BY_KIND.values()) - accountable)
    assert not unclaimed, f"no recording site claims {unclaimed}"
    assert TRACE_CONSTRAINTS_KEY in accountable


def test_every_constraint_kind_the_evaluation_reaches_is_accounted_for():
    """Accounting follows the walk, so a kind nested in a composite is not lost."""
    result = evaluate_trace_checks(
        _inspected_then_shipped(), TraceChecksConfig(**_NESTED_TRACE_BLOCK)
    )

    assert [item.passed for item in result.constraints] == [True]
    assert set(result.accounted_keys) == _NESTED_TRACE_KEYS
    assert {record.outcome for record in result.accounted_keys.values()} == {
        KeyAccounting.EVALUATED
    }


def test_only_the_kinds_a_block_declares_have_to_be_accounted_for():
    """Eleven ledger keys name one list field, so populated is read per element path.

    The block below declares three of the ten kinds. Reading the ``constraints``
    field alone would mark all ten populated and demand a recording site for
    seven kinds nothing evaluated, failing every task that writes less than the
    whole vocabulary.
    """
    config = GradingConfig(trace_checks=TraceChecksConfig(**_NESTED_TRACE_BLOCK))
    accounted = evaluate_trace_checks(_inspected_then_shipped(), config.trace_checks).accounted_keys

    assert audit_accounted_keys(config, accounted).error is None

    # The other half, so the pass above is not the ledger looking at nothing: with
    # the accounting dropped the audit names the kinds the block does declare.
    starved = audit_accounted_keys(config, {})
    assert TRACE_CONSTRAINT_KEY_BY_KIND["before"] in starved.error
    assert TRACE_CONSTRAINT_KEY_BY_KIND["present"] not in starved.error


def test_a_trial_with_no_events_accounts_every_declared_kind_as_skipped():
    """Nothing is evaluated there, and every key the block declares says so."""
    result = evaluate_trace_checks(
        build_turn_timeline([]), TraceChecksConfig(**_NESTED_TRACE_BLOCK)
    )

    assert result.constraints == []
    assert set(result.accounted_keys) == _NESTED_TRACE_KEYS
    assert {record.detail for record in result.accounted_keys.values()} == {
        "the trial's timeline carries no events"
    }


# --------------------------------------------------------------------------
# runner_field resolution — a malformed manifest entry fails loud
# --------------------------------------------------------------------------


def _probe_key(
    runner_field: str,
    runner_dict_key: str | None = None,
    runner_evaluator: str | None = None,
) -> GradingKey:
    return GradingKey(
        author_key="probe.key",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.RUNNER_ONLY,
        enforcement=Enforcement.FIELD_RESOLUTION_ONLY,
        core_field=None,
        runner_field=runner_field,
        runner_dict_key=runner_dict_key,
        runner_evaluator=runner_evaluator,
        reason="a probe entry built by this test",
    )


def test_runner_field_naming_an_unknown_model_fails_loud():
    with pytest.raises(ValueError, match="not part of the runner grading config"):
        runner_dump_path(_probe_key("GradingCombineConfig.method"))


def test_runner_field_naming_an_unknown_field_fails_loud():
    with pytest.raises(ValueError, match="has no field 'jsonpath_chekcs'"):
        runner_dump_path(_probe_key("RunnerStateChecksConfig.jsonpath_chekcs"))


def test_runner_dict_key_is_not_resolvable():
    with pytest.raises(ValueError, match="runner_dict_key"):
        runner_dump_path(
            _probe_key("RunnerStateChecksConfig.jsonpath_checks", runner_dict_key="enabled")
        )


def test_a_hash_family_member_another_evaluator_reads_fails_loud():
    """The family shares one outcome, so a second reader needs its own recording site."""
    foreign = _probe_key(
        "RunnerStateChecksConfig.golden_actions",
        runner_evaluator="tolokaforge.runner.grading.evaluate_golden_action_traces",
    )

    with pytest.raises(ValueError, match="needs its own recording site"):
        reject_hash_members_read_by_another_evaluator([foreign])


# --------------------------------------------------------------------------
# GradeTrial: the config shapes shipping today still grade
# --------------------------------------------------------------------------


def test_id_fields_shaped_config_grades_instead_of_erroring(runner_service, mock_grpc_context):
    """The config-input false-positive class: every `id_fields` pack shipping today."""
    grading = {
        "combine_method": "weighted",
        "weights": {"state_checks": 1.0, "transcript_rules": 1.0},
        "pass_threshold": 0.7,
        "state_checks": {
            "numeric_string_fields": ["amount"],
            "id_fields": {"widgets": "widget_id"},
            "relaxed_validation": True,
            "jsonpath_checks": [_JSONPATH_CHECK],
        },
        "transcript_rules": {"must_contain": ["done"]},
    }

    response = _grade(
        runner_service,
        mock_grpc_context,
        "ledger_id_fields:0",
        grading,
        llm_messages=[{"role": "assistant", "content": "All done"}],
    )

    assert response.success is True, response.error
    assert response.grade.score == pytest.approx(1.0)
    assert response.grade.binary_pass is True


def test_every_transcript_rule_and_jsonpath_key_grades_together(runner_service, mock_grpc_context):
    """A config populating every runner-graded scored key still produces a grade.

    Pins each author key the evaluators record: a typo in one of them would leave
    that key unaccounted and fail the RPC instead of grading.

    The `5 / 6` fraction is also what pins the activity floor's "no row when met"
    rule. The trial carries one assistant turn, so the declared floor of 1 is met
    and contributes no seventh sub-check — a passing row would move this to 6 / 7.
    """
    grading = {
        "combine_method": "weighted",
        "weights": {"state_checks": 1.0, "transcript_rules": 1.0},
        "pass_threshold": 0.7,
        "state_checks": {"jsonpath_checks": [_JSONPATH_CHECK]},
        "transcript_rules": {
            "must_contain": ["shipped"],
            "disallow_regex": ["password"],
            "max_turns": 5,
            "min_assistant_turns": 1,
            "tool_expectations": {"required_tools": [], "disallowed_tools": ["delete_widget"]},
            "required_actions": [
                {
                    "action_id": "a1",
                    "requestor": "assistant",
                    "tool_name": "ship_widget",
                    "arguments": {"widget_id": "w1"},
                }
            ],
            "communicate_info": [{"info": "shipped", "required": True}],
        },
    }

    response = _grade(
        runner_service,
        mock_grpc_context,
        "ledger_all_keys:0",
        grading,
        llm_messages=[{"role": "assistant", "content": "The widget was shipped"}],
    )

    assert response.success is True, response.error
    # required_actions fails (no tool ran); the other five sub-checks pass.
    assert response.grade.components.transcript_rules == pytest.approx(5 / 6)
    assert response.grade.components.state_checks == pytest.approx(1.0)


def test_degenerate_trial_records_the_transcript_skip_in_reasons(runner_service, mock_grpc_context):
    """No messages and no tool history: score badly, never error the RPC."""
    grading = {
        "combine_method": "weighted",
        "weights": {"transcript_rules": 1.0},
        "pass_threshold": 0.7,
        "transcript_rules": {"must_contain": ["done"]},
    }

    response = _grade(runner_service, mock_grpc_context, "ledger_degenerate:0", grading)

    assert response.success is True, response.error
    assert response.grade.binary_pass is False
    assert response.grade.score == pytest.approx(0.0)
    assert (
        "transcript_rules.must_contain skipped: the trial's timeline carries no events"
        in response.grade.reasons
    )


def test_degenerate_trial_still_grades_a_declared_activity_floor(runner_service, mock_grpc_context):
    """The most degenerate trial there is must not escape a declared floor.

    No messages and no tool history is exactly the answer `min_assistant_turns`
    asks for, so the floor is evaluated where its siblings are skipped: the
    component is `0.0` rather than absent from the combine, the reason names the
    bound, the floor itself is never recorded as skipped, and the sibling's skip
    note survives the split.
    """
    grading = {
        "combine_method": "weighted",
        "weights": {"transcript_rules": 1.0},
        "pass_threshold": 0.7,
        "transcript_rules": {"min_assistant_turns": 1, "must_contain": ["done"]},
    }

    response = _grade(runner_service, mock_grpc_context, "ledger_degenerate_floor:0", grading)

    assert response.success is True, response.error
    assert response.grade.binary_pass is False
    assert response.grade.components.transcript_rules == pytest.approx(0.0)
    assert "Assistant turn count 0 below min_assistant_turns of 1" in response.grade.reasons
    # The blanket skip sweeps the whole subtree by key prefix, so the floor is the one
    # member that must be carved out of it. Left in, the reasons would say the floor
    # drove the verdict *and* was never evaluated.
    assert "transcript_rules.min_assistant_turns skipped" not in response.grade.reasons
    assert (
        "transcript_rules.must_contain skipped: the trial's timeline carries no events"
        in response.grade.reasons
    )


def test_golden_actions_without_hash_enabled_records_the_hash_skip(
    runner_service, mock_grpc_context
):
    """The adapter fills golden_actions whether or not `hash.enabled` is set."""
    grading = {
        "combine_method": "weighted",
        "weights": {"state_checks": 1.0},
        "pass_threshold": 0.7,
        "state_checks": {
            "hash_enabled": False,
            "golden_actions": [{"tool_name": "ship_widget", "arguments": {"widget_id": "w1"}}],
            "jsonpath_checks": [_JSONPATH_CHECK],
        },
    }

    response = _grade(runner_service, mock_grpc_context, "ledger_hash_off:0", grading)

    assert response.success is True, response.error
    assert response.grade.score == pytest.approx(1.0)
    assert (
        "state_checks.hash.golden_actions skipped: hash grading not enabled"
        in response.grade.reasons
    )


def test_expected_hash_is_reported_as_read_by_nothing_on_the_runner(
    runner_service, mock_grpc_context
):
    """A populated key the manifest declares core-only never reads as evaluated.

    The adapter fills `expected_hash` from `hash.expected_state_hash` and no runner
    path reads it, so the author is told that in `grade.reasons` rather than being
    shown a key that looks scored.
    """
    grading = {
        "combine_method": "weighted",
        "weights": {"state_checks": 1.0},
        "pass_threshold": 0.7,
        "state_checks": {
            "hash_enabled": False,
            "expected_hash": "deadbeef",
            "jsonpath_checks": [_JSONPATH_CHECK],
        },
    }

    response = _grade(runner_service, mock_grpc_context, "ledger_core_only_hash:0", grading)

    assert response.success is True, response.error
    assert response.grade.score == pytest.approx(1.0)
    assert (
        "state_checks.hash.expected_state_hash skipped: core-only — no runner path "
        "reads it (#693)" in response.grade.reasons
    )


def test_custom_checks_pack_grades_instead_of_erroring(runner_service, mock_grpc_context):
    """A pack whose only scored key is `custom_checks` gets a grade, not an audit failure."""
    grading = {
        "combine_method": "weighted",
        "weights": {"custom_checks": 1.0},
        "pass_threshold": 0.7,
        "custom_checks": {"enabled": True, "file": "checks.py", "interface_version": "1.0"},
    }

    response = _grade(
        runner_service,
        mock_grpc_context,
        "ledger_custom_checks:0",
        grading,
        tool_artifacts=_checks_artifacts(),
    )

    assert response.success is True, response.error
    assert response.grade.components.custom_checks == pytest.approx(1.0)
    assert response.grade.score == pytest.approx(1.0)


def test_custom_checks_written_but_disabled_records_the_skip(runner_service, mock_grpc_context):
    """`enabled: false` still populates the key, so the runner says it scored nothing."""
    grading = {
        "combine_method": "weighted",
        "weights": {"state_checks": 1.0},
        "pass_threshold": 0.7,
        "state_checks": {"jsonpath_checks": [_JSONPATH_CHECK]},
        "custom_checks": {"enabled": False, "file": "checks.py"},
    }

    response = _grade(runner_service, mock_grpc_context, "ledger_custom_checks_off:0", grading)

    assert response.success is True, response.error
    assert response.grade.score == pytest.approx(1.0)
    assert "custom_checks skipped: custom checks not enabled" in response.grade.reasons


_TRACE_CHECKS_GRADING: dict[str, Any] = {
    "combine_method": "weighted",
    "weights": {"trace_checks": 1.0},
    "pass_threshold": 0.7,
    "trace_checks": {
        "constraints": [
            {
                "id": "said_the_widget_shipped",
                "description": "the agent reports the shipment and leaks no credential",
                "require": {
                    "all_of": [
                        {
                            "present": {
                                "match": {
                                    "kind": "assistant_message",
                                    "text": {"contains": "shipped"},
                                }
                            }
                        },
                        {
                            "absent": {
                                "match": {
                                    "kind": "assistant_message",
                                    "text": {"contains": "password"},
                                }
                            }
                        },
                    ]
                },
            }
        ]
    },
}


def test_a_trace_checks_pack_grades_instead_of_erroring(runner_service, mock_grpc_context):
    """A pack whose only scored key is `trace_checks` gets a grade through GradeTrial."""
    response = _grade(
        runner_service,
        mock_grpc_context,
        "ledger_trace_checks:0",
        _TRACE_CHECKS_GRADING,
        llm_messages=[{"role": "assistant", "content": "The widget was shipped"}],
    )

    assert response.success is True, response.error
    assert response.grade.components.trace_checks == pytest.approx(1.0)
    assert response.grade.score == pytest.approx(1.0)
    assert [(item.id, item.passed) for item in response.grade.trace_checks] == [
        ("said_the_widget_shipped", True)
    ]


def test_a_degenerate_trial_leaves_trace_checks_unscored(runner_service, mock_grpc_context):
    """No messages and no tool history: the component is not scored, and the trial fails.

    Scoring it would grade constraints against evidence the trial does not carry,
    and passing it would let the one component the pack weights decide nothing.
    """
    response = _grade(
        runner_service, mock_grpc_context, "ledger_trace_degenerate:0", _TRACE_CHECKS_GRADING
    )

    assert response.success is True, response.error
    assert response.grade.components.trace_checks == -1.0
    assert list(response.grade.trace_checks) == []
    assert response.grade.binary_pass is False
    # The skip is asserted together with the unscored component: a component the
    # runner silently declined to score is as opaque to the task author as one it
    # never accounted for.
    assert (
        f"{TRACE_CONSTRAINTS_KEY} skipped: the trial's timeline carries no events"
        in response.grade.reasons
    )
    assert (
        f"{TRACE_CONSTRAINT_KEY_BY_KIND['all_of']} skipped: the trial's timeline carries no "
        "events" in response.grade.reasons
    )


@pytest.mark.parametrize(
    ("evaluator_name", "grading", "unaccounted_key", "message"),
    [
        (
            "evaluate_transcript_rules",
            {
                "combine_method": "weighted",
                "weights": {"transcript_rules": 1.0},
                "pass_threshold": 0.7,
                "transcript_rules": {"must_contain": ["done"]},
            },
            "transcript_rules.must_contain",
            [{"role": "assistant", "content": "All done"}],
        ),
        (
            "evaluate_trace_checks",
            _TRACE_CHECKS_GRADING,
            TRACE_CONSTRAINT_KEY_BY_KIND["all_of"],
            [{"role": "assistant", "content": "The widget was shipped"}],
        ),
    ],
)
def test_grade_trial_fails_loud_when_an_evaluator_stops_decomposing_a_key(
    evaluator_name,
    grading,
    unaccounted_key,
    message,
    runner_service,
    mock_grpc_context,
    monkeypatch,
):
    """Fault injection: the drift the ledger exists to catch, end to end.

    The real evaluator still runs and still scores; only its per-author-key
    accounting is dropped — what a future ``RunnerTranscriptRulesConfig`` key that
    nothing decomposes would look like on the wire. The trace-checks row is the
    leaf-granular case: the block key alone being accounted would leave a kind
    evaluated by neither substrate invisible, so the key the error must name is
    the kind's.
    """
    from tolokaforge.runner import service as service_module

    real = getattr(service_module, evaluator_name)

    def drifted(*args: Any, **kwargs: Any) -> Any:
        return real(*args, **kwargs).model_copy(update={"accounted_keys": {}})

    monkeypatch.setattr(service_module, evaluator_name, drifted)

    response = _grade(
        runner_service,
        mock_grpc_context,
        f"ledger_drift_{evaluator_name}:0",
        grading,
        llm_messages=message,
    )

    assert response.success is False
    assert unaccounted_key in response.error
    assert entry(unaccounted_key).runner_evaluator in response.error
    assert not response.HasField("grade")


def test_test_execution_dispatch_is_exempt_from_the_ledger(runner_service, mock_grpc_context):
    """`grading_method: test_execution` returns before the component phase.

    The manifest's ``grading_method`` entry declares that exemption; this pins that
    such a request fails on its missing exec tool, not on the ledger.
    """
    grading = {
        "grading_method": "test_execution",
        "combine_method": "weighted",
        "weights": {"state_checks": 1.0},
        "state_checks": {"jsonpath_checks": [_JSONPATH_CHECK]},
    }

    response = _grade(runner_service, mock_grpc_context, "ledger_test_exec:0", grading)

    assert response.success is False
    assert "neither evaluated nor recorded a skip" not in response.error
