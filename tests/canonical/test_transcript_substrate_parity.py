"""Differential lock: one ``transcript_rules`` pack, two substrates, one score.

Twenty-two rows, each an authored pack under ``tests/data/transcript_parity/`` plus
the trial's two views of itself — its messages and its tool-call record. Every row
is graded through **both** substrates' real paths:

- core: ``NativeAdapter.get_grading_config`` → ``GradingEngine.grade_trajectory``
  → ``grade.components.transcript_rules``;
- runner: ``NativeAdapter.to_task_description`` → ``evaluate_transcript_rules``.

Driving through the adapter is what keeps the rows readable as authored packs
rather than as either substrate's internal config model.

Twenty of the rows sit on the nine scoring questions a transcript rule has to
answer the same way on either substrate — how the sub-checks aggregate, what
denominator they aggregate over, which events a phrase rule reads, whether a
phrase matches case-insensitively, whether it matches as the phrase or as a bag of
its words, whether a call's status counts, whose call it was, whether a veto beats
a fraction, and what a missing tool-call record proves. The rows are pinned at
three different scores and the two **anchor** rows sit on no question at all, so a
harness that drove one substrate twice, or returned a constant, fails some of them.

A tenth question is not a row, because its answer is that neither substrate
produces a component at all: what an events-less trial scores is a property of the
fold rather than of one pack's verdict, so it is locked by the two named tests
below, which drive the core engine's ``grade_trajectory`` against the runner's own
``GradeTrial`` RPC.

The runner's column is checked first and separately: a table pinning a value the
runner does not produce raises :class:`_FixtureDefect` rather than reading as the
substrates disagreeing.

The fixture root is separate from ``grading_parity`` on purpose: that corpus's
packs each declare exactly one author key, and several rows here declare two so a
veto can be told apart from a fraction.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

import pytest

from tests.utils.runner_requests import register_request, trial_spec_json
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core import models as core_models
from tolokaforge.core.grading.combine import GradingEngine
from tolokaforge.core.grading.trace_timeline import build_trial_timeline
from tolokaforge.core.grading.transcript import evaluate_transcript_rules
from tolokaforge.core.models import (
    Message,
    MessageRole,
    RecordedToolCall,
    ToolCall,
    ToolExecutionStatus,
    ToolExecutorIdentity,
    Trajectory,
)
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.service import RunnerServiceImpl

pytestmark = pytest.mark.canonical

_PARITY_GLOB = "transcript_parity/**/task.yaml"
_FIXTURE_TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _ScoringQuestion(str, Enum):
    """The nine scoring questions both substrates have to answer alike."""

    AGGREGATION = "aggregation"
    DENOMINATOR = "denominator"
    EVIDENCE_SET = "evidence set"
    CASE = "case"
    MATCHING_MODE = "matching mode"
    CALL_STATUS = "call status"
    CALL_EXECUTOR = "call executor"
    VETO_VS_FRACTION = "veto vs fraction"
    RECORD_ABSENT = "record-absent evidence"


def _user(text: str, *calls: ToolCall) -> Message:
    return Message(role=MessageRole.USER, content=text, tool_calls=list(calls) or None)


def _assistant(text: str, *calls: ToolCall) -> Message:
    return Message(role=MessageRole.ASSISTANT, content=text, tool_calls=list(calls) or None)


def _record_for(
    call: ToolCall,
    status: ToolExecutionStatus,
    *,
    output: str = "",
    executor: ToolExecutorIdentity = ToolExecutorIdentity.AGENT,
) -> RecordedToolCall:
    """The record a substrate keeps of ``call`` once it ran.

    ``sequence`` is pinned at 0 because no row declares more than one call; a
    second one would want its own, and ordering within a turn follows it.
    """
    return RecordedToolCall(
        call_id=call.id,
        sequence=0,
        tool_name=call.name,
        arguments=call.arguments,
        executor=executor,
        output=output,
        status=status,
        latency_seconds=0.0,
        timestamp=_FIXTURE_TIMESTAMP,
    )


_REFUND_ARGS = {"order_id": "O-001"}

_LOOKUP_ORDER = ToolCall(id="call_0", name="lookup_order", arguments=_REFUND_ARGS)
_ISSUE_REFUND = ToolCall(id="call_0", name="issue_refund", arguments=_REFUND_ARGS)
_WRITE_FILE = ToolCall(id="call_0", name="write_file", arguments={"path": "report.txt"})
_DELETE_CUSTOMER = ToolCall(id="call_0", name="delete_customer", arguments={"customer_id": "C1"})


@dataclass(frozen=True)
class _Row:
    """One pack graded over one trial, and the component score both substrates owe.

    ``tool_log`` is stated per row rather than derived from the messages: whether
    the trial kept a record of the calls it declared, and which actor that record
    names, are two of the nine questions.
    """

    row_id: str
    pack: str
    messages: tuple[Message, ...]
    tool_log: tuple[RecordedToolCall, ...]
    expected: float
    question: _ScoringQuestion | None


_ROWS: tuple[_Row, ...] = (
    _Row(
        row_id="must_contain_absent",
        pack="must_contain_one",
        messages=(
            _user("Please refund my order."),
            _assistant("I closed the ticket without taking action."),
        ),
        tool_log=(),
        expected=0.0,
        question=_ScoringQuestion.AGGREGATION,
    ),
    _Row(
        row_id="disallow_regex_matched",
        pack="disallow_regex_one",
        messages=(
            _user("Please refund my order."),
            _assistant("I will cancel the order instead."),
        ),
        tool_log=(),
        expected=0.0,
        question=_ScoringQuestion.AGGREGATION,
    ),
    _Row(
        row_id="max_turns_exceeded",
        pack="max_turns_one",
        messages=(
            _user("Please refund my order."),
            _assistant("Looking into it."),
            _assistant("Done."),
        ),
        tool_log=(),
        expected=0.0,
        question=_ScoringQuestion.AGGREGATION,
    ),
    _Row(
        row_id="disallowed_tool_called",
        pack="disallowed_tools_one",
        messages=(
            _user("Please refund my order."),
            _assistant("Removing the customer record.", _DELETE_CUSTOMER),
        ),
        tool_log=(_record_for(_DELETE_CUSTOMER, ToolExecutionStatus.SUCCESS, output="deleted"),),
        expected=0.0,
        question=_ScoringQuestion.AGGREGATION,
    ),
    _Row(
        row_id="required_tool_never_called",
        pack="required_tools_one",
        messages=(
            _user("Please write the report."),
            _assistant("I decided not to write anything."),
        ),
        tool_log=(),
        expected=0.0,
        question=_ScoringQuestion.AGGREGATION,
    ),
    _Row(
        row_id="must_contain_one_of_two",
        pack="must_contain_two",
        messages=(
            _user("Please refund my order."),
            _assistant("Refund issued for O-001."),
        ),
        tool_log=(),
        expected=0.5,
        question=_ScoringQuestion.DENOMINATOR,
    ),
    _Row(
        row_id="disallow_regex_one_of_two_matched",
        pack="disallow_regex_two",
        messages=(
            _user("Please refund my order."),
            _assistant("I will cancel the order instead."),
        ),
        tool_log=(),
        expected=0.5,
        question=_ScoringQuestion.DENOMINATOR,
    ),
    _Row(
        row_id="must_contain_only_in_a_tool_result",
        pack="must_contain_one",
        messages=(
            _user("Please refund my order."),
            _assistant("Consulting the billing system.", _LOOKUP_ORDER),
        ),
        tool_log=(
            _record_for(
                _LOOKUP_ORDER,
                ToolExecutionStatus.SUCCESS,
                output="Refund issued on 2026-01-01",
            ),
        ),
        expected=0.0,
        question=_ScoringQuestion.EVIDENCE_SET,
    ),
    _Row(
        row_id="must_contain_only_in_a_user_turn",
        pack="must_contain_one",
        messages=(
            _user("Refund issued yesterday — can you confirm?"),
            _assistant("I have nothing to add."),
        ),
        tool_log=(),
        expected=0.0,
        question=_ScoringQuestion.EVIDENCE_SET,
    ),
    _Row(
        row_id="disallow_regex_only_in_a_tool_result",
        pack="disallow_regex_one",
        messages=(
            _user("Please refund my order."),
            _assistant("Consulting the billing system.", _LOOKUP_ORDER),
        ),
        tool_log=(
            _record_for(
                _LOOKUP_ORDER,
                ToolExecutionStatus.SUCCESS,
                output="order was cancelled by billing",
            ),
        ),
        expected=1.0,
        question=_ScoringQuestion.EVIDENCE_SET,
    ),
    _Row(
        row_id="disallow_regex_only_in_a_user_turn",
        pack="disallow_regex_one",
        messages=(
            _user("Should I cancel it?"),
            _assistant("I kept the order active."),
        ),
        tool_log=(),
        expected=1.0,
        question=_ScoringQuestion.EVIDENCE_SET,
    ),
    _Row(
        row_id="must_contain_differs_only_in_case",
        pack="must_contain_one",
        messages=(
            _user("Please refund my order."),
            _assistant("refund issued for O-001."),
        ),
        tool_log=(),
        expected=1.0,
        question=_ScoringQuestion.CASE,
    ),
    _Row(
        row_id="communicate_info_paraphrased_word_by_word",
        pack="communicate_info_and_must_contain",
        messages=(
            _user("Please refund my order."),
            # Every word of the required info, none of it as the phrase asks.
            _assistant("Ticket closed. Your 50 dollars refund has been processed."),
        ),
        tool_log=(),
        expected=0.5,
        question=_ScoringQuestion.MATCHING_MODE,
    ),
    _Row(
        row_id="required_tool_call_errored",
        pack="required_tools_one",
        messages=(
            _user("Please write the report."),
            _assistant("Writing the report.", _WRITE_FILE),
        ),
        tool_log=(_record_for(_WRITE_FILE, ToolExecutionStatus.ERROR, output="disk full"),),
        expected=0.0,
        question=_ScoringQuestion.CALL_STATUS,
    ),
    _Row(
        row_id="disallowed_tool_call_errored",
        pack="disallowed_tools_one",
        messages=(
            _user("Please refund my order."),
            _assistant("Removing the customer record.", _DELETE_CUSTOMER),
        ),
        tool_log=(_record_for(_DELETE_CUSTOMER, ToolExecutionStatus.ERROR, output="denied"),),
        expected=0.0,
        question=_ScoringQuestion.CALL_STATUS,
    ),
    _Row(
        row_id="user_call_does_not_satisfy_required_tool",
        pack="required_tools_one",
        messages=(
            _user("I wrote the report myself.", _WRITE_FILE),
            _assistant("Then there is nothing left for me to write."),
        ),
        tool_log=(
            _record_for(
                _WRITE_FILE,
                ToolExecutionStatus.SUCCESS,
                output="written",
                executor=ToolExecutorIdentity.USER,
            ),
        ),
        expected=0.0,
        question=_ScoringQuestion.CALL_EXECUTOR,
    ),
    _Row(
        row_id="user_call_does_not_violate_disallowed_tool",
        pack="disallowed_tools_one",
        messages=(
            _user("I removed the customer record myself.", _DELETE_CUSTOMER),
            _assistant("Understood — I will leave the record alone."),
        ),
        tool_log=(
            _record_for(
                _DELETE_CUSTOMER,
                ToolExecutionStatus.SUCCESS,
                output="deleted",
                executor=ToolExecutorIdentity.USER,
            ),
        ),
        expected=1.0,
        question=_ScoringQuestion.CALL_EXECUTOR,
    ),
    _Row(
        row_id="communicate_info_missing_beside_a_found_phrase",
        pack="communicate_info_and_must_contain",
        messages=(
            _user("Please refund my order."),
            _assistant("Ticket closed for O-001."),
        ),
        tool_log=(),
        expected=0.5,
        question=_ScoringQuestion.VETO_VS_FRACTION,
    ),
    _Row(
        row_id="required_action_missing_beside_a_found_phrase",
        pack="required_actions_and_must_contain",
        messages=(
            _user("Please refund my order."),
            _assistant("Ticket closed for O-001."),
        ),
        tool_log=(),
        expected=0.5,
        question=_ScoringQuestion.VETO_VS_FRACTION,
    ),
    _Row(
        row_id="required_action_declared_with_no_record",
        pack="required_actions_one",
        messages=(
            _user("Please refund my order."),
            _assistant("Issuing the refund.", _ISSUE_REFUND),
        ),
        tool_log=(),
        expected=0.0,
        question=_ScoringQuestion.RECORD_ABSENT,
    ),
    _Row(
        row_id="anchor_required_action_never_declared",
        pack="required_actions_one",
        messages=(
            _user("Please refund my order."),
            _assistant("I did nothing."),
        ),
        tool_log=(),
        expected=0.0,
        question=None,
    ),
    _Row(
        row_id="anchor_optional_communicate_info_absent",
        pack="communicate_info_optional",
        messages=(
            _user("Please refund my order."),
            _assistant("I closed the ticket."),
        ),
        tool_log=(),
        expected=1.0,
        question=None,
    ),
)


class _FixtureDefect(Exception):
    """The row cannot measure what it claims, so its verdict is about the fixture.

    A pack that declares no ``transcript_rules`` block, or a pinned column the
    runner does not produce, is the table being wrong rather than the two
    substrates disagreeing — raised rather than asserted so the report says which.
    """


@pytest.fixture(scope="module")
def parity_adapter(test_data_dir: Path) -> NativeAdapter:
    return NativeAdapter({"base_dir": str(test_data_dir), "tasks_glob": _PARITY_GLOB})


def _core_component(adapter: NativeAdapter, row: _Row, task_dir: Path) -> float:
    """``transcript_rules`` as the core engine's real grade reports it."""
    trajectory = Trajectory(
        task_id=row.pack,
        trial_index=0,
        start_ts=_FIXTURE_TIMESTAMP,
        end_ts=_FIXTURE_TIMESTAMP,
        messages=list(row.messages),
        tool_log=list(row.tool_log),
    )
    config: core_models.GradingConfig = adapter.get_grading_config(row.pack)
    grade = GradingEngine(config, task_dir=task_dir).grade_trajectory(trajectory, {})
    component = grade.components.transcript_rules
    if component is None:
        raise _FixtureDefect(
            f"row {row.row_id!r} produced no core transcript_rules component, so the "
            f"pack {row.pack!r} declares no transcript_rules block and the row grades nothing"
        )
    return component


