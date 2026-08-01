"""How a quantifier reads a side, and which way a side pulls the verdict.

Three claims are locked here, each against a trial the real builder produced.

**The sixteen readings.** ``before`` and ``immediately_before`` each carry a
quantifier per side, so each has a sixteen-cell answer table whose key set is
asserted equal to ``Quantifier × Quantifier``. ``before``'s cells are cross-checked
against the min/max closed form computed independently in the test; adjacency has
no such closed form, so its fixture's adjacent pairs are asserted instead and every
cell is readable off them.

**The window anchors.** ``absent_before``'s anchor and ``absent_between``'s
``start × end`` select demonstrably different windows, tabled over the restricted
``{first, last}`` domain.

**Polarity.** Which way each ``(kind, position, quantifier)`` pulls is what makes
three-valued evaluation sound: reading it wrongly returns a definite verdict where
indeterminate is correct. The table below is re-derived by perturbation — every
trajectory over the kind's own tools, one extra event inserted at every place it
could go — rather than trusted. Polarity is a property of the *quantified* domain,
so the sweep is over trajectories where every side matched something: a side that
matched nothing never reaches a quantifier at all, and ``on_missing`` decides it.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from enum import Enum
from functools import cache
from itertools import product
from typing import Any, get_args

import pytest
from pydantic import BaseModel

from tests.utils.recorded_calls import recorded_call
from tests.utils.timelines import Turn, build_timeline, build_turn_timeline
from tests.utils.trace_constraints import evaluate_constraint
from tolokaforge.core.grading.trace_timeline import TraceEventKind, TrialTimeline
from tolokaforge.core.models import (
    AnchorQuantifier,
    AnchorSide,
    MatcherSide,
    Quantifier,
    ToolCall,
    TraceConstraintExpr,
    TraceMatcher,
)
from tolokaforge.runner.models import TRACE_CONSTRAINT_KINDS

pytestmark = pytest.mark.unit

_LEFT = "left_tool"
_RIGHT = "right_tool"


def _matches(tool: str) -> dict[str, Any]:
    return {"kind": "tool_call", "tool": {"equals": tool}}


def _side(quantifier: str, tool: str) -> dict[str, Any]:
    return {"quantifier": quantifier, "match": _matches(tool)}


@cache
def _timeline_of(tools: tuple[str, ...]) -> TrialTimeline:
    """One assistant turn that called ``tools`` in order, every call recorded."""
    return build_timeline(
        turns=(("user", "Do the work."), ("assistant", "Working.")),
        recorded=[recorded_call(tool, sequence=index) for index, tool in enumerate(tools)],
    )


def _positions_of(timeline: TrialTimeline, tool: str) -> list[int]:
    return [
        event.position
        for event in timeline.events
        if event.kind is TraceEventKind.TOOL_CALL and event.tool_name == tool
    ]


# Two calls to each side, interleaved, so no cell is decided by a side holding one
# event: left sits at the first and third call, right at the second and fourth.
_INTERLEAVED = (_LEFT, _RIGHT, _LEFT, _RIGHT)

# What ``before`` says for each pair of quantifiers over that trajectory, written
# out rather than computed, and cross-checked below against the closed form.
_BEFORE_TABLE: dict[tuple[str, str], bool] = {
    ("any", "any"): True,
    ("any", "all"): True,
    ("any", "first"): True,
    ("any", "last"): True,
    ("first", "any"): True,
    ("first", "all"): True,
    ("first", "first"): True,
    ("first", "last"): True,
    ("all", "any"): True,
    ("all", "all"): False,
    ("all", "first"): False,
    ("all", "last"): True,
    ("last", "any"): True,
    ("last", "all"): False,
    ("last", "first"): False,
    ("last", "last"): True,
}

# The same trajectory read as adjacency in the ``tool_calls`` view, where the four
# calls are consecutive: only the two same-turn pairs are adjacent, so eight of the
# sixteen readings fail where ordering succeeds.
_ADJACENCY_TABLE: dict[tuple[str, str], bool] = {
    ("any", "any"): True,
    ("any", "all"): False,
    ("any", "first"): True,
    ("any", "last"): True,
    ("first", "any"): True,
    ("first", "all"): False,
    ("first", "first"): True,
    ("first", "last"): False,
    ("all", "any"): True,
    ("all", "all"): False,
    ("all", "first"): False,
    ("all", "last"): False,
    ("last", "any"): True,
    ("last", "all"): False,
    ("last", "first"): False,
    ("last", "last"): True,
}

_QUANTIFIER_PAIRS = {(left.value, right.value) for left in Quantifier for right in Quantifier}


def test_both_ordering_tables_span_the_quantifier_product():
    assert set(_BEFORE_TABLE) == _QUANTIFIER_PAIRS
    assert set(_ADJACENCY_TABLE) == _QUANTIFIER_PAIRS
    disagreeing = {
        pair for pair in _QUANTIFIER_PAIRS if _BEFORE_TABLE[pair] != _ADJACENCY_TABLE[pair]
    }
    assert sorted(disagreeing) == [
        ("all", "last"),
        ("any", "all"),
        ("first", "all"),
        ("first", "last"),
    ], (
        "ordering and adjacency would be read by one table if they never disagreed, so "
        "the fixture has to separate them, and these are the cells where it does"
    )


def _extreme(positions: Sequence[int], quantifier: str, *, on_the_left: bool) -> int:
    """The position the closed form compares: small helps on the left, large on the right."""
    earliest = quantifier in ({"any", "first"} if on_the_left else {"all", "first"})
    return min(positions) if earliest else max(positions)


@pytest.mark.parametrize(("left", "right"), sorted(_QUANTIFIER_PAIRS))
def test_before_reads_each_quantifier_pair_as_the_closed_form_does(left: str, right: str):
    timeline = _timeline_of(_INTERLEAVED)
    lefts = _positions_of(timeline, _LEFT)
    rights = _positions_of(timeline, _RIGHT)

    verdict = evaluate_constraint(
        timeline, {"before": {"left": _side(left, _LEFT), "right": _side(right, _RIGHT)}}
    ).passed

    assert (lefts, rights) == ([2, 6], [4, 8])
    assert verdict is _BEFORE_TABLE[(left, right)]
    assert verdict is (
        _extreme(lefts, left, on_the_left=True) < _extreme(rights, right, on_the_left=False)
    )


@pytest.mark.parametrize(("left", "right"), sorted(_QUANTIFIER_PAIRS))
def test_immediately_before_reads_each_quantifier_pair_over_the_view(left: str, right: str):
    timeline = _timeline_of(_INTERLEAVED)
    calls = [event.position for event in timeline.events if event.kind is TraceEventKind.TOOL_CALL]

    verdict = evaluate_constraint(
        timeline,
        {
            "immediately_before": {
                "left": _side(left, _LEFT),
                "right": _side(right, _RIGHT),
                "among": "tool_calls",
            }
        },
    ).passed

    assert calls == [2, 4, 6, 8]
    assert [(calls[index], calls[index + 1]) for index in range(len(calls) - 1)] == [
        (2, 4),
        (4, 6),
        (6, 8),
    ], "the pairs a left-to-right reading of the table has to be checked against"
    assert verdict is _ADJACENCY_TABLE[(left, right)]


_ANCHOR = "anchor_tool"
_FORBIDDEN = "forbidden_tool"

# A forbidden call sits between the two anchors, so the first anchor's prefix is
# clear and the last anchor's is not.
_ANCHOR_TABLE: dict[str, bool] = {"first": True, "last": False}


def test_the_absent_before_anchor_table_spans_the_restricted_domain():
    assert set(_ANCHOR_TABLE) == {quantifier.value for quantifier in AnchorQuantifier}


@pytest.mark.parametrize("quantifier", sorted(_ANCHOR_TABLE))
def test_the_anchor_quantifier_selects_which_prefix_must_be_clear(quantifier: str):
    timeline = _timeline_of((_ANCHOR, _FORBIDDEN, _ANCHOR))

    result = evaluate_constraint(
        timeline,
        {
            "absent_before": {
                "forbidden": _matches(_FORBIDDEN),
                "anchor": _side(quantifier, _ANCHOR),
            }
        },
    )

    assert (_positions_of(timeline, _ANCHOR), _positions_of(timeline, _FORBIDDEN)) == (
        [2, 6],
        [4],
    )
    assert result.passed is _ANCHOR_TABLE[quantifier]


_START = "start_tool"
_END = "end_tool"

# Anchors at 2 / 6 (start) and 8 / 12 (end), forbidden calls at 4 and 10, so each
# of the four windows holds a different part of the trial.
_WINDOW_TABLE: dict[tuple[str, str], bool] = {
    ("first", "first"): False,
    ("first", "last"): False,
    ("last", "first"): True,
    ("last", "last"): False,
}


def test_the_absent_between_window_table_spans_the_anchor_product():
    assert set(_WINDOW_TABLE) == {
        (start.value, end.value) for start in AnchorQuantifier for end in AnchorQuantifier
    }


@pytest.mark.parametrize(("start", "end"), sorted(_WINDOW_TABLE))
def test_the_anchor_quantifiers_select_which_window_must_be_clear(start: str, end: str):
    timeline = _timeline_of((_START, _FORBIDDEN, _START, _END, _FORBIDDEN, _END))

    result = evaluate_constraint(
        timeline,
        {
            "absent_between": {
                "forbidden": _matches(_FORBIDDEN),
                "start": _side(start, _START),
                "end": _side(end, _END),
            }
        },
    )

    assert (
        _positions_of(timeline, _START),
        _positions_of(timeline, _END),
        _positions_of(timeline, _FORBIDDEN),
    ) == ([2, 6], [8, 12], [4, 10])
    assert result.passed is _WINDOW_TABLE[(start, end)]


class _Polarity(str, Enum):
    """Which way adding one event to a side may move the verdict."""

    INCREASING = "increasing"
    DECREASING = "decreasing"
    NEITHER = "neither"


# One row per quantified position of every kind that has one. The rule that
# generates the monotone rows: small positions help on the left, large positions
# help on the right, and a shorter prefix helps — so `first` / `last` invert
# between `left` and `right`.
#
# Eight rows are NEITHER, and each is measured rather than reasoned: a *selected*
# position is not monotone wherever the relation reading it is not.
#   - adjacency: an inserted event both breaks an adjacent pair and forms a new
#     one, so `immediately_before` moves either way under `first` / `last`;
#   - window validity: `absent_between` is unmatched when its anchors are not in
#     the declared order, so an inserted anchor that widens the window can also be
#     the one that makes the window exist at all.
# The sweep below asserts each of those rows moves in both directions, which is
# what makes reading a side at two extremes an unsound shortcut there.
_POLARITY: dict[tuple[str, str, str | None], _Polarity] = {
    ("before", "left", "any"): _Polarity.INCREASING,
    ("before", "left", "first"): _Polarity.INCREASING,
    ("before", "left", "all"): _Polarity.DECREASING,
    ("before", "left", "last"): _Polarity.DECREASING,
    ("before", "right", "any"): _Polarity.INCREASING,
    ("before", "right", "last"): _Polarity.INCREASING,
    ("before", "right", "all"): _Polarity.DECREASING,
    ("before", "right", "first"): _Polarity.DECREASING,
    ("immediately_before", "left", "any"): _Polarity.INCREASING,
    ("immediately_before", "left", "all"): _Polarity.DECREASING,
    ("immediately_before", "left", "first"): _Polarity.NEITHER,
    ("immediately_before", "left", "last"): _Polarity.NEITHER,
    ("immediately_before", "right", "any"): _Polarity.INCREASING,
    ("immediately_before", "right", "all"): _Polarity.DECREASING,
    ("immediately_before", "right", "first"): _Polarity.NEITHER,
    ("immediately_before", "right", "last"): _Polarity.NEITHER,
    ("absent_before", "anchor", "first"): _Polarity.INCREASING,
    ("absent_before", "anchor", "last"): _Polarity.DECREASING,
    ("absent_before", "forbidden", None): _Polarity.DECREASING,
    ("absent_between", "start", "first"): _Polarity.NEITHER,
    ("absent_between", "start", "last"): _Polarity.NEITHER,
    ("absent_between", "end", "first"): _Polarity.NEITHER,
    ("absent_between", "end", "last"): _Polarity.NEITHER,
    ("absent_between", "forbidden", None): _Polarity.DECREASING,
}

_SIDE_DOMAINS: Mapping[type, type[Enum] | None] = {
    MatcherSide: Quantifier,
    AnchorSide: AnchorQuantifier,
    TraceMatcher: None,
}


def _payload_model(kind: str) -> type[BaseModel] | None:
    annotation = TraceConstraintExpr.model_fields[kind].annotation
    models = [
        argument
        for argument in get_args(annotation)
        if isinstance(argument, type) and issubclass(argument, BaseModel)
    ]
    return models[0] if models else None


def _matcher_positions(kind: str) -> dict[str, type[Enum] | None]:
    """Each matcher-bearing field of ``kind``'s payload, and the quantifiers it admits."""
    payload = _payload_model(kind)
    if payload is None:
        return {}
    return {
        name: _SIDE_DOMAINS[field.annotation]
        for name, field in payload.model_fields.items()
        if field.annotation in _SIDE_DOMAINS
    }


