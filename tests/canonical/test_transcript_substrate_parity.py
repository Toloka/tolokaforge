"""Differential lock: one ``transcript_rules`` pack, two substrates, one score.

Nineteen rows, each an authored pack under ``tests/data/transcript_parity/`` plus
the trial's two views of itself — its messages and its tool-call record. Every row
is graded through **both** substrates' real paths:

- core: ``NativeAdapter.get_grading_config`` → ``GradingEngine.grade_trajectory``
  → ``grade.components.transcript_rules``;
- runner: ``NativeAdapter.to_task_description`` → ``evaluate_transcript_rules``.

Driving through the adapter is what keeps the rows readable as authored packs
rather than as either substrate's internal config model.

Seventeen of the rows are the seven ways the two implementations disagree today,
and each is marked ``xfail(strict=True)``: a row that starts passing fails the
suite, so a marker cannot rot into a silent skip. The two unmarked **anchor** rows
are cases the substrates already agree on, asserted directly and at two different
scores — a harness that drove one substrate twice, or returned a constant, fails
one of them.

``raises=AssertionError`` is on every marker for the same reason ``strict`` is: a
bare ``xfail`` swallows *any* failure, so a row whose pack stopped loading would
report as the expected divergence and its pinned score would never be read. The
runner's column is checked before that assertion and outside it — a table pinning
a value the runner does not produce raises :class:`_FixtureDefect`, which no
marker absorbs.

The fixture root is separate from ``grading_parity`` on purpose: that corpus's
packs each declare exactly one author key, and several rows here declare two so a
veto can be told apart from a fraction.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

import pytest

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

pytestmark = pytest.mark.canonical

_PARITY_GLOB = "transcript_parity/**/task.yaml"
_FIXTURE_TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _Divergence(str, Enum):
    """The seven ways the two implementations of ``transcript_rules`` disagree."""

    AGGREGATION = "aggregation"
    DENOMINATOR = "denominator"
    EVIDENCE_SET = "evidence set"
    CASE = "case"
    CALL_STATUS = "call status"
    VETO_VS_FRACTION = "veto vs fraction"
    RECORD_ABSENT = "record-absent evidence"


def _user(text: str) -> Message:
    return Message(role=MessageRole.USER, content=text)


def _assistant(text: str, *calls: ToolCall) -> Message:
    return Message(role=MessageRole.ASSISTANT, content=text, tool_calls=list(calls) or None)


def _record_for(
    call: ToolCall, status: ToolExecutionStatus, *, output: str = ""
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
        executor=ToolExecutorIdentity.AGENT,
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
    the trial kept a record of the calls it declared is itself one of the seven
    things the substrates read differently.
    """

    row_id: str
    pack: str
    messages: tuple[Message, ...]
    tool_log: tuple[RecordedToolCall, ...]
    expected: float
    divergence: _Divergence | None


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
        divergence=_Divergence.AGGREGATION,
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
        divergence=_Divergence.AGGREGATION,
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
        divergence=_Divergence.AGGREGATION,
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
        divergence=_Divergence.AGGREGATION,
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
        divergence=_Divergence.AGGREGATION,
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
        divergence=_Divergence.DENOMINATOR,
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
        divergence=_Divergence.DENOMINATOR,
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
        divergence=_Divergence.EVIDENCE_SET,
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
        divergence=_Divergence.EVIDENCE_SET,
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
        divergence=_Divergence.EVIDENCE_SET,
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
        divergence=_Divergence.EVIDENCE_SET,
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
        divergence=_Divergence.CASE,
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
        divergence=_Divergence.CALL_STATUS,
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
        divergence=_Divergence.CALL_STATUS,
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
        divergence=_Divergence.VETO_VS_FRACTION,
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
        divergence=_Divergence.VETO_VS_FRACTION,
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
        divergence=_Divergence.RECORD_ABSENT,
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
        divergence=None,
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
        divergence=None,
    ),
)


class _FixtureDefect(Exception):
    """The row cannot measure what it claims, so its verdict is about the fixture.

    Deliberately not an ``AssertionError``: every divergence marker declares
    ``raises=AssertionError``, so this reaches the report as a failure on rows the
    marker would otherwise absorb as the expected divergence.
    """


def _param(row: _Row):
    """The one place a row is marked, so no row can be ``xfail`` without ``strict``."""
    if row.divergence is None:
        return pytest.param(row, id=row.row_id)
    return pytest.param(
        row,
        id=row.row_id,
        marks=pytest.mark.xfail(
            strict=True,
            raises=AssertionError,
            reason=(
                f"transcript_rules diverges on {row.divergence.value}: the core engine "
                f"does not score this pack the {row.expected} the runner does"
            ),
        ),
    )


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


@pytest.mark.parametrize("row", [_param(row) for row in _ROWS])
def test_both_substrates_score_one_transcript_rules_pack_alike(
    row: _Row, parity_adapter: NativeAdapter, tmp_path: Path
):
    """One authored pack and one trial score the same on both substrates.

    The pinned value is the runner's own verdict, which is the semantics the
    unification adopts, so a row is satisfied only by the core engine moving to it.
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