def _runner_component(adapter: NativeAdapter, row: _Row) -> float:
    """``transcript_rules`` as the runner's real evaluator scores it."""
    timeline = build_trial_timeline(list(row.messages), list(row.tool_log), None)
    grading = adapter.to_task_description(row.pack).grading
    return evaluate_transcript_rules(timeline, grading.transcript_rules).score


@pytest.mark.parametrize("row", [pytest.param(row, id=row.row_id) for row in _ROWS])
def test_both_substrates_score_one_transcript_rules_pack_alike(
    row: _Row, parity_adapter: NativeAdapter, tmp_path: Path
):
    """One authored pack and one trial score the same on both substrates.

    The pinned value is the runner's own verdict, which is the semantics the
    shared evaluator carries, so a row is satisfied only by the core engine
    reaching it too.
    """
    runner_score = _runner_component(parity_adapter, row)
    if runner_score != row.expected:
        raise _FixtureDefect(
            f"row {row.row_id!r} pins {row.expected} but the runner scores {runner_score}. "
            "The pinned column is the runner's own verdict, so this is the table being "
            "wrong about the substrate it copied, not the two substrates disagreeing"
        )

    core_score = _core_component(parity_adapter, row, tmp_path)
    assert core_score == runner_score == row.expected, (
        f"row {row.row_id!r}: the core engine scores transcript_rules {core_score} where "
        f"the runner scores {runner_score}"
    )


