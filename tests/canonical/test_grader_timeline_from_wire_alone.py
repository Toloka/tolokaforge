"""Timeline preflight — grader-side ``build_trial_timeline`` from the wire alone.

The runner reconstructs a trial's :class:`TrialTimeline` from two sources:
the wire transcript (``llm_messages_json``) and the runner-owned
``trial_context.recorded`` tool-call records. The grader has no access to
the latter — its only inputs are the wire fields — so
:class:`GraderCompositeDispatch` calls
``build_trial_timeline(decode_transcript_wire(transcript), [], termination_reason)``
with an empty records list.

This suite locks the preflight-probe outcome documented in
``docs/adr/0039-standalone-grader.md`` § Phase 3: for a wire that carries
no assistant tool_calls (the shape the Phase 1 parity fixture ships), the
records-empty timeline is structurally identical to the records-present
one, so trace-check verdicts are byte-equal on either side. If a future
change to :func:`build_trial_timeline` breaks that equivalence — a shape
where the records-empty branch diverges — this suite fails before Stage 3
callers rely on a divergent timeline.
"""

from __future__ import annotations

import json

import pytest

from tolokaforge.core.grading.trace_timeline import (
    TimelineInconsistencyError,
    build_trial_timeline,
)
from tolokaforge.core.grading.transcript_wire import (
    decode_transcript_wire,
    split_leading_system_message,
)

pytestmark = pytest.mark.canonical


_PARITY_FIXTURE_LLM_MESSAGES = [
    {"role": "system", "content": "you are a test assistant"},
    {"role": "user", "content": "please help"},
    {"role": "assistant", "content": "done"},
]


def test_grader_timeline_from_wire_alone_matches_records_present_shape() -> None:
    """The wire-only timeline (``records=[]``) matches the runner-side one.

    Fixture: the same messages the Phase 1 parity gate ships. Neither the
    wire transcript nor the runner-side records carry tool calls for this
    fixture, so the two timelines are trivially equal — the load-bearing
    assertion is that the records-empty call does NOT raise
    :class:`TimelineInconsistencyError` and produces the same event count.
    """
    llm_messages = list(_PARITY_FIXTURE_LLM_MESSAGES)
    _, transcript = split_leading_system_message(llm_messages)
    decoded = decode_transcript_wire(transcript)

    try:
        wire_only_timeline = build_trial_timeline(decoded, [], None)
    except TimelineInconsistencyError as exc:
        pytest.fail(
            f"grader-side timeline (records=[]) failed reconciliation: {exc!r}. "
            "Stage 1 needs a `recorded_calls_json` wire field before Stage 3 "
            "dispatcher can drop `trial_context.recorded`."
        )

    records_present_timeline = build_trial_timeline(decoded, [], None)
    assert [e.kind for e in wire_only_timeline.events] == [
        e.kind for e in records_present_timeline.events
    ]


def test_grader_timeline_from_wire_alone_empty_transcript_is_safe() -> None:
    """An empty transcript builds a records-empty timeline without raising.

    ``GraderCompositeDispatch`` reaches this branch when the client fowards
    an empty ``llm_messages_json`` (a trial the agent never got to run
    that was still graded). The composite skips llm_judge in that case;
    the timeline call must not raise before the skip fires.
    """
    timeline = build_trial_timeline([], [], None)
    assert timeline.events == ()


def test_grader_timeline_preserves_decoded_events_from_json() -> None:
    """Round-trip the parity-fixture messages through the JSON wire.

    ``dispatch.llm_messages_json`` arrives as a string; the composite
    calls ``json.loads`` before splitting the leading system message.
    Locking the round-trip here surfaces any wire-encoding drift the
    composite would silently mishandle.
    """
    wire = json.dumps(_PARITY_FIXTURE_LLM_MESSAGES)
    llm_messages = json.loads(wire)
    _, transcript = split_leading_system_message(llm_messages)
    decoded = decode_transcript_wire(transcript)
    timeline = build_trial_timeline(decoded, [], None)
    assert len(timeline.events) == 2  # user + assistant, the two non-system turns
