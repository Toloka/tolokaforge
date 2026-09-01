"""Timeline preflight — grader-side ``build_timeline_from_wire`` from the wire alone.

The runner reconstructs a trial's :class:`TrialTimeline` from two sources:
the wire transcript (``llm_messages_json``) and the runner-owned
``trial_context.recorded`` tool-call records. The grader has no access to
the latter — its only inputs are the wire fields — so
:class:`GraderCompositeDispatch` calls
``build_timeline_from_wire(llm_messages, [], termination_reason)``
with an empty records list.

This suite locks the preflight-probe outcome documented in
``docs/adr/0040-standalone-grader.md``: for a wire that carries no
assistant tool_calls, the records-empty call reconciles without raising
and yields the same event count the same fixture reaches through the
runner-side codepath. If a future change to
:func:`build_timeline_from_wire` breaks that equivalence — a shape where
the records-empty branch diverges — this suite fails before the composite
dispatcher relies on a divergent timeline.
"""

from __future__ import annotations

import json

import pytest

from tolokaforge.core.grading.trace_timeline import (
    TimelineInconsistencyError,
    build_timeline_from_wire,
)

pytestmark = pytest.mark.canonical


_PARITY_FIXTURE_LLM_MESSAGES = [
    {"role": "system", "content": "you are a test assistant"},
    {"role": "user", "content": "please help"},
    {"role": "assistant", "content": "done"},
]


def test_grader_timeline_from_wire_alone_reconciles_without_recorded_calls() -> None:
    """A wire-only build reconciles without raising and yields two events.

    Fixture: a system + user + assistant transcript with no tool calls —
    the shape the parity gate ships. ``build_timeline_from_wire`` with
    ``recorded=[]`` must NOT raise :class:`TimelineInconsistencyError` and
    must produce one event per non-system turn (user + assistant = 2).
    Anything else would mean the grader-side timeline needs a
    ``recorded_calls_json`` wire field before the dispatcher can drop
    ``trial_context.recorded``.
    """
    llm_messages = list(_PARITY_FIXTURE_LLM_MESSAGES)

    try:
        wire_only_timeline = build_timeline_from_wire(llm_messages, [], None)
    except TimelineInconsistencyError as exc:
        pytest.fail(f"grader-side timeline (records=[]) failed reconciliation: {exc!r}.")

    assert [e.kind.value for e in wire_only_timeline.events] == [
        "user_message",
        "assistant_message",
    ]


def test_grader_timeline_from_wire_alone_empty_transcript_is_safe() -> None:
    """An empty transcript builds a records-empty timeline without raising.

    ``GraderCompositeDispatch`` reaches this branch when the client forwards
    an empty ``llm_messages_json`` (a trial the agent never got to run
    that was still graded). The composite skips llm_judge in that case;
    the timeline call must not raise before the skip fires.
    """
    timeline = build_timeline_from_wire([], [], None)
    assert timeline.events == ()


def test_grader_timeline_preserves_decoded_events_from_json() -> None:
    """Round-trip the parity fixture messages through the JSON wire.

    ``dispatch.llm_messages_json`` arrives as a string; the composite
    calls ``json.loads`` before handing the list to
    :func:`build_timeline_from_wire`. Locking the round-trip here surfaces
    any wire-encoding drift the composite would silently mishandle.
    """
    wire = json.dumps(_PARITY_FIXTURE_LLM_MESSAGES)
    llm_messages = json.loads(wire)
    timeline = build_timeline_from_wire(llm_messages, [], None)
    assert len(timeline.events) == 2  # user + assistant, the two non-system turns
