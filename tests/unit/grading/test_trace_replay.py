"""Trace-check replay over recorded bundles — discovery, classification, reading.

Locks the contract of :mod:`tolokaforge.core.grading.trace_replay`:

* discovery keys on ``task.yaml`` + ``trajectory.yaml``, so a trial is re-checkable
  whether or not it was ever graded, and never walks either replay command's output;
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
  than as an anonymous validation error;
* a discrimination row is keyed by task *and* constraint id, and a bundle that
  cannot name its task is a named failure rather than a row belonging to nobody;
* agreement with the live run is counted against the *same constraint's* recorded
  verdict, joined by id — never against the trial-level pass, which a trial can fail
  for reasons no one constraint is responsible for;
* the report refuses what it cannot report rather than dropping it: a task measured
  against two different blocks, a ``declared`` mapping from outside the batch, and an
  outcome carrying a result without the provenance of the block it came from;
* the degenerate corpora answer as themselves — one bundle reports the single
  observation its unanimity rests on, and an empty one produces no report at all.

Bundles are written by the real :class:`FileArtifactWriter`, so the tests read the
same on-disk contract the eval flow produces, and every override travels through
the real :func:`load_trace_checks_override`.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.canonical._factories import make_trajectory, make_trial_messages
from tests.utils.recorded_calls import recorded_call
from tests.utils.trace_overrides import override_file
from tolokaforge.core.grading.trace_replay import (
    TRACE_REPLAY_DIRNAME,
    TRACE_REPLAY_REPORT_FILENAME,
    ConstraintDiscrimination,
    ConstraintProvenance,
    TraceChecksOverride,
    TraceChecksOverrideError,
    TraceReplayOutcomeStatus,
    TraceReplayReportError,
    build_trace_replay_report,
    declared_trace_checks,
    discover_trace_bundles,
    emit_trace_replay_report,
    load_trace_checks_override,
    read_trace_replay_inputs,
    run_trace_replay_batch,
)
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    TraceConstraintKind,
    TraceConstraintResult,
    Trajectory,
)
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


def _looked_up_the_order(task_id: str = "refund_task") -> Trajectory:
    call = recorded_call(
        "get_order", sequence=0, arguments={"id": "O-1"}, output='{"total": 328.5}'
    )
    return make_trajectory(
        task_id=task_id, messages=make_trial_messages([call], _TURNS), tool_log=[call]
    )


def _answered_without_calling_anything(task_id: str = "refund_task") -> Trajectory:
    return make_trajectory(task_id=task_id, messages=make_trial_messages([], _TURNS), tool_log=[])


def _recorded_verdict(
    *, passed: bool, undecided: bool = False, constraint_id: str = "the_order_was_looked_up"
) -> TraceConstraintResult:
    """One per-constraint verdict as a live run would have frozen it into ``grade.yaml``."""
    return TraceConstraintResult(
        id=constraint_id,
        kind=TraceConstraintKind.PRESENT,
        passed=passed,
        weight=1.0,
        undecided=undecided,
    )


def _recorded_grade(
    *, binary_pass: bool, constraints: list[TraceConstraintResult] | None = None
) -> Grade:
    """The grade a live run froze: the trial-level pass and the per-constraint verdicts."""
    return Grade(
        binary_pass=binary_pass,
        score=1.0,
        components=GradeComponents(),
        reasons="",
        trace_check_results=constraints or [],
    )


def _write_bundle(
    trial_dir: Path,
    trajectory: Trajectory | None = None,
    *,
    trace_checks: dict[str, Any] | None = _TRACE_CHECKS,
    with_tool_log: bool = True,
    grade: Grade | None = None,
) -> Path:
    """A bundle on disk, written the way the eval flow writes one.

    ``with_tool_log=False`` is the shape of a bundle written before the record was
    persisted: every other artifact present, the sidecar absent. ``grade`` writes what
    a live run would have frozen — the per-constraint verdicts the report joins the
    recomputation to, and the trial-level pass beside them; without one the bundle is
    an ungraded trial, which is still re-checkable.
    """
    trajectory = trajectory if trajectory is not None else _looked_up_the_order()
    writer = FileArtifactWriter()
    writer.write_trajectory(trial_dir, trajectory)
    if with_tool_log:
        writer.write_tool_log(trial_dir, trajectory)
    writer.write_metrics(trial_dir, trajectory)
    if grade is not None:
        writer.write_grade(trial_dir, grade)
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


@pytest.mark.parametrize(
    "nested_under",
    [
        pytest.param(Path("trace_replay") / "earlier", id="this_commands_own_output_at_the_top"),
        pytest.param(Path("replays") / "earlier", id="judge_replays_output_at_the_top"),
        pytest.param(Path("trials") / "replays" / "earlier", id="judge_replays_output_nested"),
        pytest.param(
            Path("trials") / "trace_replay" / "earlier", id="this_commands_own_output_nested"
        ),
    ],
)
def test_a_bundle_under_a_reserved_directory_is_not_discovered(
    tmp_path: Path, nested_under: Path
) -> None:
    """Two directory names are reserved anywhere under a source, at any depth.

    ``trace_replay/`` is this command's output and ``replays/`` is judge replay's, and
    a source re-pointed at a run that already holds either would otherwise re-check
    what sits under it — for ``replays/`` that means grading a *replayed* bundle as
    though it were a trial. The names are written out here rather than imported:
    reserving a name is a claim about the string, and a test that read it off the
    module could not tell a renamed constant from a widened rule.

    At any depth, because a previously-replayed subtree can be nested arbitrarily
    under whatever the operator points ``--source`` at. The deliberate cost is that a
    *task* named ``replays`` would hide its own trials, which is why both names are
    documented as reserved rather than left to be discovered.
    """
    live = _write_bundle(tmp_path / "trials" / "refund_task" / "0")
    _write_bundle(tmp_path / nested_under / "refund_task" / "0")

    assert discover_trace_bundles(tmp_path) == [live]


def test_each_bundles_task_file_is_parsed_once_for_the_whole_re_check(
    tmp_path: Path,
) -> None:
    """The eligibility, the constraint block and the task id are three readers, one parse.

    ``task.yaml`` carries the whole ``grading_config``, so parsing it is not free — and
    it dominates a re-check whose evaluation costs well under a millisecond. Each
    reader therefore takes the mapping rather than the path. Asserted as "read once per
    bundle" rather than as a duration, because a wall-clock threshold on a machine
    nobody controls is a flake, while the number of times a file is opened is exact.
    """
    bundles = [_write_bundle(tmp_path / "trials" / "refund_task" / str(index)) for index in (0, 1)]
    reads: list[str] = []
    unpatched = Path.read_text

    def counting(self: Path, *args: Any, **kwargs: Any) -> str:
        reads.append(str(self))
        return unpatched(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(Path, "read_text", counting)
        outcomes = run_trace_replay_batch(tmp_path, replay_id="r1")

    assert [outcome.status for outcome in outcomes] == [TraceReplayOutcomeStatus.REPLAYED] * 2
    assert [reads.count(str(bundle / "task.yaml")) for bundle in bundles] == [1, 1]


def test_a_bundle_declaring_no_trace_checks_is_skipped_until_an_override_supplies_them(
    tmp_path: Path,
) -> None:
    """The skip is declared, and an override is what makes the same bundle eligible."""
    _write_bundle(tmp_path / "trials" / "refund_task" / "0", trace_checks=None)

    (skipped,) = run_trace_replay_batch(tmp_path, replay_id="r1")
    assert skipped.status is TraceReplayOutcomeStatus.SKIPPED_NOT_APPLICABLE
    assert skipped.result is None
    assert not (tmp_path / TRACE_REPLAY_DIRNAME).exists()

    (replayed,) = run_trace_replay_batch(
        tmp_path, replay_id="r2", override=override_file(tmp_path, _TRACE_CHECKS)
    )
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

    override = override_file(tmp_path, _TRACE_CHECKS)
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


@pytest.mark.parametrize(
    "artifact", ["task.yaml", "trajectory.yaml", "metrics.yaml", "tools_schemas.yaml", "grade.yaml"]
)
def test_a_bundle_file_that_is_not_utf_8_fails_its_own_bundle_only(
    tmp_path: Path, artifact: str
) -> None:
    """Bytes no decoder accepts are one bundle's damage, whichever artifact carries them.

    The per-bundle net catches this module's own refusal and nothing else, so a
    ``UnicodeDecodeError`` raised straight out of ``read_text`` walks past it and takes every
    trial after this one with it. Swept over each artifact rather than asserted on one,
    because the reads sit in five places and a net over four of them is a batch that still
    aborts.
    """
    broken = _write_bundle(
        tmp_path / "trials" / "refund_task" / "0", grade=_recorded_grade(binary_pass=True)
    )
    healthy = _write_bundle(tmp_path / "trials" / "refund_task" / "1")
    (broken / artifact).write_bytes(b"\xff\xfe\x00not utf-8")

    failed, replayed = run_trace_replay_batch(
        tmp_path, replay_id="r1", override=override_file(tmp_path, _TRACE_CHECKS)
    )

    assert failed.status is TraceReplayOutcomeStatus.FAILED
    assert str(broken / artifact) in (failed.reason or "")
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
    document = override_file(tmp_path / "doc", {"trace_checks": _TRACE_CHECKS})
    bare = override_file(tmp_path / "bare", _TRACE_CHECKS)

    assert document.block == bare.block == _TRACE_CHECKS
    assert document.config == bare.config
    assert [item.id for item in bare.config.constraints] == ["the_order_was_looked_up"]


def test_an_override_holds_its_own_copy_of_the_block_it_validated(tmp_path: Path) -> None:
    """The authored block and the config derived from it cannot be made to disagree.

    ``config`` is derived once, at construction, from the mapping handed in — so a
    caller that goes on mutating that mapping would leave the override reporting a
    block the authoring gate never validated, and the gate addresses its findings by
    where the operator wrote them. The block is copied behind a read-only view, so the
    mutation lands nowhere and the view itself refuses one.
    """
    authored: dict[str, Any] = {"constraints": list(_TRACE_CHECKS["constraints"])}
    override = TraceChecksOverride(path=tmp_path / "constraints.yaml", block=authored)

    authored["constraints"] = []
    authored["alternatives"] = []

    assert override.block == _TRACE_CHECKS
    assert [item.id for item in override.config.constraints] == ["the_order_was_looked_up"]
    with pytest.raises(TypeError):
        override.block["constraints"] = []  # type: ignore[index]


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
        tmp_path, replay_id="r1", override=override_file(tmp_path, _TRACE_CHECKS)
    )

    assert failed.status is TraceReplayOutcomeStatus.FAILED
    assert str(broken / "tools_schemas.yaml") in (failed.reason or "")
    assert said in (failed.reason or "")
    assert replayed.bundle == healthy
    assert replayed.status is TraceReplayOutcomeStatus.REPLAYED
    assert not (tmp_path / TRACE_REPLAY_DIRNAME / "r1" / "trials" / "refund_task" / "0").exists()


def _dumped(payload: Any) -> str:
    """A fixture file's text, built from the payload rather than typed as one.

    Indentation typed into a YAML string mis-nests silently and would make a
    rejection test pass for the wrong shape.
    """
    return yaml.safe_dump(payload, sort_keys=False)


@pytest.mark.parametrize(
    ("filename", "written", "said"),
    [
        pytest.param(
            "tool_log.yaml",
            "- call_id: a\n  arguments: {\n",
            "unreadable YAML",
            id="truncated_record",
        ),
        pytest.param(
            "tool_log.yaml",
            _dumped({"call_id": "a"}),
            "must be a YAML list of recorded calls",
            id="a_record_that_is_not_a_list",
        ),
        pytest.param(
            "grade.yaml",
            _dumped([{"binary_pass": True}]),
            "holds list where a mapping belongs",
            id="a_grade_that_is_not_a_mapping",
        ),
        pytest.param(
            "grade.yaml",
            _dumped({"binary_pass": True, "trace_check_results": 5}),
            "holds int where the live run's per-constraint verdicts belong",
            id="per_constraint_verdicts_that_are_not_a_list",
        ),
        pytest.param(
            "grade.yaml",
            _dumped(
                {
                    "binary_pass": True,
                    "trace_check_results": [
                        {
                            "id": "the_order_was_looked_up",
                            "kind": "present",
                            "passed": True,
                            "weight": 1.0,
                            "a_key_this_reader_cannot_account_for": 1,
                        }
                    ],
                }
            ),
            "records trace-check verdicts that do not validate",
            id="a_verdict_carrying_an_unaccountable_key",
        ),
        pytest.param(
            "metrics.yaml",
            _dumped([{"schema_version": TRIAL_BUNDLE_SCHEMA_VERSION}]),
            "holds list where a mapping belongs",
            id="metrics_that_are_not_a_mapping",
        ),
    ],
)
def test_a_bundle_input_the_reader_cannot_account_for_fails_only_that_bundle(
    tmp_path: Path, filename: str, written: str, said: str
) -> None:
    """Every corrupt input is one named failure, and never the batch's last word.

    Six shapes over three of a bundle's files, because each is a state the reader has
    to keep apart from a state it *is* allowed to read as absent. A missing
    ``tool_log.yaml`` is a record-less bundle and a missing ``grade.yaml`` is an
    ungraded trial — both fine — so a file that is present and unreadable must refuse
    rather than fall through to that reading: it would otherwise report a truncated
    record as a bundle written before records existed, a corrupt grade as a trial
    nobody graded, and an unreadable stamp as an unstamped one.

    The verdict rows are strict on purpose. The recorded verdicts are one of the two
    sources the report joins by constraint id, and they are read through the very
    model that wrote them — so a verdict list that is not a list, and an entry
    carrying a key that model does not declare, are both refused. Reading them more
    loosely than they were written would make the reader a second, weaker schema for
    the harness's own artifact.

    The batch runs on either way: the healthy bundle beside each broken one is
    re-checked, and the broken one's output directory is never created.
    """
    broken = _write_bundle(tmp_path / "trials" / "refund_task" / "0")
    healthy = _write_bundle(tmp_path / "trials" / "refund_task" / "1")
    (broken / filename).write_text(written, encoding="utf-8")

    failed, replayed = run_trace_replay_batch(tmp_path, replay_id="r1")

    assert failed.status is TraceReplayOutcomeStatus.FAILED
    assert str(broken / filename) in (failed.reason or "")
    assert said in (failed.reason or "")
    assert (replayed.bundle, replayed.status) == (healthy, TraceReplayOutcomeStatus.REPLAYED)
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
        tmp_path, replay_id="r1", override=override_file(tmp_path, _TRACE_CHECKS)
    )

    assert (checked.bundle, skipped.bundle) == (resolvable, unresolvable)
    assert checked.override_authoring.unchecked == ()
    assert [skip.where for skip in skipped.override_authoring.unchecked] == ["grading"]


def _report(source: Path, *, replay_id: str = "r1"):
    """A batch over *source* and the report built off it, the way the command will."""
    outcomes = run_trace_replay_batch(source, replay_id=replay_id)
    return outcomes, build_trace_replay_report(
        outcomes,
        declared=declared_trace_checks(outcomes),
        source=source,
        replay_id=replay_id,
    )


def test_two_packs_reusing_one_constraint_id_get_a_row_each(tmp_path: Path) -> None:
    """A discrimination row is keyed by task and id, because an id is unique per pack.

    Both bundles declare ``the_order_was_looked_up`` and reach opposite verdicts on
    it. Keyed on the id alone the two fold into one row reading ``DISCRIMINATING``
    over a mix of two predicates and two corpora — and every pack's constraints
    would additionally read as unmeasured on every other pack's trials. Keyed
    properly, each pack is told about its own corpus.
    """
    _write_bundle(tmp_path / "trials" / "refund_task" / "0", _looked_up_the_order())
    _write_bundle(
        tmp_path / "trials" / "status_task" / "0",
        _answered_without_calling_anything("status_task"),
    )

    _, report = _report(tmp_path)

    assert report is not None
    assert [(row.task_id, row.constraint_id, row.verdict) for row in report.discrimination] == [
        ("refund_task", "the_order_was_looked_up", ConstraintDiscrimination.ALWAYS_TRUE),
        ("status_task", "the_order_was_looked_up", ConstraintDiscrimination.ALWAYS_FALSE),
    ]
    assert [row.trials_evaluated for row in report.discrimination] == [1, 1]


def test_a_one_bundle_corpus_reports_the_single_observation_its_verdict_rests_on(
    tmp_path: Path,
) -> None:
    """Standing single case at the smallest corpus that measures anything.

    ``ALWAYS_TRUE`` over one trial is a far weaker claim than over twenty, and the
    counts beside the verdict are the only thing that says which one it is — so they
    are asserted rather than the verdict alone. The recorded verdict is counted here
    too: one labelled trial, agreeing with the recomputation.
    """
    _write_bundle(
        tmp_path / "trials" / "refund_task" / "0",
        grade=_recorded_grade(binary_pass=True, constraints=[_recorded_verdict(passed=True)]),
    )

    outcomes = run_trace_replay_batch(tmp_path, replay_id="r1")
    report = emit_trace_replay_report(
        outcomes, declared=declared_trace_checks(outcomes), source=tmp_path, replay_id="r1"
    )

    assert report is not None
    (row,) = report.discrimination
    assert row.verdict is ConstraintDiscrimination.ALWAYS_TRUE
    assert (row.trials_evaluated, row.trials_decided, row.passed_trials) == (1, 1, 1)
    assert (row.trials_labelled, row.agreed_with_recorded_pass) == (1, 1)
    assert (report.evidence.bundles_read, report.evidence.bundles_with_tool_log) == (1, 1)
    assert report.evidence.schema_versions == {str(TRIAL_BUNDLE_SCHEMA_VERSION): 1}
    assert (tmp_path / TRACE_REPLAY_DIRNAME / "r1" / TRACE_REPLAY_REPORT_FILENAME).is_file()


def test_agreement_is_counted_against_the_recorded_verdict_of_the_same_constraint(
    tmp_path: Path,
) -> None:
    """The two sources compared are one constraint's verdict, recomputed and recorded.

    Four bundles that all satisfy the constraint, so the recomputation is ``passed``
    on every one and the only thing the agreement count can vary with is what each
    bundle *recorded* for that constraint. Every trial-level ``binary_pass`` is
    ``False`` — the trials failed for reasons beyond this constraint, which is the
    ordinary case — so a count taken against the trial verdict instead reads two
    agreements as zero and cannot produce the number below.

    Three of the four are labelled: the fourth recorded the constraint *undecided*,
    which is ``passed: false`` on the wire, and counting that as a recorded failure
    would report the live run's missing evidence as a disagreement. Of the three, one
    recorded a failure the re-check contradicts, so the count is short of the
    denominator and a builder that assumed agreement is caught too.
    """
    recorded = [
        _recorded_grade(binary_pass=False, constraints=[_recorded_verdict(passed=True)]),
        _recorded_grade(binary_pass=False, constraints=[_recorded_verdict(passed=True)]),
        _recorded_grade(binary_pass=False, constraints=[_recorded_verdict(passed=False)]),
        _recorded_grade(
            binary_pass=False, constraints=[_recorded_verdict(passed=False, undecided=True)]
        ),
    ]
    for index, grade in enumerate(recorded):
        _write_bundle(tmp_path / "trials" / "refund_task" / str(index), grade=grade)

    _, report = _report(tmp_path)

    assert report is not None
    (row,) = report.discrimination
    assert row.verdict is ConstraintDiscrimination.ALWAYS_TRUE
    assert (row.trials_evaluated, row.trials_decided, row.passed_trials) == (4, 4, 4)
    assert (row.trials_labelled, row.agreed_with_recorded_pass) == (3, 2)
    assert [trial.recorded_binary_pass for trial in report.trials] == [False] * 4


def test_a_bundle_recording_no_verdict_for_the_constraint_is_not_labelled(
    tmp_path: Path,
) -> None:
    """Standing single case: nothing recorded is nothing to agree with.

    An ungraded trial and a trial re-checked against a constraint its pack never had
    are the same state from the report's side — the bundle holds no verdict for this
    id — and both are re-checkable. Neither may enter the agreement denominator,
    which would otherwise silently compare the recomputation against a default.
    """
    _write_bundle(tmp_path / "trials" / "refund_task" / "0")
    _write_bundle(
        tmp_path / "trials" / "refund_task" / "1",
        grade=_recorded_grade(
            binary_pass=True,
            constraints=[
                _recorded_verdict(passed=True, constraint_id="a_constraint_since_renamed")
            ],
        ),
    )

    _, report = _report(tmp_path)

    assert report is not None
    (row,) = report.discrimination
    assert (row.trials_evaluated, row.trials_decided) == (2, 2)
    assert (row.trials_labelled, row.agreed_with_recorded_pass) == (0, 0)
    assert [trial.recorded_binary_pass for trial in report.trials] == [None, True]


def test_an_empty_corpus_produces_no_report_and_writes_nothing(tmp_path: Path) -> None:
    """Standing single case at the degenerate boundary: nothing found, nothing claimed.

    A selector matching no bundle validates nothing, so there is no report — and the
    caller's non-zero exit rests on that, never on an empty table that would read as
    a corpus in which every constraint behaved.
    """
    outcomes = run_trace_replay_batch(tmp_path, replay_id="r1")

    assert outcomes == []
    assert emit_trace_replay_report(outcomes, declared={}, source=tmp_path, replay_id="r1") is None
    assert not (tmp_path / TRACE_REPLAY_DIRNAME).exists()


def test_one_task_measured_against_two_blocks_is_refused_rather_than_folded(
    tmp_path: Path,
) -> None:
    """A row is one claim about one block, so two revisions of a pack cannot share it.

    Both bundles name ``refund_task`` and both declare ``the_order_was_looked_up``, but
    one requires the order specifically and the other any call at all. Folded into one
    row the counts would mix two predicates, and the row could read
    ``DISCRIMINATING`` off the difference between the *blocks* rather than between the
    trials — the corpus-spanning form of the defect keying rows on the id alone
    creates, which :func:`test_two_packs_reusing_one_constraint_id_get_a_row_each`
    locks the other half of.

    The message has to name a way out, because the operator's corpus is not wrong —
    it just spans revisions, and ``--constraints`` measures one block over all of it.
    """
    _write_bundle(tmp_path / "trials" / "refund_task" / "0")
    _write_bundle(
        tmp_path / "trials" / "refund_task" / "1",
        trace_checks={
            "constraints": [
                {
                    "id": "the_order_was_looked_up",
                    "description": "the agent called something",
                    "require": {"present": {"match": {"kind": "tool_call"}}},
                }
            ]
        },
    )

    with pytest.raises(TraceReplayReportError) as raised:
        _report(tmp_path)

    assert "a different trace_checks block" in str(raised.value)
    assert "refund_task" in str(raised.value)
    assert "--constraints" in str(raised.value)


@pytest.mark.parametrize(
    ("declared_from", "said"),
    [
        pytest.param(dict, "no constraint block for", id="a_bundle_the_mapping_omits"),
        pytest.param(
            "unrelated", "does not declare", id="a_block_that_declares_another_constraint"
        ),
    ],
)
def test_a_declared_mapping_from_outside_the_batch_is_refused_by_name(
    tmp_path: Path, declared_from: object, said: str
) -> None:
    """``declared`` is derivable, and the one supported way to build it is named.

    It stays a parameter because the signature is frozen, so a caller *can* hand the
    builder a mapping that did not come from this batch — an empty one, or one whose
    block declares different constraints. Both used to be a bare ``KeyError`` out of a
    published function, which tells the caller nothing about where the mapping should
    have come from.
    """
    bundle = _write_bundle(tmp_path / "trials" / "refund_task" / "0")
    outcomes = run_trace_replay_batch(tmp_path, replay_id="r1")
    declared = (
        {}
        if declared_from is dict
        else {bundle: override_file(tmp_path / "other", _STATUS_TRACE_CHECKS).config}
    )

    with pytest.raises(TraceReplayReportError) as raised:
        build_trace_replay_report(outcomes, declared=declared, source=tmp_path, replay_id="r1")

    assert said in str(raised.value)
    assert "declared_trace_checks(outcomes)" in str(raised.value)


def test_a_bundle_naming_no_task_fails_rather_than_folding_into_another_pack(
    tmp_path: Path,
) -> None:
    """An unattributable verdict is a named failure, never a row under no task.

    Constraint ids are unique only inside one pack's block, so a verdict with no task
    to key on would land wherever another pack reused the id. The bundle is named and
    the batch replays around it.
    """
    broken = _write_bundle(tmp_path / "trials" / "refund_task" / "0")
    healthy = _write_bundle(tmp_path / "trials" / "refund_task" / "1")
    task = yaml.safe_load((broken / "task.yaml").read_text(encoding="utf-8"))
    del task["task_id"]
    _rewrite_yaml(broken / "task.yaml", task)

    failed, replayed = run_trace_replay_batch(tmp_path, replay_id="r1")

    assert failed.status is TraceReplayOutcomeStatus.FAILED
    assert str(broken / "task.yaml") in (failed.reason or "")
    assert "task_id" in (failed.reason or "")
    assert (replayed.bundle, replayed.status) == (healthy, TraceReplayOutcomeStatus.REPLAYED)


def test_a_report_names_the_block_each_trial_was_measured_against(tmp_path: Path) -> None:
    """Provenance is a per-trial fact, because an override re-checks every bundle.

    One bundle declares a block and the other declares none, and a supplied override
    makes both re-checkable — so the run cannot say "these came from the packs" or
    "these came from a file" without saying it per trial. Reported off the outcome
    rather than inferred from the run having had an override at all.
    """
    _write_bundle(tmp_path / "trials" / "refund_task" / "0")
    _write_bundle(tmp_path / "trials" / "refund_task" / "1", trace_checks=None)
    override = override_file(tmp_path / "supplied", _TRACE_CHECKS)

    for supplied, expected in (
        (None, [ConstraintProvenance.RECORDED]),
        (override, [ConstraintProvenance.OVERRIDE] * 2),
    ):
        outcomes = run_trace_replay_batch(tmp_path, replay_id="r1", override=supplied)
        report = build_trace_replay_report(
            outcomes,
            declared=declared_trace_checks(outcomes),
            source=tmp_path,
            replay_id="r1",
        )
        assert report is not None
        assert [trial.provenance for trial in report.trials] == expected


def test_a_result_without_its_provenance_is_refused_rather_than_dropped(tmp_path: Path) -> None:
    """Standing single case: a trial the report cannot describe must not just vanish.

    A result is written by the one path that also resolves the block's provenance and
    reads the bundle's evidence, so an outcome carrying one without the others did not
    come from that path. Filtering it out instead would leave the batch's size
    unaccountable — the trial would be neither reported nor reported missing — which is
    the failure mode the dispositions exist to prevent.
    """
    _write_bundle(tmp_path / "trials" / "refund_task" / "0")

    (outcome,) = run_trace_replay_batch(tmp_path, replay_id="r1")
    stripped = replace(outcome, provenance=None)

    with pytest.raises(TraceReplayReportError) as raised:
        build_trace_replay_report(
            [stripped],
            declared=declared_trace_checks([stripped]),
            source=tmp_path,
            replay_id="r1",
        )

    assert str(outcome.bundle) in str(raised.value)
    assert "provenance" in str(raised.value)


def test_an_unreadable_recorded_pass_fails_rather_than_reading_as_ungraded(
    tmp_path: Path,
) -> None:
    """An ungraded trial and an unreadable verdict are different states.

    Absent means the trial was never graded, which says nothing about its replay;
    unreadable means the bundle cannot say what the live run concluded. Reading the
    second as the first would drop a trial out of the agreement denominator without
    saying so, and agreement is the one number in the report that has two sources.
    """
    bundle = _write_bundle(
        tmp_path / "trials" / "refund_task" / "0", grade=_recorded_grade(binary_pass=True)
    )
    grade = yaml.safe_load((bundle / "grade.yaml").read_text(encoding="utf-8"))
    grade["binary_pass"] = "yes"
    _rewrite_yaml(bundle / "grade.yaml", grade)

    (outcome,) = run_trace_replay_batch(tmp_path, replay_id="r1")

    assert outcome.status is TraceReplayOutcomeStatus.FAILED
    assert str(bundle / "grade.yaml") in (outcome.reason or "")
    assert "binary_pass" in (outcome.reason or "")
