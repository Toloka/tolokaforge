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
    MIN_ASSISTANT_TURNS_KEY,
    Enforcement,
    GradingKey,
    KeyKind,
    SubstrateCoverage,
    entry,
)
from tolokaforge.core.grading.trace_checks import evaluate_trace_checks
from tolokaforge.core.grading.trace_timeline import TrialTimeline
from tolokaforge.core.models import ToolCall
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.grading_ledger import (
    CUSTOM_CHECKS_DISABLED_SKIP,
    DB_PROBES_KEY,
    EVALUATED,
    HASH_DISABLED_SKIP,
    JSONPATHS_KEY,
    LEDGER_KEYS,
    TRACE_ALTERNATIVES_KEY,
    TRACE_CONSTRAINT_KEY_BY_KIND,
    TRACE_CONSTRAINTS_KEY,
    UNBOUND_BINDING_SKIP,
    accountable_author_keys,
    audit_accounted_keys,
    hash_family_accounting,
    hash_family_skip_accounting,
    populated_ledger_keys,
    reject_hash_members_the_hash_evaluator_does_not_read,
    runner_dump_path,
    skip_note,
    skip_note_prefix,
)
from tolokaforge.runner.models import (
    TRACE_CONSTRAINT_KINDS,
    HashComparisonBasis,
    KeyAccounting,
    KeyAccountingRecord,
    RunnerGradingConfig,
    RunnerStateChecksConfig,
    TraceChecksConfig,
    TraceConstraintKind,
    TranscriptRulesConfig,
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


def test_an_explicitly_empty_check_is_not_populated():
    """``disallowed_tools: []`` written out is indistinguishable from unset."""
    config = RunnerGradingConfig(
        transcript_rules=TranscriptRulesConfig(
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
    """The note is also exactly ``skip_note``'s rendering of the same record.

    The canonical guard rail decides a key was skipped by matching ``skip_note``'s
    output against ``grade.reasons``. An audit that rendered its own sentence
    would leave every such match silently unsatisfiable, so the second equality
    is what stops the two spellings from drifting apart while the first keeps the
    text itself pinned.
    """
    config = RunnerGradingConfig(state_checks=RunnerStateChecksConfig(expect_initial_state=True))

    audit = audit_accounted_keys(
        config, {"state_checks.hash.expect_initial_state": HASH_DISABLED_SKIP}
    )

    assert audit.error is None
    assert audit.skip_notes == (
        "state_checks.hash.expect_initial_state skipped: hash grading not enabled",
    )
    assert audit.skip_notes == (
        skip_note("state_checks.hash.expect_initial_state", HASH_DISABLED_SKIP),
    )


def test_a_skipped_record_must_say_why():
    """The detail is what a task author reads, so an empty one is not a skip."""
    with pytest.raises(ValueError, match="detail rendered into Grade.reasons"):
        KeyAccountingRecord(outcome=KeyAccounting.SKIPPED)


def test_the_whole_family_carries_one_skip_when_hash_grading_did_not_run():
    """A member left out would fail the RPC for a key the disabled flag never reached.

    Asserted as the whole mapping rather than member by member, so a family that grew a
    member the skip does not answer for is caught here rather than at the audit.
    """
    assert hash_family_skip_accounting(HASH_DISABLED_SKIP) == {
        "state_checks.hash": HASH_DISABLED_SKIP,
        "state_checks.hash.enabled": HASH_DISABLED_SKIP,
        "state_checks.hash.golden_actions": HASH_DISABLED_SKIP,
        "state_checks.hash.expect_initial_state": HASH_DISABLED_SKIP,
    }


@pytest.mark.parametrize(
    ("basis", "source_read"),
    [
        pytest.param(
            HashComparisonBasis.DECLARED_INITIAL_STATE,
            "state_checks.hash.expect_initial_state",
            id="declared_initial_state",
        ),
        pytest.param(
            HashComparisonBasis.GOLDEN_REPLAY,
            "state_checks.hash.golden_actions",
            id="golden_replay",
        ),
        pytest.param(
            HashComparisonBasis.UNDECLARED_INITIAL_STATE, None, id="undeclared_initial_state"
        ),
    ],
)
def test_only_the_source_that_selected_the_basis_is_accounted_as_evaluated(basis, source_read):
    """The reason the evaluator returns its basis rather than the site re-reading the config.

    A second read at the accounting site would file ``EVALUATED`` for whichever source
    the config populated, so an evaluator that stopped consulting the key would keep
    its ledger row and the read would be unobservable. Fed from the basis instead, a
    source it did not select is absent — and a populated key nothing filed fails the
    RPC, which is what makes the read falsifiable.

    Asserted as the whole mapping, so the *other* source being absent is part of the
    claim rather than something the row happens not to look at.
    """
    records = hash_family_accounting(basis)

    assert records == {
        "state_checks.hash": EVALUATED,
        "state_checks.hash.enabled": EVALUATED,
        **({source_read: EVALUATED} if source_read is not None else {}),
    }


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
    assert TRACE_ALTERNATIVES_KEY in accountable


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
    config = RunnerGradingConfig(trace_checks=TraceChecksConfig(**_NESTED_TRACE_BLOCK))
    accounted = evaluate_trace_checks(_inspected_then_shipped(), config.trace_checks).accounted_keys

    assert audit_accounted_keys(config, accounted).error is None

    # The other half, so the pass above is not the ledger looking at nothing: with
    # the accounting dropped the audit names the kinds the block does declare.
    starved = audit_accounted_keys(config, {})
    assert TRACE_CONSTRAINT_KEY_BY_KIND["before"] in starved.error
    assert TRACE_CONSTRAINT_KEY_BY_KIND["present"] not in starved.error


_ROUTED_TRACE_BLOCK: dict[str, Any] = {
    "alternatives": [
        {
            "id": "by_order",
            "description": "the widget was inspected before it shipped",
            "constraints": [
                {
                    "id": "inspected_first",
                    "description": "the inspection came before the shipment",
                    "require": {
                        "before": {
                            "left": {
                                "quantifier": "first",
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
                }
            ],
        },
        {
            "id": "by_restraint",
            "description": "the widget was inspected at most once",
            "constraints": [
                {
                    "id": "inspected_once",
                    "description": "no re-inspection",
                    "require": {
                        "count": {
                            "match": {"kind": "tool_call", "tool": {"equals": "inspect_widget"}},
                            "max": 1,
                        }
                    },
                }
            ],
        },
    ]
}

_ROUTED_TRACE_KEYS = {
    "trace_checks.constraints",
    "trace_checks.alternatives",
    "trace_checks.constraints.before",
    "trace_checks.constraints.count",
}


def test_a_kind_declared_only_inside_a_route_is_accounted_for_like_a_shared_one():
    """The walk reaches a path's constraints, and ``alternatives`` is a key of its own.

    The block declares no shared constraints at all, so an account read off
    ``constraints`` alone records nothing the pack actually asserts — and
    ``GradeTrial`` then rejects every trial the pack grades.
    """
    config = RunnerGradingConfig(trace_checks=TraceChecksConfig(**_ROUTED_TRACE_BLOCK))
    graded = evaluate_trace_checks(_inspected_then_shipped(), config.trace_checks)
    silent = evaluate_trace_checks(build_turn_timeline([]), config.trace_checks)

    assert set(graded.accounted_keys) == _ROUTED_TRACE_KEYS
    assert set(silent.accounted_keys) == _ROUTED_TRACE_KEYS
    assert audit_accounted_keys(config, graded.accounted_keys).error is None
    assert TRACE_ALTERNATIVES_KEY in audit_accounted_keys(config, {}).error


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


def _bound_before(constraint_id: str, binder_tool: str, **bind_fields: Any) -> dict[str, Any]:
    """A correlated ordering whose binder selects ``binder_tool`` and nothing else."""
    return {
        "id": constraint_id,
        "description": "every widget shipped was inspected first",
        "bind": {
            "match": {"kind": "tool_call", "tool": {"equals": binder_tool}},
            "values": {"widget": {"field": "args.widget_id"}},
            **bind_fields,
        },
        "require": {
            "before": {
                "left": {
                    "quantifier": "any",
                    "match": {
                        "kind": "tool_call",
                        "tool": {"equals": "inspect_widget"},
                        "args": {"widget_id": {"equals_binding": "widget"}},
                    },
                },
                "right": {
                    "quantifier": "any",
                    "match": {
                        "kind": "tool_call",
                        "tool": {"equals": binder_tool},
                        "args": {"widget_id": {"equals_binding": "widget"}},
                    },
                },
            }
        },
    }


def _inspected_then_shipped_widget_w1() -> TrialTimeline:
    """The same trajectory, with the widget named on both calls so a binder can read it."""
    return build_turn_timeline(
        [
            Turn("user", "Ship widget w1 once you have checked it."),
            Turn(
                "assistant",
                "Inspecting it.",
                recorded=[
                    recorded_call("inspect_widget", sequence=0, arguments={"widget_id": "w1"})
                ],
            ),
            Turn(
                "assistant",
                "Shipping it.",
                recorded=[recorded_call("ship_widget", sequence=1, arguments={"widget_id": "w1"})],
            ),
        ]
    )


def _bound_composite(constraint_id: str, binder_tool: str, **bind_fields: Any) -> dict[str, Any]:
    """``_bound_before``'s correlation nested under a composite, beside a second kind.

    The nesting separates the two accounting outcomes a zero-candidate binder
    produces: the composite is the kind the constraint's verdict is filed under, and
    ``before`` and ``present`` are the kinds no evaluation reached.
    """
    constraint = _bound_before(constraint_id, binder_tool, **bind_fields)
    constraint["require"] = {
        "all_of": [
            constraint["require"],
            {"present": {"match": {"kind": "tool_call", "tool": {"equals": "ship_widget"}}}},
        ]
    }
    return constraint


def test_a_constraint_whose_binder_selected_nothing_accounts_its_nested_kinds_as_skipped():
    """A kind reaches the ledger by carrying a verdict, and only the outer one does.

    The constraint scores under its own kind whatever the binder yielded, so filing
    that kind as skipped would report it as unevaluated in the same grade that fails
    the trial on it. The kinds *under* the require tree are the ones nothing reached:
    left unaccounted the RPC fails on a key the config populates, and filed as
    ``EVALUATED`` the grade claims an evaluation that never happened. The skip is the
    honest record, and it is subtracted away by a sibling constraint of the same kind
    that *did* evaluate — otherwise the grade reports a kind as skipped while another
    constraint scored it.

    ``on_unbound`` decides the verdict and nothing about the accounting: the require
    tree went unevaluated under either policy, so both file the same skips. The audit
    is read for its skip notes rather than only for ``error``, because a
    non-``EVALUATED`` record is routed to ``skip_notes`` — the audit reports no error
    over a skip filed with the wrong outcome, so ``error is None`` alone would hold
    whatever this evaluation recorded.
    """
    timeline = _inspected_then_shipped_widget_w1()
    for policy, verdict in (({}, False), ({"on_unbound": "pass"}, True)):
        config = RunnerGradingConfig(
            trace_checks=TraceChecksConfig(
                constraints=[_bound_composite("no_binder_fires", "recall_widget", **policy)]
            )
        )
        result = evaluate_trace_checks(timeline, config.trace_checks)
        accounted = result.accounted_keys
        audit = audit_accounted_keys(config, accounted)

        assert audit.error is None, policy
        assert set(audit.skip_notes) == {
            skip_note(TRACE_CONSTRAINT_KEY_BY_KIND[kind], UNBOUND_BINDING_SKIP)
            for kind in ("before", "present")
        }, policy
        assert accounted[TRACE_CONSTRAINT_KEY_BY_KIND["before"]] == UNBOUND_BINDING_SKIP, policy
        assert accounted[TRACE_CONSTRAINT_KEY_BY_KIND["present"]] == UNBOUND_BINDING_SKIP, policy
        assert accounted[TRACE_CONSTRAINT_KEY_BY_KIND["all_of"]] == EVALUATED, policy
        assert accounted[TRACE_CONSTRAINTS_KEY] == EVALUATED, policy
        assert result.constraints[0].kind is TraceConstraintKind.ALL_OF, policy
        assert result.constraints[0].passed is verdict, policy

    beside_an_evaluated_sibling = RunnerGradingConfig(
        trace_checks=TraceChecksConfig(
            constraints=[
                _bound_composite("no_binder_fires", "recall_widget"),
                _bound_before("the_binder_fires", "ship_widget"),
            ]
        )
    )
    both = evaluate_trace_checks(timeline, beside_an_evaluated_sibling.trace_checks).accounted_keys

    assert audit_accounted_keys(beside_an_evaluated_sibling, both).error is None
    assert both[TRACE_CONSTRAINT_KEY_BY_KIND["before"]] == EVALUATED
    assert both[TRACE_CONSTRAINT_KEY_BY_KIND["present"]] == UNBOUND_BINDING_SKIP


def test_a_binder_whose_value_the_trial_never_recorded_accounts_its_nested_kinds_as_skipped():
    """The other way a ``require`` tree goes unentered: no assignment to enter it under.

    The binder selects the call and the trial records no outcome for it, so the value
    it extracts is unreadable rather than absent — a candidate with no name. There is
    nothing to evaluate the tree under, so its nested kinds reach the ledger no more
    than an unbound binder's do, and left unaccounted the RPC fails on a key the
    config populates. The composite still carries the constraint's own verdict, which
    is a failing sub-check rather than a silent resolution.
    """
    config = RunnerGradingConfig(
        trace_checks=TraceChecksConfig(
            constraints=[
                {
                    "id": "quoted_only_what_a_passing_inspection_returned",
                    "description": (
                        "every grade the agent quoted came out of an inspection, and no "
                        "inspection failed"
                    ),
                    "bind": {
                        "match": {
                            "kind": "tool_call",
                            "tool": {"equals": "inspect_widget"},
                            "status": {"equals": "success"},
                        },
                        "values": {"grade": {"field": "result"}},
                    },
                    "require": {
                        "all_of": [
                            {
                                "present": {
                                    "match": {
                                        "kind": "assistant_message",
                                        "text": {"contains_binding": "grade"},
                                    }
                                }
                            },
                            {
                                "absent": {
                                    "match": {
                                        "kind": "tool_call",
                                        "tool": {"equals": "inspect_widget"},
                                        "status": {"equals": "error"},
                                    }
                                }
                            },
                        ]
                    },
                }
            ]
        )
    )
    timeline = build_turn_timeline(
        [
            Turn("user", "Inspect widget w1 and tell me its grade."),
            Turn(
                "assistant",
                "Widget w1 is grade A.",
                unexecuted=[ToolCall(id="never_ran", name="inspect_widget", arguments={})],
            ),
        ]
    )

    result = evaluate_trace_checks(timeline, config.trace_checks)
    audit = audit_accounted_keys(config, result.accounted_keys)

    assert timeline.records_present is False
    assert audit.error is None
    assert set(audit.skip_notes) == {
        skip_note(TRACE_CONSTRAINT_KEY_BY_KIND[kind], UNBOUND_BINDING_SKIP)
        for kind in ("present", "absent")
    }
    assert result.accounted_keys[TRACE_CONSTRAINT_KEY_BY_KIND["present"]] == UNBOUND_BINDING_SKIP
    assert result.accounted_keys[TRACE_CONSTRAINT_KEY_BY_KIND["absent"]] == UNBOUND_BINDING_SKIP
    assert result.accounted_keys[TRACE_CONSTRAINT_KEY_BY_KIND["all_of"]] == EVALUATED
    assert result.constraints[0].kind is TraceConstraintKind.ALL_OF
    assert result.constraints[0].passed is False
    assert "cannot be decided" in result.constraints[0].message


# --------------------------------------------------------------------------
# The predicates the canonical guard rail reads
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "record",
    [HASH_DISABLED_SKIP, CUSTOM_CHECKS_DISABLED_SKIP],
    ids=["hash_disabled", "custom_checks_disabled"],
)
def test_a_skip_note_starts_with_its_key_prefix_whatever_the_detail(record):
    """A caller expecting a key to be *evaluated* asserts the prefix is absent.

    It knows the key and cannot know what detail a hypothetical skip would carry,
    so the prefix is the whole needle it has. Both halves are needed: a prefix
    that were not literally how the note starts would let that assertion pass
    over a key that was in fact skipped, and one that did not name the key would
    make it fail over a sibling key's skip.
    """
    assert skip_note(JSONPATHS_KEY, record).startswith(skip_note_prefix(JSONPATHS_KEY))
    assert not skip_note(DB_PROBES_KEY, record).startswith(skip_note_prefix(JSONPATHS_KEY))


def test_an_evaluated_record_has_no_skip_note_to_render():
    """Rendering one would let a caller match a sentence the audit never emits."""
    with pytest.raises(ValueError, match="an EVALUATED record has no skip note"):
        skip_note(JSONPATHS_KEY, EVALUATED)


_PARTIALLY_POPULATED_KEYS = frozenset(
    {
        "custom_checks",
        "state_checks.jsonpaths",
        "trace_checks.constraints",
        "trace_checks.constraints.all_of",
        "trace_checks.constraints.before",
        "trace_checks.constraints.count",
        "transcript_rules.must_contain",
    }
)


def _partially_populated_config() -> RunnerGradingConfig:
    """A config populating :data:`_PARTIALLY_POPULATED_KEYS` and no other ledger key."""
    return RunnerGradingConfig(
        state_checks=RunnerStateChecksConfig(jsonpath_checks=[_JSONPATH_CHECK]),
        transcript_rules=TranscriptRulesConfig(must_contain=["shipped"]),
        trace_checks=TraceChecksConfig(**_NESTED_TRACE_BLOCK),
        custom_checks={"enabled": True, "file": "checks.py", "interface_version": "1.0"},
    )


def test_populated_ledger_keys_agrees_with_the_audit_on_what_is_populated():
    """Two answers to "did this config populate the key" must not diverge.

    The canonical guard rail asserts that a driver populated the key it claims to
    drive; on a predicate that had drifted from the one the audit visits keys by,
    it would vouch for a key the audit never looked at. Starving the audit of
    records makes it name every key it visited, which is the only observation of
    its own predicate it exposes.

    ``state_checks.hash`` is absent because it names no ``runner_field``:
    resolving one raises, so the comprehension over :data:`LEDGER_KEYS` written
    without the audit's guard would fail on every call rather than return a set.
    """
    config = _partially_populated_config()

    populated = populated_ledger_keys(config)
    starved = audit_accounted_keys(config, {})

    assert populated == _PARTIALLY_POPULATED_KEYS
    assert "state_checks.hash" not in populated
    domain = frozenset(item.author_key for item in LEDGER_KEYS if item.runner_field is not None)
    assert frozenset(key for key in domain if f"{key} (" in starved.error) == populated
    assert domain - populated, "the config must leave ledger keys unpopulated to discriminate"


# --------------------------------------------------------------------------
# runner_field resolution — a malformed manifest entry fails loud
# --------------------------------------------------------------------------


def _probe_key(
    runner_field: str,
    runner_evaluator: str | None = None,
) -> GradingKey:
    return GradingKey(
        author_key="probe.key",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.RUNNER_ONLY,
        enforcement=Enforcement.FIELD_RESOLUTION_ONLY,
        core_field=None,
        runner_field=runner_field,
        runner_evaluator=runner_evaluator,
        reason="a probe entry built by this test",
    )


def test_runner_field_naming_an_unknown_model_fails_loud():
    with pytest.raises(ValueError, match="not part of the runner grading config"):
        runner_dump_path(_probe_key("GradingCombineConfig.method"))


def test_runner_field_naming_an_unknown_field_fails_loud():
    with pytest.raises(ValueError, match="has no field 'jsonpath_chekcs'"):
        runner_dump_path(_probe_key("RunnerStateChecksConfig.jsonpath_chekcs"))


@pytest.mark.parametrize(
    "runner_evaluator",
    [
        "tolokaforge.runner.grading.evaluate_golden_action_traces",
        None,
    ],
    ids=["another_evaluator_reads_it", "nothing_on_this_substrate_reads_it"],
)
def test_a_hash_family_member_the_hash_evaluator_does_not_read_fails_loud(runner_evaluator):
    """The family is reported from one returned basis, so any other member needs its own site.

    The ``None`` row is the core-only shape: a scored key the manifest gives no runner
    evaluator while a runner field still carries it across the wire. Reporting it from the
    hash evaluator's basis would call it evaluated on a substrate that never read it.
    """
    member = _probe_key("RunnerStateChecksConfig.golden_actions", runner_evaluator)

    with pytest.raises(ValueError, match="needs its own recording site"):
        reject_hash_members_the_hash_evaluator_does_not_read([member])


# --------------------------------------------------------------------------
# GradeTrial: the config shapes shipping today still grade
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("id_fields", "trial_id"),
    [
        ({"widgets": "widget_id"}, "ledger_id_fields:0"),
        ({"widgets": ["widget_id", "status"]}, "ledger_id_fields_composite:0"),
    ],
    ids=["a_single_field", "a_composite_key"],
)
def test_id_fields_shaped_config_grades_instead_of_erroring(
    runner_service, mock_grpc_context, id_fields, trial_id
):
    """The config-input false-positive class: every `id_fields` pack shipping today.

    Both value forms are `CONFIG_INPUT` — the key shapes write resolution instead of
    producing a component score, so it takes no ledger record in either form.
    """
    grading = {
        "combine_method": "weighted",
        "weights": {"state_checks": 1.0, "transcript_rules": 1.0},
        "pass_threshold": 0.7,
        "state_checks": {
            "numeric_string_fields": ["amount"],
            "id_fields": id_fields,
            "relaxed_validation": True,
            "jsonpath_checks": [_JSONPATH_CHECK],
        },
        "transcript_rules": {"must_contain": ["done"]},
    }

    response = _grade(
        runner_service,
        mock_grpc_context,
        trial_id,
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
                    "name": "ship_widget",
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


def test_degenerate_trial_refuses_when_the_declared_transcript_rules_did_not_score(
    runner_service, mock_grpc_context
):
    """No messages and no tool history leaves ``transcript_rules`` at the ``-1.0``
    sentinel while the config declared it and weighted it. The fold refuses:
    folding on nothing would surface a bare ``0.0`` on a component that never
    produced a verdict. ``GradeTrial`` returns ``success=False`` with an error
    naming the missing component so the trial lands ungradeable.
    """
    grading = {
        "combine_method": "weighted",
        "weights": {"transcript_rules": 1.0},
        "pass_threshold": 0.7,
        "transcript_rules": {"must_contain": ["done"]},
    }

    response = _grade(runner_service, mock_grpc_context, "ledger_degenerate:0", grading)

    assert response.success is False, response.error
    assert "transcript_rules" in response.error, response.error


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
    assert skip_note_prefix(MIN_ASSISTANT_TURNS_KEY) not in response.grade.reasons
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


def test_a_degenerate_trial_refuses_when_the_declared_trace_checks_did_not_score(
    runner_service, mock_grpc_context
):
    """No messages and no tool history leaves ``trace_checks`` at the ``-1.0``
    sentinel while the pack declared and weighted it. Scoring against evidence
    the trial does not carry would fabricate a verdict, and folding on nothing
    else would let a declared component decide nothing. ``GradeTrial`` refuses
    with an error naming the missing component so the trial lands ungradeable.
    """
    response = _grade(
        runner_service, mock_grpc_context, "ledger_trace_degenerate:0", _TRACE_CHECKS_GRADING
    )

    assert response.success is False, response.error
    assert "trace_checks" in response.error, response.error


@pytest.mark.parametrize(
    ("evaluator_module", "evaluator_name", "grading", "unaccounted_key", "message"),
    [
        (
            "tolokaforge.core.grading.default_transcript_rule_matcher",
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
            "tolokaforge.core.grading.composite",
            "evaluate_trace_checks",
            _TRACE_CHECKS_GRADING,
            TRACE_CONSTRAINT_KEY_BY_KIND["all_of"],
            [{"role": "assistant", "content": "The widget was shipped"}],
        ),
    ],
)
def test_grade_trial_fails_loud_when_an_evaluator_stops_decomposing_a_key(
    evaluator_module,
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
    accounting is dropped — what a future ``TranscriptRulesConfig`` key that
    nothing decomposes would look like on the wire. The trace-checks row is the
    leaf-granular case: the block key alone being accounted would leave a kind
    evaluated by neither substrate invisible, so the key the error must name is
    the kind's.

    ``evaluator_module`` names the module the drift is injected on: composite
    for :func:`evaluate_trace_checks` (still imported directly at composite
    load time), and the default matcher module for :func:`evaluate_transcript_rules`
    (reached through the :class:`TranscriptRuleMatcher` seam).
    """
    import importlib

    module = importlib.import_module(evaluator_module)
    real = getattr(module, evaluator_name)

    def drifted(*args: Any, **kwargs: Any) -> Any:
        return real(*args, **kwargs).model_copy(update={"accounted_keys": {}})

    monkeypatch.setattr(module, evaluator_name, drifted)

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