def _quantified_kinds() -> dict[str, dict[str, type[Enum] | None]]:
    """The kinds carrying a quantified position, read off the payload models.

    Two independent sources: the vocabulary's models here, and the polarity table
    above. A position added to a kind, or a quantifier added to a side's domain,
    arrives with no polarity and fails the coverage assertion rather than silently
    returning definite verdicts.
    """
    found = {kind: _matcher_positions(kind) for kind in sorted(TRACE_CONSTRAINT_KINDS)}
    return {
        kind: positions
        for kind, positions in found.items()
        if any(domain is not None for domain in positions.values())
    }


def _polarity_domain() -> set[tuple[str, str, str | None]]:
    return {
        (kind, position, None if domain is None else member.value)
        for kind, positions in _quantified_kinds().items()
        for position, domain in positions.items()
        for member in (domain or [None])
        if True
    }


def test_the_polarity_table_covers_every_quantified_position_of_every_kind():
    assert set(_POLARITY) == _polarity_domain()
    assert set(_quantified_kinds()) == {
        "before",
        "immediately_before",
        "absent_before",
        "absent_between",
    }


def _require(kind: str, quantifiers: Mapping[str, str]) -> dict[str, Any]:
    """The constraint for ``kind`` with each quantified position set as named."""

    def side(position: str) -> dict[str, Any]:
        return _side(quantifiers[position], f"{position}_tool")

    if kind == "before":
        return {kind: {"left": side("left"), "right": side("right")}}
    if kind == "immediately_before":
        return {kind: {"left": side("left"), "right": side("right"), "among": "tool_calls"}}
    if kind == "absent_before":
        return {kind: {"forbidden": _matches(_FORBIDDEN), "anchor": side("anchor")}}
    return {
        kind: {
            "forbidden": _matches(_FORBIDDEN),
            "start": side("start"),
            "end": side("end"),
        }
    }


