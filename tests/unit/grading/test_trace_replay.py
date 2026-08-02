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
  the operator a pydantic traceback;
* an override is read off a file that says which file it was, so a block the
  vocabulary refuses is reported against the path the operator supplied rather
  than as an anonymous validation error.

Bundles are written by the real :class:`FileArtifactWriter`, so the tests read the
same on-disk contract the eval flow produces, and every override travels through
the real :func:`load_trace_checks_override`.
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
    TraceChecksOverride,
    TraceChecksOverrideError,
    TraceReplayOutcomeStatus,
    discover_trace_bundles,
    load_trace_checks_override,
    read_trace_replay_inputs,
    run_trace_replay_batch,
)
from tolokaforge.core.models import Trajectory
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


def _override(tmp_path: Path, block: Any = _TRACE_CHECKS) -> TraceChecksOverride:
    """An override as an operator supplies one — written to a file and loaded back.

    Never hand-constructed: the path the loader records is what every rejection
    message is judged on, so a fixture that skipped the loader would test a value
    the command never builds.
    """
    path = tmp_path / "constraints.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(block, sort_keys=False), encoding="utf-8")
    return load_trace_checks_override(path)


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

    (replayed,) = run_trace_replay_batch(tmp_path, replay_id="r2", override=_override(tmp_path))
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

    override = _override(tmp_path)
    outcomes = run_trace_replay_batch(tmp_path, replay_id="r1", override=override)
    timelines = [
        read_trace_replay_inputs(bundle, override=override).timeline
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


def test_a_grading_document_and_a_bare_block_supply_the_same_constraints(
    tmp_path: Path,
) -> None:
    """Both authoring shapes reach the evaluator as one block, and it is the inner one.

    A pack's ``grading.yaml`` nests the block under ``trace_checks:`` and a snippet
    written to iterate on one constraint does not, so ``--constraints`` accepts
    either. What the two must agree on is the block itself: a document form that
    kept its wrapper would hand the authoring gate ``trace_checks.trace_checks`` and
    find no constraint to check.
    """
    document = _override(tmp_path / "doc", {"trace_checks": _TRACE_CHECKS})
    bare = _override(tmp_path / "bare", _TRACE_CHECKS)

    assert document.block == bare.block == _TRACE_CHECKS
    assert document.config == bare.config
    assert [item.id for item in bare.config.constraints] == ["the_order_was_looked_up"]


def test_an_override_carrying_no_constraint_block_names_the_file(tmp_path: Path) -> None:
    """Pointing ``--constraints`` at the wrong document says which document it was.

    A rubric file is the near miss — same directory, same ``grading.yaml`` name in
    a pack — and the operator's next move is to look at the file, so the message
    has to carry it.
    """
    path = tmp_path / "grading.yaml"
    path.write_text(yaml.safe_dump({"llm_judge": {"rubric": {}}}), encoding="utf-8")

    with pytest.raises(TraceChecksOverrideError) as raised:
        load_trace_checks_override(path)

    assert str(path) in str(raised.value)
    assert "declares nothing to re-check" in str(raised.value)


def test_an_empty_override_document_is_refused_at_load_naming_the_file(
    tmp_path: Path,
) -> None:
    """A file that wrote nothing is told what to write, not that its block is empty.

    Standing single case at the degenerate boundary. ``{}`` could be routed to
    ``TraceChecksConfig``, which does refuse it — but its message ends "or drop the
    block", advice with no meaning for a file supplied on the command line. The
    loader answers first and names both keys the file could have carried.
    """
    path = tmp_path / "constraints.yaml"
    path.write_text(yaml.safe_dump({}), encoding="utf-8")

    with pytest.raises(TraceChecksOverrideError) as raised:
        load_trace_checks_override(path)

    assert str(path) in str(raised.value)
    assert "declares nothing to re-check" in str(raised.value)


def test_a_zero_constraint_override_is_refused_at_load_naming_the_file(
    tmp_path: Path,
) -> None:
    """The block model already refuses this; what is new is that the file is named.

    Standing single case at the degenerate boundary. ``constraints: []`` asserts
    nothing and scores nothing, and ``TraceChecksConfig`` says so — but it says it
    about "a trace_checks block", which is every block in the repo. An operator who
    supplied a file needs the file back, so the loader re-raises with the path and
    the model's own reason.
    """
    path = tmp_path / "constraints.yaml"
    path.write_text(yaml.safe_dump({"constraints": []}), encoding="utf-8")

    with pytest.raises(TraceChecksOverrideError) as raised:
        load_trace_checks_override(path)

    assert str(path) in str(raised.value)
    assert "declares neither constraints nor alternatives" in str(raised.value)


def test_a_route_less_alternatives_override_is_refused_at_load_naming_the_file(
    tmp_path: Path,
) -> None:
    """The second degenerate block reaches a different rule, and still names the file.

    Standing single case at the degenerate boundary. ``alternatives: []`` clears the
    neither-constraints-nor-alternatives rule — an alternatives list *is* declared —
    and is refused one rule later for holding fewer than the two routes a choice
    between routes needs. Locking the two messages apart is what stops the path
    assertion from passing on whichever rejection happens to fire.
    """
    path = tmp_path / "constraints.yaml"
    path.write_text(yaml.safe_dump({"alternatives": []}), encoding="utf-8")

    with pytest.raises(TraceChecksOverrideError) as raised:
        load_trace_checks_override(path)

    assert str(path) in str(raised.value)
    assert "declares fewer than the two paths" in str(raised.value)


@pytest.mark.parametrize(
    ("recorded", "said"),
    [
        pytest.param("- function:\n  name: [\n", "unreadable YAML", id="not_yaml_at_all"),
        pytest.param("get_order: {}\n", "holds dict where", id="a_mapping_where_a_list_belongs"),
        pytest.param(
            "- type: function\n  function: {}\n",
            "records no function.name",
            id="an_entry_naming_no_tool",
        ),
    ],
)
def test_a_bundle_whose_recorded_tool_list_is_unreadable_fails_without_stopping_the_batch(
    tmp_path: Path, recorded: str, said: str
) -> None:
    """An unreadable tool record is that bundle's defect, not the override's.

    The two failure modes meet here and must not be folded together: an override
    the tools refuse stops everything, while a bundle that cannot say which tools
    it had is one named failure the batch replays around. Folding either into
    ``ToolInventory.unresolvable`` would be worse than both — the block would be
    reported as checked against a tool set nothing read.

    Three unreadable shapes, because they are three separate refusals over one
    file and each says something the others do not: broken YAML, a well-formed
    document of the wrong shape, and a well-formed entry standing for no tool.
    """
    broken = _write_bundle(tmp_path / "trials" / "refund_task" / "0")
    healthy = _write_bundle(tmp_path / "trials" / "refund_task" / "1")
    (broken / "tools_schemas.yaml").write_text(recorded, encoding="utf-8")

    failed, replayed = run_trace_replay_batch(
        tmp_path, replay_id="r1", override=_override(tmp_path)
    )

    assert failed.status is TraceReplayOutcomeStatus.FAILED
    assert str(broken / "tools_schemas.yaml") in (failed.reason or "")
    assert said in (failed.reason or "")
    assert replayed.bundle == healthy
    assert replayed.status is TraceReplayOutcomeStatus.REPLAYED
    assert not (tmp_path / TRACE_REPLAY_DIRNAME / "r1" / "trials" / "refund_task" / "0").exists()


@pytest.mark.parametrize(
    ("written", "said"),
    [
        pytest.param(None, "does not exist", id="a_path_nothing_is_at"),
        pytest.param("- constraints\n", "is not a YAML mapping", id="a_list_where_a_block_belongs"),
        pytest.param("constraints: [\n", "is unreadable", id="not_yaml_at_all"),
    ],
)
def test_an_override_file_that_cannot_be_read_says_which_file_and_why(
    tmp_path: Path, written: str | None, said: str
) -> None:
    """Every refusal the loader makes carries the path, including the shape ones.

    Three ways a supplied file is not a constraint block before its contents are
    even in question. Each is told apart from the others, because "is not a YAML
    mapping" for a path nothing is at would send the operator to inspect a file
    that was never there.
    """
    path = tmp_path / "constraints.yaml"
    if written is not None:
        path.write_text(written, encoding="utf-8")

    with pytest.raises(TraceChecksOverrideError) as raised:
        load_trace_checks_override(path)

    assert str(path) in str(raised.value)
    assert said in str(raised.value)


def test_bundles_with_different_recorded_tool_sets_get_their_own_gate_answers(
    tmp_path: Path,
) -> None:
    """The gate runs once per distinct tool set, and each bundle gets *its* answer.

    A source spans tasks, so one batch can hold several inventories. The reuse that
    keeps the gate from running once per trial is keyed on the recorded list itself;
    keyed on anything coarser, every bundle would carry whichever answer was
    computed first, and a bundle whose tools were never resolvable would report the
    findings of one whose tools were.
    """
    writer = FileArtifactWriter()
    resolvable = _write_bundle(tmp_path / "trials" / "refund_task" / "0")
    unresolvable = _write_bundle(tmp_path / "trials" / "refund_task" / "1")
    writer.write_tools_schemas(
        resolvable, [{"type": "function", "function": {"name": "get_order", "parameters": {}}}]
    )

    checked, skipped = run_trace_replay_batch(
        tmp_path, replay_id="r1", override=_override(tmp_path)
    )

    assert (checked.bundle, skipped.bundle) == (resolvable, unresolvable)
    assert checked.override_authoring.unchecked == ()
    assert [skip.where for skip in skipped.override_authoring.unchecked] == ["grading"]
