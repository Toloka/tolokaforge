"""Trace-check replay leaves the run it reads exactly as it found it.

Four claims, over the two bundle populations that matter:

1. **A replay writes only into its own subtree.** Over a single-bundle source —
   the layout where the output lands *inside* ``--source`` by construction — neither
   the per-bundle result nor the run-level report changes or removes a file the
   bundle already held, and the bundle root gains no new path. Both halves are
   asserted because either alone is blind: the digest set cannot see a file created
   under a name nothing held, and the root's file set cannot see one rewritten in
   place.
2. **The schema stamp is evidence, never a gate.** The three ``tau_retail_mini``
   bundles committed under ``tests/data/projects/`` predate the stamp entirely, carry
   no ``tool_log.yaml``, and re-check fine — so a version gate would reject bundles
   whose only defect is age.
3. **A mis-authored override stops the batch before it re-checks anything.** A
   constraint naming a tool the recorded wire list does not carry is one defect in
   one file; replayed, it would arrive as every trial failing that constraint.
4. **A bundle that recorded no tool list leaves the override unchecked, not clean.**
   The same override that aborts against a recorded tool set is admitted against
   those three committed bundles, and the skip carries its reason — so an operator
   never reads "no findings" off a gate that could not run.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tests.canonical._factories import make_trajectory, make_trial_messages
from tests.utils.recorded_calls import recorded_call
from tests.utils.trace_overrides import override_file
from tolokaforge.core.grading.trace_replay import (
    TRACE_CHECKS_RESULT_FILENAME,
    TRACE_REPLAY_DIRNAME,
    TRACE_REPLAY_REPORT_FILENAME,
    TraceChecksOverrideError,
    TraceReplayOutcomeStatus,
    declared_trace_checks,
    discover_trace_bundles,
    emit_trace_replay_report,
    read_trace_replay_inputs,
    run_trace_replay_batch,
)
from tolokaforge.core.grading.trace_timeline import attempted_calls
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import Grade, GradeComponents
from tolokaforge.core.output.artifacts import FileArtifactWriter

pytestmark = [pytest.mark.canonical, pytest.mark.grading]

_RECORDED_RUNS = Path(__file__).resolve().parents[1] / "data" / "projects" / "tau_retail_mini"
# Every call each committed bundle persisted, in discovery order, so a sweep that
# built empty timelines fails instead of passing over them.
_RECORDED_CALL_COUNTS = [3, 6, 5]

_TRACE_CHECKS = {
    "constraints": [
        {
            "id": "the_order_was_looked_up",
            "description": "the agent read something before answering",
            "require": {"present": {"match": {"kind": "tool_call"}}},
        }
    ]
}
# The same shape naming a tool, so the authoring gate's tool-name rule has something
# to decide. ``get_ordr`` is the typo the rule exists to catch and no recorded wire
# list carries it.
_MISSPELLED_TOOL_CHECKS = {
    "constraints": [
        {
            "id": "the_order_was_looked_up",
            "description": "the agent read the order before answering",
            "require": {
                "present": {"match": {"kind": "tool_call", "tool": {"equals": "get_ordr"}}}
            },
        }
    ]
}
# Its argument twin: the tool name is right and the argument is not one the recorded
# closed schema declares, so this row is decided by the inventory's ``parameters``
# where the row above is decided by its ``declared``.
_MISSPELLED_ARGUMENT_CHECKS = {
    "constraints": [
        {
            "id": "the_order_was_looked_up",
            "description": "the agent read the order before answering",
            "require": {
                "present": {
                    "match": {
                        "kind": "tool_call",
                        "tool": {"equals": "get_order"},
                        "args": {"ordr_id": {"equals": "O-1"}},
                    }
                }
            },
        }
    ]
}
# The wire envelope the conductor writes for every provider — ``function.name`` and
# ``function.parameters``, which is where the inventory reads a recorded tool from.
_WIRE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_order",
            "description": "Read one order by id",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
    }
]

_BUNDLE_ARTIFACTS = {
    "task.yaml",
    "trajectory.yaml",
    "tool_log.yaml",
    "env.yaml",
    "metrics.yaml",
    "grade.yaml",
    "logs.yaml",
}


def _digests(root: Path) -> dict[Path, str]:
    return {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _write_graded_bundle(trial_dir: Path) -> Path:
    call = recorded_call(
        "get_order", sequence=0, arguments={"id": "O-1"}, output='{"total": 328.5}'
    )
    trajectory = make_trajectory(
        task_id="refund_task",
        messages=make_trial_messages([call], ("Refund order O-1.", "Reading the order.")),
        tool_log=[call],
    )
    FileArtifactWriter().write_trial_bundle(
        trial_dir,
        trajectory.model_copy(
            update={
                "grade": Grade(
                    binary_pass=True, score=1.0, components=GradeComponents(), reasons=""
                )
            }
        ),
        {"task_id": "refund_task", "grading_config": {"trace_checks": _TRACE_CHECKS}},
        {"orders": [{"id": "O-1"}]},
        StructuredLogger("refund_task-0"),
    )
    return trial_dir


def test_a_replay_changes_no_file_the_bundle_held_and_adds_none_to_its_root(
    tmp_path: Path,
) -> None:
    """The read-only guarantee, over the layout that cannot promise "writes nothing".

    ``--source`` here *is* the bundle, so the output subtree is created inside it by
    construction and a whole-tree comparison is unsatisfiable. What is promised
    instead — and asserted — is that nothing already on disk moved, and that the
    bundle root gained no path outside the subtree the command owns.

    Both artifacts are written before the assertions, the per-bundle result and the
    run-level report, because they are two write paths into the same subtree and the
    guarantee has to hold for the pair. The run-level one lands beside the per-bundle
    directories rather than inside one.
    """
    bundle = _write_graded_bundle(tmp_path / "trials" / "refund_task" / "0")
    before = _digests(bundle)
    root_before = {entry.name for entry in bundle.iterdir()}
    assert root_before == _BUNDLE_ARTIFACTS

    outcomes = run_trace_replay_batch(bundle, replay_id="ro")
    report = emit_trace_replay_report(
        outcomes, declared=declared_trace_checks(outcomes), source=bundle, replay_id="ro"
    )

    assert [outcome.status for outcome in outcomes] == [TraceReplayOutcomeStatus.REPLAYED]
    assert report is not None
    assert [path for path in before if not path.is_file()] == []
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before} == before
    assert {entry.name for entry in bundle.iterdir()} - {TRACE_REPLAY_DIRNAME} == root_before
    assert (
        bundle / TRACE_REPLAY_DIRNAME / "ro" / bundle.name / TRACE_CHECKS_RESULT_FILENAME
    ).is_file()
    assert (bundle / TRACE_REPLAY_DIRNAME / "ro" / TRACE_REPLAY_REPORT_FILENAME).is_file()


def test_the_committed_unstamped_bundles_are_re_checked_rather_than_rejected(
    tmp_path: Path,
) -> None:
    """Age is not a defect: bundles predating the stamp carry ids, so they replay.

    Every one of these is unstamped and record-less, which is what a stamp gate would
    read as "too old to re-check". They validate, build timelines from the message
    view alone, and reach the evaluator — the stamp only tells an operator which
    artifacts to expect. Run dry, because the source is a committed fixture and a
    dry run is also where "nothing is written" is worth asserting.

    Supplied an override rather than reading a recorded block: none of the committed
    runs declares a ``trace_checks`` block, and an override is what makes them
    reachable by the loader at all. Without it the sweep would classify three skips
    and measure nothing.
    """
    source = _RECORDED_RUNS / "output"
    override = override_file(tmp_path, _TRACE_CHECKS)
    bundles = discover_trace_bundles(source)
    outcomes = run_trace_replay_batch(source, replay_id="stamp", override=override, dry_run=True)

    assert len(bundles) == len(_RECORDED_CALL_COUNTS)
    assert [outcome.status for outcome in outcomes] == [TraceReplayOutcomeStatus.WOULD_REPLAY] * 3
    assert [outcome.evidence.schema_version for outcome in outcomes] == [None] * 3
    assert [outcome.evidence.tool_log_present for outcome in outcomes] == [False] * 3

    timelines = [read_trace_replay_inputs(bundle, override=override).timeline for bundle in bundles]
    assert [len(attempted_calls(timeline)) for timeline in timelines] == _RECORDED_CALL_COUNTS
    assert [timeline.records_present for timeline in timelines] == [False] * 3
    assert not (source / TRACE_REPLAY_DIRNAME).exists()


@pytest.mark.parametrize(
    ("block", "misspelling"),
    [
        pytest.param(_MISSPELLED_TOOL_CHECKS, "get_ordr", id="a_tool_the_wire_list_never_declared"),
        pytest.param(
            _MISSPELLED_ARGUMENT_CHECKS, "ordr_id", id="an_argument_its_closed_schema_admits_not"
        ),
    ],
)
def test_an_override_the_recorded_tool_set_refuses_aborts_before_any_replay(
    tmp_path: Path, block: dict, misspelling: str
) -> None:
    """A typo in a supplied constraint file is one defect, reported once.

    The gate runs over every discovered bundle's recorded wire list *before* the
    first trial, so the operator reads the misspelling rather than a corpus of
    trials that all failed a constraint selecting nothing. Two bundles, because the
    claim is that nothing replayed — with one, "aborted before replaying" and
    "replayed and then aborted" are the same observation.

    The two rows read the two halves of a recorded inventory: the tool name comes
    off ``function.name`` and the argument names off ``function.parameters``, so a
    reader that found one and not the other still aborts on one row and admits the
    other.
    """
    writer = FileArtifactWriter()
    bundles = [_write_graded_bundle(tmp_path / "trials" / "refund_task" / str(i)) for i in (0, 1)]
    for bundle in bundles:
        writer.write_tools_schemas(bundle, _WIRE_TOOLS)

    with pytest.raises(TraceChecksOverrideError) as raised:
        run_trace_replay_batch(
            tmp_path,
            replay_id="typo",
            override=override_file(tmp_path / "supplied", block),
        )

    assert misspelling in str(raised.value)
    assert str(tmp_path / "supplied" / "constraints.yaml") in str(raised.value)
    assert not (tmp_path / TRACE_REPLAY_DIRNAME).exists()


def test_a_bundle_that_recorded_no_wire_tool_list_leaves_the_override_unchecked(
    tmp_path: Path,
) -> None:
    """Not knowing the tools is reported as not knowing them, never as nothing wrong.

    The very override that aborts against a recorded wire list is admitted against
    these three committed bundles, which carry no ``tools_schemas.yaml`` at all —
    that asymmetry is the whole claim, and it is why the override names a tool no
    inventory could ever declare. An empty inventory read as authoritative would
    reject all three; an unresolvable one routes the tool-name rule into
    ``unchecked`` and says why, so a gate that could not run never reads as a clean
    bill of health.
    """
    source = _RECORDED_RUNS / "output"
    assert not any(
        (bundle / "tools_schemas.yaml").exists() for bundle in discover_trace_bundles(source)
    )

    outcomes = run_trace_replay_batch(
        source,
        replay_id="unchecked",
        override=override_file(tmp_path, _MISSPELLED_TOOL_CHECKS),
        dry_run=True,
    )

    assert [outcome.status for outcome in outcomes] == [TraceReplayOutcomeStatus.WOULD_REPLAY] * 3
    reports = [outcome.override_authoring for outcome in outcomes]
    assert [report.errors for report in reports] == [()] * 3
    assert [[skip.reason for skip in report.unchecked] for report in reports] == [
        [
            "the tool set of this task could not be resolved, so no tool name and no "
            "argument name in this block is checkable"
        ]
    ] * 3
    assert not (source / TRACE_REPLAY_DIRNAME).exists()
