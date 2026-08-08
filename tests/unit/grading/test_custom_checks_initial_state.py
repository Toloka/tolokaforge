"""What a custom check is handed as ``ctx.initial_state.data``, per shape its task declared.

A task writes ``initial_state.json_db`` either as an inline mapping or as the name of a
JSON file beside it, and a check reading the state it started in is entitled to the same
evidence either way. Driven through the real
:class:`~tolokaforge.core.grading.combine.GradingEngine` over a ``checks.py`` on disk,
because the resolution happens at the host's call site rather than in
:func:`~tolokaforge.core.grading.checks_helpers.build_check_context`, which stays I/O-free
and is locked in ``test_build_check_context.py``: a case driving that helper directly would
hand it the resolved mapping and never exercise the shape rule at all.

The check reports what it was handed rather than only whether it liked it, so the cases can
compare *evidence* across shapes instead of comparing two scores that could agree for
unrelated reasons.

A declared path that does not resolve is a check's evidence missing rather than empty, so it
fails the component with a reason naming the key — post-trial, and deliberately a backstop:
a live native run refuses the same pack before the trial is paid for, when the adapter builds
the task description. An *empty* declared state is a different fact and reaches the check as
``{}``, whichever shape declared it and exactly as no declaration does. The hash source
refuses the file holding ``{}`` because an empty state hashes to a digest no trial can match;
that rationale does not transfer to evidence a check reads for itself.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tolokaforge.core.grading.combine import GradingEngine
from tolokaforge.core.models import Grade, GradingConfig, InitialStateConfig, Trajectory

pytestmark = [pytest.mark.unit, pytest.mark.grading]

_JSON_DB = "initial_state.json"

_DECLARED_STATE: dict[str, Any] = {"orders": [{"id": "O-1", "status": "pending"}]}

_TIMESTAMP = "2026-01-01T00:00:00+00:00"

#: Reports the state it was handed either way round, so a case can compare what two shapes
#: put in front of the check rather than only which verdict each earned.
_CHECKS = """
from tolokaforge.core.grading.checks_interface import (
    CheckContext,
    CheckFailed,
    CheckPassed,
    check,
    init,
)

_seen = {}


@init(interface_version="1.0")
def setup(ctx: CheckContext):
    _seen["initial"] = ctx.initial_state.data


@check
def the_state_the_task_declared_reached_the_check():
    orders = _seen["initial"].get("orders", [])
    if orders:
        return CheckPassed(f"initial_state.data carries orders {[o['id'] for o in orders]}")
    return CheckFailed(f"initial_state.data = {_seen['initial']}")
"""


def _pack(tmp_path: Path, name: str, written_state: dict[str, Any] | None) -> Path:
    """A task directory carrying the checks file, and the state file when one is declared."""
    task_dir = tmp_path / name
    task_dir.mkdir()
    (task_dir / "checks.py").write_text(_CHECKS)
    if written_state is not None:
        (task_dir / _JSON_DB).write_text(json.dumps(written_state))
    return task_dir


def _grade(task_dir: Path, json_db: str | dict[str, Any] | None) -> Grade:
    """The pack graded on an empty trial, so only the declared initial state moves."""
    return GradingEngine(
        GradingConfig(
            combine={
                "method": "weighted",
                "weights": {"custom_checks": 1.0},
                "pass_threshold": 0.5,
            },
            custom_checks={"enabled": True, "file": "checks.py"},
        ),
        task_dir=task_dir,
        task_initial_state=InitialStateConfig(json_db=json_db),
    ).grade_trajectory(
        Trajectory(
            task_id=task_dir.name,
            trial_index=0,
            start_ts=_TIMESTAMP,
            end_ts=_TIMESTAMP,
            messages=[],
        ),
        {},
    )


def _reported(grade: Grade) -> list[str]:
    """What each check said it was handed."""
    assert grade.custom_checks_details is not None, grade.reasons
    return [detail.message for detail in grade.custom_checks_details]


def test_a_path_declared_pack_and_its_inline_twin_hand_the_check_one_state(tmp_path) -> None:
    """The issue: identical state, two authoring shapes, one body of evidence.

    The path leg used to reach the check as ``{}`` — the call site took the declared
    ``json_db`` only when it already was a mapping — so a pack keeping its seed data in a
    file graded as though the task started empty, and its author had no way to see that from
    the grade. Both legs are compared on what the check reported, not only on the score: two
    checks agreeing on ``1.0`` while reading different states is the failure mode a score
    comparison cannot see, and the passing sentence names the seeded record so a pair of
    empty readings cannot satisfy it either.
    """
    inline = _grade(_pack(tmp_path, "inline", None), _DECLARED_STATE)
    from_file = _grade(_pack(tmp_path, "from_file", _DECLARED_STATE), _JSON_DB)

    assert _reported(inline) == ["initial_state.data carries orders ['O-1']"]
    assert _reported(from_file) == _reported(inline)
    assert (from_file.components.custom_checks, inline.components.custom_checks) == (1.0, 1.0)


def test_a_declared_path_that_does_not_resolve_fails_the_component_naming_the_key(
    tmp_path,
) -> None:
    """Evidence the task promised and did not deliver is a failure, not an empty state.

    The reason has to reach the pack's author, so it names the key, the path as the task
    wrote it and the problem. The component is ``0.0`` and no check ran at all: a call site
    that swallowed the refusal into ``{}`` would score whatever the checks then made of an
    empty state, and one that handed the checks the absence to discover for themselves would
    leave a detail behind here — either way the pack's own declaration would go unmentioned.
    """
    grade = _grade(_pack(tmp_path, "unresolvable", None), _JSON_DB)

    assert grade.components.custom_checks == 0.0
    assert grade.custom_checks_details is None, grade.custom_checks_details
    assert "context build error" in grade.reasons, grade.reasons
    assert "initial_state.json_db" in grade.reasons, grade.reasons
    assert _JSON_DB in grade.reasons, grade.reasons


def test_an_empty_declared_state_reaches_the_check_exactly_as_no_declaration_does(
    tmp_path,
) -> None:
    """A task declaring nothing and a task declaring nothing in particular are one fact here.

    An inline mapping holding nothing is already indistinguishable from no declaration, so a
    file holding ``{}`` has to read the same way or the two shapes diverge again in the
    opposite direction. The exact sentence is asserted, not just the equality between the
    three, because three legs failing for three different reasons — or the file leg being
    refused as unresolvable — would otherwise agree on the score and say nothing.
    """
    from_file = _grade(_pack(tmp_path, "empty_file", {}), _JSON_DB)
    inline = _grade(_pack(tmp_path, "empty_inline", None), {})
    undeclared = _grade(_pack(tmp_path, "undeclared", None), None)

    assert _reported(from_file) == ["initial_state.data = {}"]
    assert _reported(inline) == _reported(from_file)
    assert _reported(undeclared) == _reported(from_file)
    assert {grade.components.custom_checks for grade in (from_file, inline, undeclared)} == {0.0}
