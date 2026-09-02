"""Unit tests for deterministic failure attribution."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tests.utils.recorded_calls import recorded_call
from tests.utils.servicer_runtime import DUPLICATE_CALL_ID, produce_grading_refusal
from tolokaforge.core.failure_attribution import (
    EXCLUDED_TYPED_REASONS,
    TrialOutcomeClass,
    attribute_failure,
    classify_trial_outcome,
    is_failed_trajectory,
    summarize_failure_attributions,
)
from tolokaforge.core.grading.checks_helpers import custom_checks_reason
from tolokaforge.core.grading.checks_interface import CheckResult, CheckResultSet, CheckStatus
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    Message,
    MessageRole,
    Metrics,
    TerminationReason,
    ToolExecutionStatus,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.orchestrator import Orchestrator

pytestmark = pytest.mark.unit

_MEASURED = TrialOutcomeClass.MEASURED
_HARNESS = TrialOutcomeClass.HARNESS_ERROR
_ABORT = TrialOutcomeClass.INFRASTRUCTURE_ABORT
_UNGRADEABLE = TrialOutcomeClass.UNGRADEABLE

# Every ``TrialStatus`` x (``TerminationReason`` | None) cell of a trial whose
# grading did not refuse, with both answers written out: what the trial is
# counted as, and whether it is retried. The cross-product is the point — over
# the handful of pairs a reader expects, any default looks like any other, so a
# test restricted to them proves the table's scope and never its truth.
#
# The two columns disagree in six reachable cells — ``(error, api_error)``,
# ``(error, error)``, ``(error, trial_lost)``, ``(timeout, timeout)``,
# ``(timeout, empty_completion)`` and ``(error, empty_completion)`` are retried
# *and* counted — and that is the design, not a defect: whether an attempt is
# worth repeating and whether it measured the agent are different questions.
# Deriving either column from the other is what this table exists to prevent.
_GRADED_CELLS: tuple[tuple[TrialStatus, TerminationReason | None, TrialOutcomeClass, bool], ...] = (
    (TrialStatus.COMPLETED, TerminationReason.AGENT_DONE, _MEASURED, False),
    (TrialStatus.COMPLETED, TerminationReason.USER_STOP, _MEASURED, False),
    (TrialStatus.COMPLETED, TerminationReason.STUCK_DETECTED, _MEASURED, False),
    (TrialStatus.COMPLETED, TerminationReason.MAX_TURNS, _MEASURED, False),
    (TrialStatus.COMPLETED, TerminationReason.TIMEOUT, _MEASURED, True),
    (TrialStatus.COMPLETED, TerminationReason.ERROR, _HARNESS, True),
    (TrialStatus.COMPLETED, TerminationReason.RATE_LIMIT, _ABORT, True),
    (TrialStatus.COMPLETED, TerminationReason.API_TIMEOUT, _ABORT, False),
    (TrialStatus.COMPLETED, TerminationReason.API_ERROR, _MEASURED, True),
    (TrialStatus.COMPLETED, TerminationReason.EMPTY_COMPLETION, _MEASURED, False),
    (TrialStatus.COMPLETED, TerminationReason.PROVISION_ERROR, _ABORT, False),
    (TrialStatus.COMPLETED, TerminationReason.TRIAL_LOST, _HARNESS, False),
    (TrialStatus.COMPLETED, None, _MEASURED, False),
    (TrialStatus.FAILED, TerminationReason.AGENT_DONE, _MEASURED, False),
    (TrialStatus.FAILED, TerminationReason.USER_STOP, _MEASURED, False),
    (TrialStatus.FAILED, TerminationReason.STUCK_DETECTED, _MEASURED, False),
    (TrialStatus.FAILED, TerminationReason.MAX_TURNS, _MEASURED, False),
    (TrialStatus.FAILED, TerminationReason.TIMEOUT, _MEASURED, True),
    (TrialStatus.FAILED, TerminationReason.ERROR, _HARNESS, True),
    (TrialStatus.FAILED, TerminationReason.RATE_LIMIT, _ABORT, True),
    (TrialStatus.FAILED, TerminationReason.API_TIMEOUT, _ABORT, False),
    (TrialStatus.FAILED, TerminationReason.API_ERROR, _MEASURED, True),
    (TrialStatus.FAILED, TerminationReason.EMPTY_COMPLETION, _MEASURED, False),
    (TrialStatus.FAILED, TerminationReason.PROVISION_ERROR, _ABORT, False),
    (TrialStatus.FAILED, TerminationReason.TRIAL_LOST, _HARNESS, False),
    (TrialStatus.FAILED, None, _HARNESS, False),
    (TrialStatus.TIMEOUT, TerminationReason.AGENT_DONE, _MEASURED, True),
    (TrialStatus.TIMEOUT, TerminationReason.USER_STOP, _MEASURED, True),
    (TrialStatus.TIMEOUT, TerminationReason.STUCK_DETECTED, _MEASURED, True),
    (TrialStatus.TIMEOUT, TerminationReason.MAX_TURNS, _MEASURED, True),
    (TrialStatus.TIMEOUT, TerminationReason.TIMEOUT, _MEASURED, True),
    (TrialStatus.TIMEOUT, TerminationReason.ERROR, _HARNESS, True),
    (TrialStatus.TIMEOUT, TerminationReason.RATE_LIMIT, _ABORT, True),
    (TrialStatus.TIMEOUT, TerminationReason.API_TIMEOUT, _ABORT, True),
    (TrialStatus.TIMEOUT, TerminationReason.API_ERROR, _MEASURED, True),
    (TrialStatus.TIMEOUT, TerminationReason.EMPTY_COMPLETION, _MEASURED, True),
    (TrialStatus.TIMEOUT, TerminationReason.PROVISION_ERROR, _ABORT, False),
    (TrialStatus.TIMEOUT, TerminationReason.TRIAL_LOST, _HARNESS, True),
    (TrialStatus.TIMEOUT, None, _HARNESS, True),
    (TrialStatus.ERROR, TerminationReason.AGENT_DONE, _MEASURED, True),
    (TrialStatus.ERROR, TerminationReason.USER_STOP, _MEASURED, True),
    (TrialStatus.ERROR, TerminationReason.STUCK_DETECTED, _MEASURED, True),
    (TrialStatus.ERROR, TerminationReason.MAX_TURNS, _MEASURED, True),
    (TrialStatus.ERROR, TerminationReason.TIMEOUT, _MEASURED, True),
    (TrialStatus.ERROR, TerminationReason.ERROR, _HARNESS, True),
    (TrialStatus.ERROR, TerminationReason.RATE_LIMIT, _ABORT, True),
    (TrialStatus.ERROR, TerminationReason.API_TIMEOUT, _ABORT, True),
    (TrialStatus.ERROR, TerminationReason.API_ERROR, _MEASURED, True),
    (TrialStatus.ERROR, TerminationReason.EMPTY_COMPLETION, _MEASURED, True),
    (TrialStatus.ERROR, TerminationReason.PROVISION_ERROR, _ABORT, False),
    (TrialStatus.ERROR, TerminationReason.TRIAL_LOST, _HARNESS, True),
    (TrialStatus.ERROR, None, _HARNESS, True),
)

# The same cross-product a second time over: every cell as it stands when
# grading refused. The class column is ``UNGRADEABLE`` throughout — a refusal is
# evidence about us, so no termination reason buys the trial out of the
# denominator — and the retryability column is copied from the graded half,
# which is what makes running both halves against production a parity lock
# rather than two unrelated passes.
_OUTCOME_CELLS: tuple[
    tuple[TrialStatus, TerminationReason | None, bool, TrialOutcomeClass, bool], ...
] = (
    *(
        (status, reason, False, outcome_class, retryable)
        for status, reason, outcome_class, retryable in _GRADED_CELLS
    ),
    *(
        (status, reason, True, _UNGRADEABLE, retryable)
        for status, reason, _, retryable in _GRADED_CELLS
    ),
)


def _cell_id(
    status: TrialStatus, reason: TerminationReason | None, *, ungradeable: bool = False
) -> str:
    stem = f"{status.value}-{reason.value if reason else 'unset'}"
    return f"{stem}-ungradeable" if ungradeable else stem


def outcome_cells() -> tuple:
    """The graded cells, as pytest params keyed by ``status-reason``.

    Exported for :mod:`tests.unit.test_trial_grader`, which drives the grader
    over them. A trajectory carrying ``grading_error`` never reaches grading —
    the conductor writes the field only after ``grade`` has already raised — so
    the ungradeable half is not that suite's domain.
    """
    return tuple(pytest.param(cell, id=_cell_id(cell[0], cell[1])) for cell in _GRADED_CELLS)


def all_outcome_cells() -> tuple:
    """Every cell, graded and ungradeable, as pytest params."""
    return tuple(
        pytest.param(cell, id=_cell_id(cell[0], cell[1], ungradeable=cell[2]))
        for cell in _OUTCOME_CELLS
    )


def _cell_trajectory(
    status: TrialStatus, reason: TerminationReason | None, *, grading_error: str | None = None
) -> Trajectory:
    traj = _base_trajectory(grading_error=grading_error)
    traj.status = status
    traj.termination_reason = reason
    return traj


@pytest.fixture
def real_refusal(runner_service, mock_grpc_context) -> str:
    """The message a real ``GradeTrial`` refusal carries.

    Produced, not typed: the in-process servicer records two tool calls under
    one ``call_id`` and the production grader is driven at the result, so no cell
    of this cross-product invents what a grading failure looks like.
    """
    return produce_grading_refusal(runner_service, mock_grpc_context)


class TestOutcomeClassificationCrossProduct:
    """``classify_trial_outcome`` is total, and the exclusion default is an
    allowlist: a reason it has never heard of is counted, not dropped."""

    def test_the_table_covers_every_cell_exactly_once(self) -> None:
        cells = {
            (status, reason, ungradeable) for status, reason, ungradeable, _, _ in _OUTCOME_CELLS
        }
        expected = {
            (status, reason, ungradeable)
            for status in TrialStatus
            for reason in (*TerminationReason, None)
            for ungradeable in (False, True)
        }
        assert cells == expected
        assert len(_OUTCOME_CELLS) == len(expected) == 104

    def test_the_class_column_exhausts_the_declared_vocabulary(self) -> None:
        """The table is hand-maintained and the enum is declared in production,
        so this compares two sources: a class nobody tabled is a class whose
        counting no cell of this cross-product checks."""
        assert {outcome_class for _, _, _, outcome_class, _ in _OUTCOME_CELLS} == set(
            TrialOutcomeClass
        )

    @pytest.mark.parametrize("cell", all_outcome_cells())
    def test_every_cell_classifies_as_tabled(
        self,
        cell: tuple[TrialStatus, TerminationReason | None, bool, TrialOutcomeClass, bool],
        real_refusal: str,
    ) -> None:
        status, reason, ungradeable, expected_class, _ = cell
        traj = _cell_trajectory(status, reason, grading_error=real_refusal if ungradeable else None)
        assert classify_trial_outcome(traj) is expected_class

    def test_an_unrecognised_reason_would_be_counted(self) -> None:
        """The allowlist default, asserted where it matters: exclusion is
        earned by membership, so a reason nobody classified stays in the
        denominator and stays visible."""
        counted = {
            reason
            for reason in TerminationReason
            if classify_trial_outcome(_cell_trajectory(TrialStatus.ERROR, reason)) is not _ABORT
        }
        assert counted == set(TerminationReason) - EXCLUDED_TYPED_REASONS

    def test_empty_completion_stays_in_the_measured_denominator(self) -> None:
        """A trial whose provider returned nothing is a measurement of the
        model, not evidence the substrate broke, so it stays in the denominator.
        The guard names the mechanism: adding ``EMPTY_COMPLETION`` to the
        excluded set here would silently drop it from
        ``measured_trials`` without failing
        ``test_an_unrecognised_reason_would_be_counted`` (which locks the shape
        of the derivation, not which reasons appear on either side)."""
        assert TerminationReason.EMPTY_COMPLETION not in EXCLUDED_TYPED_REASONS


class TestRetryabilityIsIndependentOfCountability:
    """``Orchestrator._is_retryable_trajectory`` is pinned bit-identical over
    the same cells. It is deliberately NOT derived from the classification —
    the cells where the two answers differ are the design.

    Grading is the same kind of independence one dimension over: the ungradeable
    half of the table carries the graded half's retryability answers, so this
    pass fails on any cell where a refusal moved the decision to repeat the
    attempt."""

    @pytest.mark.parametrize("cell", all_outcome_cells())
    def test_every_cell_retries_as_tabled(
        self,
        cell: tuple[TrialStatus, TerminationReason | None, bool, TrialOutcomeClass, bool],
        real_refusal: str,
    ) -> None:
        status, reason, ungradeable, _, expected_retryable = cell
        traj = _cell_trajectory(status, reason, grading_error=real_refusal if ungradeable else None)
        assert Orchestrator._is_retryable_trajectory(traj) is expected_retryable

    def test_an_auth_shaped_api_error_is_not_retried(self) -> None:
        """The one cell whose answer depends on the message rather than the
        pair: a bad key fails the same way on every attempt."""
        traj = _cell_trajectory(TrialStatus.ERROR, TerminationReason.API_ERROR)
        traj.messages = [
            Message(
                role=MessageRole.SYSTEM,
                content="API error: LLM API call failed: AuthenticationError - invalid key",
            )
        ]
        assert Orchestrator._is_retryable_trajectory(traj) is False
        # Countability does not consult the message, so it is unmoved.
        assert classify_trial_outcome(traj) is _MEASURED

    def test_the_two_answers_disagree_where_the_table_says_they_do(self) -> None:
        """A guard on the table itself: if these cells ever agree, one column
        was derived from the other and the regression lock stopped biting."""
        disagreements = {
            (status, reason)
            for status, reason, outcome_class, retryable in _GRADED_CELLS
            if retryable is not (outcome_class is _ABORT)
        }
        assert (TrialStatus.ERROR, TerminationReason.API_ERROR) in disagreements
        assert (TrialStatus.ERROR, TerminationReason.ERROR) in disagreements
        assert (TrialStatus.ERROR, TerminationReason.TRIAL_LOST) in disagreements
        assert (TrialStatus.TIMEOUT, TerminationReason.TIMEOUT) in disagreements


def _base_trajectory(*, grading_error: str | None = None) -> Trajectory:
    """A trial with a verdict, or with the reason grading refused instead.

    The two are mutually exclusive — ``Trajectory`` rejects a value carrying
    both — so an ungradeable trial carries no :class:`Grade` at all.
    """
    return Trajectory(
        task_id="task_x",
        trial_index=0,
        start_ts=datetime.now(tz=timezone.utc),
        end_ts=datetime.now(tz=timezone.utc),
        messages=[Message(role=MessageRole.USER, content="hello")],
        metrics=Metrics(),
        grade=(
            None
            if grading_error
            else Grade(binary_pass=False, score=0.0, components=GradeComponents(), reasons="failed")
        ),
        grading_error=grading_error,
    )


def test_timeout_classification():
    traj = _base_trajectory()
    traj.status = TrialStatus.TIMEOUT
    traj.termination_reason = TerminationReason.TIMEOUT

    # A non-provision trajectory carries no provisioning-lifecycle stage; the
    # field is populated only when the executor synthesizes a
    # ``PROVISION_ERROR`` result. Locks the reverse direction of the invariant
    # ``provision_stage non-None iff termination_reason == PROVISION_ERROR``.
    assert traj.provision_stage is None

    assert is_failed_trajectory(traj) is True
    attribution = attribute_failure(traj)
    assert attribution["failure_class"] == "timeout_or_resource"
    assert attribution["deterministic"] is True
    # ``provision_stage`` is a first-class field on every attribution record —
    # present-but-null off the provision path so downstream consumers read it
    # without a ``.get()`` / ``KeyError`` dance.
    assert attribution["provision_stage"] is None


def test_provision_error_classification():
    """A trajectory carrying ``PROVISION_ERROR`` classifies as ``provision_failure`` —
    substrate came up wrong before the trial body ran, so the failure is
    infra-side, not model-side. The stage that raised is a first-class field
    on the returned dict alongside the evidence list, not buried inside it: a
    reader answering "which point of the provisioning lifecycle failed" does
    not walk ``evidence`` to find out.
    """
    traj = _base_trajectory()
    traj.status = TrialStatus.ERROR
    traj.termination_reason = TerminationReason.PROVISION_ERROR
    traj.provision_stage = "register_trial"

    assert is_failed_trajectory(traj) is True
    attribution = attribute_failure(traj)
    assert attribution["failure_class"] == "provision_failure"
    assert attribution["deterministic"] is True
    assert attribution["provision_stage"] == "register_trial"
    assert any(e["kind"] == "termination_reason" for e in attribution["evidence"])

    # A provision failure (which a reset-recipe failure surfaces as) counts
    # toward the run's failed attempts in the summary rollup.
    summary = summarize_failure_attributions([attribution])
    assert summary["total_failed_attempts"] == 1
    assert summary["by_failure_class"]["provision_failure"] == 1


def test_an_empty_completion_is_attributed_to_a_provider_side_resource_event() -> None:
    """The misattribution this reason exists to end, one artifact over: an
    empty-completion trial has no failed tool call to scan — the model produced
    nothing — so without its own branch it falls through to ``model_reasoning``
    at confidence 0.5, blaming the agent for what was a provider-side event
    (both ``text`` and ``tool_calls`` empty in the wire response)."""
    traj = _base_trajectory()
    traj.status = TrialStatus.FAILED
    traj.termination_reason = TerminationReason.EMPTY_COMPLETION

    attribution = attribute_failure(traj)

    assert attribution["failure_class"] == "timeout_or_resource"
    assert attribution["deterministic"] is True
    assert attribution["confidence"] == 1.0
    assert attribution["evidence"] == [
        {"kind": "termination_reason", "value": "empty_completion", "status": "failed"}
    ]


def test_a_lost_trial_is_attributed_to_the_substrate_not_to_the_model() -> None:
    """The misattribution this reason exists to end, one artifact over: a lost
    trial has no failed tool call to scan — the call that hit the fault reached
    no tool and was never recorded — so without its own branch it falls all the
    way through to ``model_reasoning`` at confidence 0.5, and
    ``failure_attribution.json`` reports a substrate fault as the agent's
    reasoning."""
    traj = _base_trajectory()
    traj.status = TrialStatus.ERROR
    traj.termination_reason = TerminationReason.TRIAL_LOST

    attribution = attribute_failure(traj)

    assert attribution["failure_class"] == "infrastructure"
    assert attribution["deterministic"] is True
    assert attribution["confidence"] == 1.0
    assert attribution["evidence"] == [
        {"kind": "termination_reason", "value": "trial_lost", "status": "error"}
    ]


def test_a_grading_refusal_is_attributed_to_us_not_to_the_model(real_refusal: str) -> None:
    """The misattribution one artifact over: an ungradeable trial has no failed
    tool call and no grade to inspect, so without its own branch it falls all the
    way through to ``model_reasoning`` and ``failure_attribution.json`` blames
    the agent for a grading fault of ours."""
    traj = _cell_trajectory(
        TrialStatus.COMPLETED, TerminationReason.AGENT_DONE, grading_error=real_refusal
    )

    attribution = attribute_failure(traj)

    assert attribution["failure_class"] == "grading_failure"
    assert attribution["deterministic"] is True
    assert attribution["outcome_class"] == "ungradeable"
    evidence = [ev for ev in attribution["evidence"] if ev["kind"] == "grading_error"]
    assert len(evidence) == 1
    assert DUPLICATE_CALL_ID in evidence[0]["error"]


def test_tool_argument_classification():
    traj = _base_trajectory()
    traj.tool_log = [
        recorded_call(
            "db_query",
            status=ToolExecutionStatus.INVALID_ARGUMENTS,
            output="Invalid arguments: 'id' is required",
        )
    ]
    attribution = attribute_failure(traj)
    assert attribution["failure_class"] == "tool_arguments"
    assert attribution["deterministic"] is True


def test_grader_contract_classification():
    traj = _base_trajectory()
    assert traj.grade is not None
    traj.grade.state_diff = {"orders.status": {"expected": "done", "actual": "pending"}}
    attribution = attribute_failure(traj)
    assert attribution["failure_class"] == "grader_contract"
    assert attribution["deterministic"] is True


def test_attribution_summary():
    a = {
        "failure_class": "tool_execution",
        "deterministic": True,
        "evidence": [{"tool": "browser"}],
    }
    b = {"failure_class": "model_reasoning", "deterministic": False, "evidence": []}
    summary = summarize_failure_attributions([a, b])
    assert summary["total_failed_attempts"] == 2
    assert summary["by_failure_class"]["tool_execution"] == 1
    assert summary["by_failure_class"]["model_reasoning"] == 1
    assert summary["by_tool"]["browser"] == 1


def test_summary_coverage_is_none_when_no_failures():
    """0/0 deterministic attribution coverage is meaningless — return None.

    Previously returned 0.0, which the analyze_run.py reporter then
    formatted as "0.000" alongside successful runs and confused readers.
    """
    summary = summarize_failure_attributions([])
    assert summary["total_failed_attempts"] == 0
    assert summary["deterministic_attribution_coverage"] is None


def test_infrastructure_class_for_connection_errors():
    """ERR_CONNECTION_REFUSED in messages → infrastructure failure class."""
    traj = _base_trajectory()
    traj.messages = [
        Message(role=MessageRole.USER, content="please check the portal"),
        Message(
            role=MessageRole.ASSISTANT,
            content="The browser returned net::ERR_CONNECTION_REFUSED at http://mock-web:8080",
        ),
    ]
    attribution = attribute_failure(traj)
    assert attribution["failure_class"] == "infrastructure"
    assert attribution["deterministic"] is True
    kinds = {ev["kind"] for ev in attribution["evidence"]}
    assert "connection_errors" in kinds


def test_grade_fail_patterns_extracted_from_reasons_string():
    traj = _base_trajectory()
    assert traj.grade is not None
    traj.grade.reasons = (
        "Files: PASS: order ref; "
        "FAIL: must mention compliance escalation; "
        "FAIL: must mention 7 business days"
    )
    attribution = attribute_failure(traj)
    fail_evidence = [ev for ev in attribution["evidence"] if ev["kind"] == "grade_fail_patterns"]
    assert len(fail_evidence) == 1
    assert any("compliance escalation" in p for p in fail_evidence[0]["patterns"])


def test_a_custom_checks_segment_contributes_evidence_exactly_when_a_check_lost():
    """A custom-checks trial now contributes what its transcript-graded siblings do.

    The two sentences are produced rather than typed: this reads the same renderer
    both grading substrates emit, so a rewording that stopped the failing sentence
    matching ``"FAIL"`` — or started the passing one matching it — reds here.

    The evidence block is gated on nothing earlier having filled ``evidence``, so the
    fixture carries no failed tool call, no state diff and no connection-error message;
    the assertions above the call are what keep this cell from passing for one of
    those reasons instead.
    """
    losing_check = custom_checks_reason(
        CheckResultSet(
            results=[
                CheckResult(
                    check_name="order_was_shipped",
                    status=CheckStatus.FAILED,
                    score=0.0,
                    message="order O1 is not shipped",
                )
            ]
        )
    )
    # The passing check is named for the thing it did not find, which is the shape a
    # sentence enumerating every check rather than only the losing ones would turn
    # into evidence of a failure the trial never had.
    winning_check = custom_checks_reason(
        CheckResultSet(
            results=[
                CheckResult(
                    check_name="no_failures_logged",
                    status=CheckStatus.PASSED,
                    score=1.0,
                    message="no failure rows in the ledger",
                )
            ]
        )
    )

    lost = _base_trajectory()
    won = _base_trajectory()
    assert lost.grade is not None and won.grade is not None
    lost.grade.reasons = losing_check
    won.grade.reasons = winning_check

    assert (lost.tool_log, won.tool_log) == ([], [])
    assert (lost.grade.state_diff, won.grade.state_diff) == (None, None)

    def _patterns(trajectory):
        return [
            ev
            for ev in attribute_failure(trajectory)["evidence"]
            if ev["kind"] == "grade_fail_patterns"
        ]

    lost_evidence = _patterns(lost)
    assert len(lost_evidence) == 1
    assert lost_evidence[0]["patterns"] == [losing_check]
    assert "order_was_shipped" in lost_evidence[0]["patterns"][0]

    assert _patterns(won) == []


#: Every error text either substrate hands the renderer, from the site that writes it:
#: the executor's module-load failure (``check_runner.py``), the host's missing-file and
#: absent-task-directory paths (``combine.py``), and the runner's undelivered-artifacts
#: path (``service.py``). Written out rather than derived, because the point is that
#: they are unlike each other.
_SUITE_ERRORS = (
    "Failed to load/run checks: invalid syntax (checks.py, line 1)",
    "checks file not found: checks.py",
    "task_dir not available",
    "custom_checks.enabled but no artifacts_dir for trial 'reconcile:0'",
)


@pytest.mark.parametrize("error", _SUITE_ERRORS)
def test_a_suite_that_failed_to_run_contributes_evidence_whatever_the_error_says(error):
    """One state, one classification — not one per wording.

    A suite that never ran is a component the fold reads as failed, whichever site
    wrote the error. The sentence carries ``failed`` in the renderer's own words, so
    the classification is a function of the state rather than of prose written in
    another module: three of these four texts contain no ``fail`` of their own, and
    a rule inheriting the substring from them would answer the same state four
    different ways.
    """
    traj = _base_trajectory()
    assert traj.grade is not None
    traj.grade.reasons = custom_checks_reason(CheckResultSet(error=error))

    evidence = [
        ev for ev in attribute_failure(traj)["evidence"] if ev["kind"] == "grade_fail_patterns"
    ]

    assert len(evidence) == 1, f"{error!r} produced {evidence!r}"
    assert evidence[0]["patterns"] == [traj.grade.reasons]


def test_missing_write_file_tool_when_grading_expected_files():
    traj = _base_trajectory()
    assert traj.grade is not None
    traj.grade.reasons = "Files: FAIL: No files match /env/fs/agent-visible/submissions/*"
    traj.tool_log = [recorded_call("browser")]
    attribution = attribute_failure(traj)
    missing_tool_evidence = [ev for ev in attribution["evidence"] if ev["kind"] == "missing_tool"]
    assert len(missing_tool_evidence) == 1
    assert missing_tool_evidence[0]["tool"] == "write_file"


def _synth_trajectory(
    marker: TerminationReason, termination: TerminationReason | None
) -> Trajectory:
    """A trajectory carrying a harness-synthesised auto-fail grade.

    ``termination`` is the trajectory's own ``termination_reason`` (what the
    ``TrialGrader`` observed); ``marker`` is the value that lands on
    :attr:`Grade.synthesized_by_termination_reason`. The two are decoupled so
    the STUCK case (whose termination reason is enumerated, but whose marker
    is set to STUCK_DETECTED by the grader) can be exercised beside the
    ERROR-fallback case (termination is None; marker is ERROR).
    """
    traj = _base_trajectory()
    traj.status = TrialStatus.COMPLETED if termination is not None else TrialStatus.ERROR
    traj.termination_reason = termination
    assert traj.grade is not None
    traj.grade.synthesized_by_termination_reason = marker
    return traj


def test_a_stuck_detected_synth_grade_is_attributed_to_harness_autofail() -> None:
    """The misattribution one artifact over: a STUCK_DETECTED synth previously
    fell past the deterministic elif chain (STUCK_DETECTED is not in the
    ``timeout_or_resource`` set), reached the tool-log scan with an empty log,
    and settled on ``model_reasoning`` — blaming the model when the harness
    itself terminated the trial. The new ``harness_autofail`` branch catches
    every synth-marker grade the elif chain does not, and marks it
    deterministic with the reason as evidence.
    """
    traj = _synth_trajectory(
        marker=TerminationReason.STUCK_DETECTED,
        termination=TerminationReason.STUCK_DETECTED,
    )

    attribution = attribute_failure(traj)

    assert attribution["failure_class"] == "harness_autofail"
    assert attribution["deterministic"] is True
    assert attribution["confidence"] == 1.0
    assert attribution["synthesized"] is True
    assert attribution["synthesized_by_termination_reason"] == "stuck_detected"
    synth_evidence = [ev for ev in attribution["evidence"] if ev["kind"] == "synthesized_grade"]
    assert synth_evidence == [{"kind": "synthesized_grade", "termination_reason": "stuck_detected"}]


def test_a_synth_grade_over_an_enumerated_termination_reason_keeps_its_class() -> None:
    """A synth grade whose ``TerminationReason`` is in the ``timeout_or_resource``
    branch (EMPTY_COMPLETION, ERROR, TIMEOUT) keeps that class — the marker
    fields ride alongside without changing the failure_class the enumerated
    branch already catches. Otherwise a downstream consumer aggregating on
    ``failure_class`` would see EMPTY_COMPLETION migrate from
    ``timeout_or_resource`` to ``harness_autofail`` under a synth grade,
    which is a wire shift the ``harness_autofail`` extension does not intend.
    """
    traj = _synth_trajectory(
        marker=TerminationReason.EMPTY_COMPLETION,
        termination=TerminationReason.EMPTY_COMPLETION,
    )

    attribution = attribute_failure(traj)

    assert attribution["failure_class"] == "timeout_or_resource"
    assert attribution["deterministic"] is True
    assert attribution["synthesized"] is True
    assert attribution["synthesized_by_termination_reason"] == "empty_completion"


def test_a_real_measured_grade_is_not_marked_synthesized() -> None:
    """The control: the two new fields default off on every grade a real
    evaluator produced.

    Without this the fields could default ``True``/populated and every downstream
    consumer would read every trial as harness-synthesised.
    """
    traj = _base_trajectory()
    traj.status = TrialStatus.TIMEOUT
    traj.termination_reason = TerminationReason.TIMEOUT

    attribution = attribute_failure(traj)

    assert attribution["synthesized"] is False
    assert attribution["synthesized_by_termination_reason"] is None
