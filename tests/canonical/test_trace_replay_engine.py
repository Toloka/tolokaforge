"""Trace-check replay leaves the run it reads exactly as it found it.

Two claims, over the two bundle populations that matter:

1. **A replay writes only into its own subtree.** Over a single-bundle source —
   the layout where the output lands *inside* ``--source`` by construction — no file
   the bundle already held changes or disappears, and the bundle root gains no new
   path. Both halves are asserted because either alone is blind: the digest set
   cannot see a file created under a name nothing held, and the root's file set
   cannot see one rewritten in place.
2. **The schema stamp is evidence, never a gate.** The three ``tau_retail_mini``
   bundles committed under ``tests/data/projects/`` predate the stamp entirely, carry
   no ``tool_log.yaml``, and re-check fine — so a version gate would reject bundles
   whose only defect is age.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tests.canonical._factories import make_trajectory, make_trial_messages
from tests.utils.recorded_calls import recorded_call
from tolokaforge.core.grading.trace_replay import (
    TRACE_CHECKS_RESULT_FILENAME,
    TRACE_REPLAY_DIRNAME,
    TraceReplayOutcomeStatus,
    discover_trace_bundles,
    read_trace_replay_inputs,
    run_trace_replay_batch,
)
from tolokaforge.core.grading.trace_timeline import attempted_calls
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import Grade, GradeComponents, TraceChecksConfig
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
# Supplied rather than read off the bundles: none of the committed runs declares a
# trace_checks block, and an override is what makes them reachable by the loader at
# all. Without it the sweep would classify three skips and measure nothing.
_OVERRIDE = TraceChecksConfig.model_validate(_TRACE_CHECKS)

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
    """
    bundle = _write_graded_bundle(tmp_path / "trials" / "refund_task" / "0")
    before = _digests(bundle)
    root_before = {entry.name for entry in bundle.iterdir()}
    assert root_before == _BUNDLE_ARTIFACTS

    (outcome,) = run_trace_replay_batch(bundle, replay_id="ro")

    assert outcome.status is TraceReplayOutcomeStatus.REPLAYED
    assert [path for path in before if not path.is_file()] == []
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before} == before
    assert {entry.name for entry in bundle.iterdir()} - {TRACE_REPLAY_DIRNAME} == root_before
    assert (
        bundle / TRACE_REPLAY_DIRNAME / "ro" / bundle.name / TRACE_CHECKS_RESULT_FILENAME
    ).is_file()


def test_the_committed_unstamped_bundles_are_re_checked_rather_than_rejected() -> None:
    """Age is not a defect: bundles predating the stamp carry ids, so they replay.

    Every one of these is unstamped and record-less, which is what a stamp gate would
    read as "too old to re-check". They validate, build timelines from the message
    view alone, and reach the evaluator — the stamp only tells an operator which
    artifacts to expect. Run dry, because the source is a committed fixture and a
    dry run is also where "nothing is written" is worth asserting.
    """
    source = _RECORDED_RUNS / "output"
    bundles = discover_trace_bundles(source)
    outcomes = run_trace_replay_batch(source, replay_id="stamp", override=_OVERRIDE, dry_run=True)

    assert len(bundles) == len(_RECORDED_CALL_COUNTS)
    assert [outcome.status for outcome in outcomes] == [TraceReplayOutcomeStatus.WOULD_REPLAY] * 3
    assert [outcome.evidence.schema_version for outcome in outcomes] == [None] * 3
    assert [outcome.evidence.tool_log_present for outcome in outcomes] == [False] * 3

    timelines = [
        read_trace_replay_inputs(bundle, override=_OVERRIDE).timeline for bundle in bundles
    ]
    assert [len(attempted_calls(timeline)) for timeline in timelines] == _RECORDED_CALL_COUNTS
    assert [timeline.records_present for timeline in timelines] == [False] * 3
    assert not (source / TRACE_REPLAY_DIRNAME).exists()