@cache
def _verdict(kind: str, quantifiers: tuple[tuple[str, str], ...], tools: tuple[str, ...]) -> bool:
    return evaluate_constraint(_timeline_of(tools), _require(kind, dict(quantifiers))).passed


def _trajectories(positions: Sequence[str], longest: int) -> list[tuple[str, ...]]:
    """Every call sequence over the kind's tools in which each side matched at least once."""
    tools = [f"{position}_tool" for position in positions]
    return [
        sequence
        for length in range(len(tools), longest + 1)
        for sequence in product(tools, repeat=length)
        if set(sequence) == set(tools)
    ]


def _perturbations(
    kind: str, position: str, quantifier: str | None, others: Mapping[str, str]
) -> Iterator[tuple[bool, bool]]:
    """Every (before, after) verdict pair for one extra event on ``position``."""
    quantifiers = dict(others) | ({} if quantifier is None else {position: quantifier})
    settings = tuple(sorted(quantifiers.items()))
    inserted = f"{position}_tool"
    longest = 4 if len(_quantified_kinds()[kind]) < 3 else 3
    for trajectory in _trajectories(tuple(_quantified_kinds()[kind]), longest):
        before = _verdict(kind, settings, trajectory)
        for index in range(len(trajectory) + 1):
            grown = trajectory[:index] + (inserted,) + trajectory[index:]
            yield before, _verdict(kind, settings, grown)


