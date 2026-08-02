"""Trace-check replay over recorded bundles — discovery, classification, reading.

Locks the contract of :mod:`tolokaforge.core.grading.trace_replay`:

* discovery keys on ``task.yaml`` + ``trajectory.yaml``, so a trial is re-checkable
  whether or not it was ever graded, and never walks the command's own output;
* a bundle declaring no ``trace_checks`` is a declared skip until an override
  supplies a block, which it replaces wholesale;
* evidence about what the bundle recorded comes from the reader's file-presence
  answer, never from the timeline's ``records_present`` — the two disagree on a
  trial that called no tool, and only one of them can tell a fully-recorded
  zero-call bundle from a bundle written before the record existed;
* every unreadable input is a named per-trial failure that leaves the batch
  running, and a bundle predating call-id threading says that rather than handing
  the operator a pydantic traceback.

Bundles are written by the real :class:`FileArtifactWriter`, so the tests read the
same on-disk contract the eval flow produces.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.canonical._factories import make_trajectory, make_trial_messages
from tests.utils.recorded_calls import recorded_call
from tolokaforge.core.grading.trace_replay import (
    TRACE_REPLAY_DIRNAME,
    ConstraintProvenance,
    TraceReplayOutcomeStatus,
    discover_trace_bundles,
    read_trace_replay_inputs,
    run_trace_replay_batch,
)
from tolokaforge.core.models import TraceChecksConfig, Trajectory
from tolokaforge.core.output.artifacts import FileArtifactWriter, read_recorded_tool_log
from tolokaforge.core.output_writer import TRIAL_BUNDLE_SCHEMA_VERSION

pytestmark = [pytest.mark.unit, pytest.mark.grading]

_TRACE_CHECKS: dict[str, Any] = {
    "constraints": [
        {
            "id": "the_order_was_looked_up",
            "description": "the agent read the order before answering",
            "require": {
                "present": {"match": {"kind": "tool_call", "tool": {"equals": "get_order"}}}
            },
        }
    ]
}
_OVERRIDE = TraceChecksConfig.model_validate(_TRACE_CHECKS)
# The same check with the one field no message can express, so deciding it needs the
# bundle's tool-call record and nothing the message view preserved will do.
_STATUS_TRACE_CHECKS: dict[str, Any] = {
    "constraints": [
        {
            "id": "the_order_lookup_succeeded",
            "description": "the order read came back successful",
            "require": {
                "present": {
                    "match": {
                        "kind": "tool_call",
                        "tool": {"equals": "get_order"},
                        "status": {"equals": "success"},
                    }
                }
            },
        }
    ]
}
_TURNS = ("I want a refund for order O-1.", "Reading the order.")


def _looked_up_the_order() -> Trajectory:
    call = recorded_call(
        "get_order", sequence=0, arguments={"id": "O-1"}, output='{"total": 328.5}'
    )
    return make_trajectory(
        task_id="refund_task", messages=make_trial_messages([call], _TURNS), tool_log=[call]
    )


def _answered_without_calling_anything() -> Trajectory:
    return make_trajectory(
        task_id="refund_task", messages=make_trial_messages([], _TURNS), tool_log=[]
    )


def _write_bundle(
    trial_dir: Path,
    trajectory: Trajectory | None = None,
    *,
    trace_checks: dict[str, Any] | None = _TRACE_CHECKS,
    with_tool_log: bool = True,
) -> Path:
    """A bundle on disk, written the way the eval flow writes one.

    ``with_tool_log=False`` is the shape of a bundle written before the record was
    persisted: every other artifact present, the sidecar absent.
    """
    trajectory = trajectory if trajectory is not None else _looked_up_the_order()
    writer = FileArtifactWriter()
    writer.write_trajectory(trial_dir, trajectory)
    if with_tool_log:
        writer.write_tool_log(trial_dir, trajectory)
    writer.write_metrics(trial_dir, trajectory)
    writer.write_task(
        trial_dir,
        {
            "task_id": trajectory.task_id,
            "grading_config": (
                {"llm_judge": None} if trace_checks is None else {"trace_checks": trace_checks}
            ),
        },
    )
    return trial_dir


def _rewrite_yaml(path: Path, payload: Any) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _persisted_tool_calls(bundle: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    persisted = yaml.safe_load((bundle / "trajectory.yaml").read_text(encoding="utf-8"))
    calls = [
        call for message in persisted["messages"] for call in (message.get("tool_calls") or [])
    ]
    assert calls, "the fixture bundle persists no tool call to strip an id from"
    return persisted, calls


def test_discovery_is_layout_agnostic_over_the_three_recorded_shapes(tmp_path: Path) -> None:
    """A bundle is ``task.yaml`` + ``trajectory.yaml``, wherever the layout puts it.

    Not ``grade.yaml``: a trial is worth re-checking whether or not it was graded,
    and keying on the grade is what makes judge replay miss ungraded trials.
    """
    run_dir = tmp_path / "run"
    nested = [_write_bundle(run_dir / "trials" / "refund_task" / str(index)) for index in (0, 1)]
    flat = tmp_path / "flat"
    loose = [_write_bundle(flat / name) for name in ("a", "b")]
    for bundle in nested:
        (bundle / "grade.yaml").unlink(missing_ok=True)

    assert discover_trace_bundles(run_dir) == nested
    assert discover_trace_bundles(flat) == loose
    assert discover_trace_bundles(flat / "a") == [flat / "a"]


def test_a_bundle_nested_under_the_output_subtree_is_not_discovered(tmp_path: Path) -> None:
    """Discovery never walks the subtree the command writes into.

    A source re-pointed at a run that already holds replay output would otherwise
    re-check whatever came to sit under it.
    """
    live = _write_bundle(tmp_path / "trials" / "refund_task" / "0")
    _write_bundle(tmp_path / TRACE_REPLAY_DIRNAME / "earlier" / "refund_task" / "0")

    assert discover_trace_bundles(tmp_path) == [live]


def test_a_bundle_declaring_no_trace_checks_is_skipped_until_an_override_supplies_them(
    tmp_path: Path,
) -> None:
    """The skip is declared, and an override is what makes the same bundle eligible."""
    _write_bundle(tmp_path / "trials" / "refund_task" / "0", trace_checks=None)

    (skipped,) = run_trace_replay_batch(tmp_path, replay_id="r1")
    assert skipped.status is TraceReplayOutcomeStatus.SKIPPED_NOT_APPLICABLE
    assert skipped.result is None
    assert not (tmp_path / TRACE_REPLAY_DIRNAME).exists()

    (replayed,) = run_trace_replay_batch(tmp_path, replay_id="r2", override=_OVERRIDE)
    assert replayed.status is TraceReplayOutcomeStatus.REPLAYED
    assert replayed.provenance is ConstraintProvenance.OVERRIDE
    assert replayed.result is not None
    assert [item.id for item in replayed.result.constraints] == ["the_order_was_looked_up"]


def test_a_zero_call_bundle_is_recorded_even_though_its_timeline_carries_no_records(
    tmp_path: Path,
) -> None:
    """The evidence signal is the reader's, and on this trial the timeline's disagrees.

    A trial that called no tool writes ``tool_log.yaml`` empty, so its bundle is
    fully recorded; ``records_present`` still reads ``False``, because it says
    whether *this timeline* was handed a record view. Reading it as the evidence
    signal would report a recorded corpus as record-less, and would make the
    zero-call bundle indistinguishable from the one below it, which genuinely
    carries no record at all.
    """
    recorded = _write_bundle(
        tmp_path / "trials" / "refund_task" / "0", _answered_without_calling_anything()
    )
    unrecorded = _write_bundle(
        tmp_path / "trials" / "refund_task" / "1",
        _answered_without_calling_anything(),
        with_tool_log=False,
    )

    assert read_recorded_tool_log(recorded) == ([], True)
    assert read_recorded_tool_log(unrecorded) == ([], False)

    outcomes = run_trace_replay_batch(tmp_path, replay_id="r1", override=_OVERRIDE)
    timelines = [
        read_trace_replay_inputs(bundle, override=_OVERRIDE).timeline
        for bundle in (recorded, unrecorded)
    ]

    assert [outcome.evidence.tool_log_present for outcome in outcomes] == [True, False]
    assert [timeline.records_present for timeline in timelines] == [False, False]
    assert [outcome.evidence.schema_version for outcome in outcomes] == (
        [TRIAL_BUNDLE_SCHEMA_VERSION] * 2
    )


def test_the_record_reaches_the_evaluator_so_a_status_matcher_stays_decidable(
    tmp_path: Path,
) -> None:
    """The sidecar is an input to the re-check, not just something the evidence counts.

    One trajectory written twice, with the record and without it, against a
    constraint reading ``status`` — the field only the record carries. The recorded
    bundle decides it; the record-less one reports ``undecided`` rather than telling
    the author their constraint failed on a trial that satisfied it.
    """
    _write_bundle(tmp_path / "trials" / "refund_task" / "0", trace_checks=_STATUS_TRACE_CHECKS)
    _write_bundle(
        tmp_path / "trials" / "refund_task" / "1",
        trace_checks=_STATUS_TRACE_CHECKS,
        with_tool_log=False,
    )

    with_record, without_record = run_trace_replay_batch(tmp_path, replay_id="r1")

    assert with_record.result is not None and without_record.result is not None
    assert [(item.passed, item.undecided) for item in with_record.result.constraints] == [
        (True, False)
    ]
    assert [(item.passed, item.undecided) for item in without_record.result.constraints] == [
        (False, True)
    ]


def test_an_unreadable_trajectory_fails_its_own_bundle_and_the_batch_continues(
    tmp_path: Path,
) -> None:
    """A broken bundle is named and left behind, never aborting the trials after it."""
    broken = _write_bundle(tmp_path / "trials" / "refund_task" / "0")
    healthy = _write_bundle(tmp_path / "trials" / "refund_task" / "1")
    (broken / "trajectory.yaml").write_text("messages: [\n", encoding="utf-8")

    failed, replayed = run_trace_replay_batch(tmp_path, replay_id="r1")

    assert failed.status is TraceReplayOutcomeStatus.FAILED
    assert str(broken / "trajectory.yaml") in (failed.reason or "")
    assert replayed.bundle == healthy
    assert replayed.status is TraceReplayOutcomeStatus.REPLAYED


def test_a_bundle_predating_call_ids_says_so_rather_than_leaking_a_traceback(
    tmp_path: Path,
) -> None:
    """The id is the join, so its absence is not a validation detail — it is the answer.

    A trial persisted before calls carried ids has no key joining a call to the
    result it produced, so nothing about it is re-checkable. What the operator needs
    to read is that, not the pydantic error that discovered it.
    """
    bundle = _write_bundle(tmp_path / "trials" / "refund_task" / "0", with_tool_log=False)
    persisted, calls = _persisted_tool_calls(bundle)
    for call in calls:
        call.pop("id")
    _rewrite_yaml(bundle / "trajectory.yaml", persisted)

    (outcome,) = run_trace_replay_batch(tmp_path, replay_id="r1")

    assert outcome.status is TraceReplayOutcomeStatus.FAILED
    assert str(bundle) in (outcome.reason or "")
    assert "predates call-id threading" in (outcome.reason or "")


def test_a_persisted_call_id_that_is_empty_reads_as_the_same_defect(tmp_path: Path) -> None:
    """The model states the rule twice — required, and non-empty — and both are that defect.

    The empty string fails the validator on ``ToolCall`` itself rather than the
    field, so it arrives as an error one level up the location path and would be
    missed by a translation keyed on the field name alone.
    """
    bundle = _write_bundle(tmp_path / "trials" / "refund_task" / "0", with_tool_log=False)
    persisted, calls = _persisted_tool_calls(bundle)
    for call in calls:
        call["id"] = ""
    _rewrite_yaml(bundle / "trajectory.yaml", persisted)

    (outcome,) = run_trace_replay_batch(tmp_path, replay_id="r1")

    assert outcome.status is TraceReplayOutcomeStatus.FAILED
    assert "predates call-id threading" in (outcome.reason or "")


@pytest.mark.parametrize("stamped", ["four", True])
def test_an_unreadable_schema_stamp_fails_rather_than_reading_as_unstamped(
    tmp_path: Path, stamped: object
) -> None:
    """An unstamped bundle and a corrupt stamp are different states.

    Absent means older than the stamp, which says nothing about replayability;
    unreadable means the bundle cannot say which artifacts it carries, and reporting
    it as the former puts a false row in the evidence an operator reads the
    discrimination verdicts against. ``True`` is the reading a version check gets
    wrong quietly rather than loudly — a bool is an ``int`` in Python, so it would
    otherwise pass for version 1.
    """
    bundle = _write_bundle(tmp_path / "trials" / "refund_task" / "0")
    metrics = yaml.safe_load((bundle / "metrics.yaml").read_text(encoding="utf-8"))
    metrics["schema_version"] = stamped
    _rewrite_yaml(bundle / "metrics.yaml", metrics)

    (outcome,) = run_trace_replay_batch(tmp_path, replay_id="r1")

    assert outcome.status is TraceReplayOutcomeStatus.FAILED
    assert str(bundle / "metrics.yaml") in (outcome.reason or "")
    assert "schema_version" in (outcome.reason or "")


def test_a_named_trial_replaces_discovery(tmp_path: Path) -> None:
    """``trial`` selects one bundle out of a source that holds several."""
    _write_bundle(tmp_path / "trials" / "refund_task" / "0")
    named = _write_bundle(tmp_path / "trials" / "refund_task" / "1")

    outcomes = run_trace_replay_batch(tmp_path, replay_id="r1", trial=named)

    assert [outcome.bundle for outcome in outcomes] == [named]
    assert outcomes[0].status is TraceReplayOutcomeStatus.REPLAYED