def test_every_scoring_question_is_measured_by_a_row():
    """Each of the nine questions keeps a row that answers it.

    ``_ScoringQuestion`` is declared independently of the table, so a row dropped
    from ``_ROWS`` leaves the question it carried with nothing measuring it.
    """
    measured = {row.question for row in _ROWS if row.question is not None}
    assert measured == set(_ScoringQuestion)


#: The value the wire carries for a component no substrate scored. ``GradeComponents``
#: is a proto3 message of bare doubles, so the runner spells "unevaluated" as a
#: sentinel where the core engine spells it ``None``.
_UNSCORED_ON_THE_WIRE = -1.0


def _core_eventless_grade(adapter: NativeAdapter, pack: str, task_dir: Path) -> core_models.Grade:
    """The core engine's grade for a trial with neither a message nor a record."""
    trajectory = Trajectory(
        task_id=pack,
        trial_index=0,
        start_ts=_FIXTURE_TIMESTAMP,
        end_ts=_FIXTURE_TIMESTAMP,
        messages=[],
        tool_log=[],
    )
    config: core_models.GradingConfig = adapter.get_grading_config(pack)
    return GradingEngine(config, task_dir=task_dir).grade_trajectory(trajectory, {})


def _runner_eventless_grade(
    adapter: NativeAdapter, pack: str, servicer: RunnerServiceImpl, context: object
) -> pb2.Grade:
    """``GradeTrial``'s verdict on a trial that sent no transcript at all.

    An empty ``llm_messages_json`` is the runner's own events-less timeline: with
    no calls executed either, the RPC builds a timeline with nothing on it — the
    same input the core engine gets from a trajectory carrying no messages.
    """
    trial_id = f"eventless:{pack}"
    described = adapter.to_task_description(pack).model_dump(mode="json")
    spec = trial_spec_json(described, trial_id=trial_id)
    registered = servicer.RegisterTrial(register_request(spec, trial_id=trial_id), context)
    assert registered.success is True, registered.error

    response = servicer.GradeTrial(
        pb2.GradeTrialRequest(trial_id=trial_id, llm_messages_json=""), context
    )
    assert response.success is True, response.error
    return response.grade