def _other_settings(kind: str, position: str) -> Iterator[dict[str, str]]:
    """Every assignment of quantifiers to the kind's other quantified positions."""
    others = {
        name: domain
        for name, domain in _quantified_kinds()[kind].items()
        if name != position and domain is not None
    }
    for choice in product(*(list(domain) for domain in others.values())):
        yield {name: member.value for name, member in zip(others, choice, strict=True)}


@pytest.mark.parametrize("cell", sorted(_POLARITY, key=str), ids=str)
def test_each_polarity_row_is_re_derived_by_perturbation(cell: tuple[str, str, str | None]):
    """Add one event to that side, everywhere it could go, on every trajectory.

    The verdict may never move against the row's polarity, and must move with it at
    least once — a row nothing moves would pass on a constraint that ignored the
    side entirely.
    """
    kind, position, quantifier = cell
    expected = _POLARITY[cell]
    moved: set[tuple[bool, bool]] = set()
    for others in _other_settings(kind, position):
        for before, after in _perturbations(kind, position, quantifier, others):
            if before is not after:
                moved.add((before, after))

    rose = (False, True) in moved
    fell = (True, False) in moved
    if expected is _Polarity.INCREASING:
        assert (rose, fell) == (True, False)
    elif expected is _Polarity.DECREASING:
        assert (rose, fell) == (False, True)
    else:
        assert (rose, fell) == (True, True), (
            "the row claims adding an event moves the verdict both ways, which is what "
            "makes two extremes an unsound reading of this position"
        )


def _undecidable_pair_timeline(first: str, second: str) -> TrialTimeline:
    """A trial that asked for two calls it never ran, then ran two more.

    The unexecuted pair is undecidable for a status-reading matcher and sits
    *before* the executed pair, so each side's two readings straddle the other
    side's.
    """
    return build_turn_timeline(
        [
            Turn("user", "Do the work."),
            Turn(
                "assistant",
                "Trying.",
                unexecuted=[
                    ToolCall(id="never_ran_first", name=first, arguments={}),
                    ToolCall(id="never_ran_second", name=second, arguments={}),
                ],
            ),
            Turn(
                "assistant",
                "Again.",
                recorded=[
                    recorded_call(first, sequence=0),
                    recorded_call(second, sequence=1),
                ],
            ),
        ]
    )


def _status_side(quantifier: str, tool: str) -> dict[str, Any]:
    return {
        "quantifier": quantifier,
        "match": _matches(tool) | {"status": {"equals": "success"}},
    }


def test_before_is_undecided_where_the_two_sides_pull_opposite_ways():
    """``left: any`` rises with its side and ``right: all`` falls with its.

    Both sides read at their definite matches agree, and both read with the
    undecidable events included agree — so an implementation moving the two
    together returns a definite verdict. Taken per side they disagree, which is the
    honest answer: some completion of the record satisfies this and another does
    not.
    """
    timeline = _undecidable_pair_timeline(_LEFT, _RIGHT)

    result = evaluate_constraint(
        timeline,
        {
            "before": {
                "left": _status_side("any", _LEFT),
                "right": _status_side("all", _RIGHT),
            }
        },
    )

    assert (_positions_of(timeline, _LEFT), _positions_of(timeline, _RIGHT)) == ([2, 5], [3, 7])
    assert result.passed is False
    assert "cannot be decided" in result.message
    assert "status" in result.message


def test_absent_between_is_undecided_where_its_two_anchors_pull_opposite_ways():
    """``start: first`` widens the window as its side grows, ``end: first`` narrows it.

    The same quantifier on the two ends of one window has opposite polarity, so an
    implementation reading polarity off the quantifier alone — or moving both
    anchors together — returns a definite verdict here.
    """
    timeline = build_turn_timeline(
        [
            Turn("user", "Do the work."),
            Turn(
                "assistant",
                "Trying.",
                unexecuted=[
                    ToolCall(id="never_ran_start", name=_START, arguments={}),
                    ToolCall(id="never_ran_end", name=_END, arguments={}),
                ],
            ),
            Turn("assistant", "Logging.", recorded=[recorded_call(_FORBIDDEN, sequence=0)]),
            Turn(
                "assistant",
                "Again.",
                recorded=[
                    recorded_call(_START, sequence=1),
                    recorded_call(_END, sequence=2),
                ],
            ),
        ]
    )

    result = evaluate_constraint(
        timeline,
        {
            "absent_between": {
                "forbidden": _matches(_FORBIDDEN),
                "start": _status_side("first", _START),
                "end": _status_side("first", _END),
            }
        },
    )

    assert (
        _positions_of(timeline, _START),
        _positions_of(timeline, _END),
        _positions_of(timeline, _FORBIDDEN),
    ) == ([2, 8], [3, 10], [5])
    assert result.passed is False
    assert "cannot be decided" in result.message