def test_an_events_less_trial_leaves_transcript_rules_unscored_on_both_substrates(
    parity_adapter: NativeAdapter, tmp_path: Path, runner_service, mock_grpc_context
):
    """A trial that left no trace scores no ``transcript_rules`` on either substrate.

    Every rule ``must_contain_one`` declares would be scored against evidence the
    trial does not carry, so the component is left out of the fold rather than
    recorded as a failure the agent earned. Both substrates are driven through
    their own integration point — ``grade_trajectory`` and the ``GradeTrial`` RPC —
    so a substrate reaching the decision only inside the shared evaluator, or not
    reaching it at all, fails here.
    """
    pack = "must_contain_one"
    core_component = _core_eventless_grade(
        parity_adapter, pack, tmp_path
    ).components.transcript_rules
    runner_grade = _runner_eventless_grade(parity_adapter, pack, runner_service, mock_grpc_context)

    assert core_component is None, (
        f"the core engine scored transcript_rules {core_component} on a trial whose "
        "timeline carries no events"
    )
    assert runner_grade.components.transcript_rules == _UNSCORED_ON_THE_WIRE, (
        f"the runner scored transcript_rules {runner_grade.components.transcript_rules} "
        "on a trial whose timeline carries no events"
    )


def test_an_events_less_trial_with_a_declared_floor_is_scored_by_the_floor_alone(
    parity_adapter: NativeAdapter, tmp_path: Path, runner_service, mock_grpc_context
):
    """A declared activity floor is the one rule an events-less timeline answers.

    ``activity_floor_and_must_contain`` declares a floor beside a phrase rule. No
    events is precisely the floor's answer, so both substrates score the component
    — and score it on the floor alone, which is why neither account of the grade
    names the phrase it was declared beside.
    """
    pack = "activity_floor_and_must_contain"
    core_grade = _core_eventless_grade(parity_adapter, pack, tmp_path)
    runner_grade = _runner_eventless_grade(parity_adapter, pack, runner_service, mock_grpc_context)

    assert core_grade.components.transcript_rules == runner_grade.components.transcript_rules == 0.0
    for substrate, reasons in (("core", core_grade.reasons), ("runner", runner_grade.reasons)):
        account = f"{substrate} on an events-less timeline: {reasons}"
        assert "min_assistant_turns" in reasons, f"the floor was not evaluated — {account}"
        assert "Refund issued" not in reasons, f"the floor's siblings were scored — {account}"
