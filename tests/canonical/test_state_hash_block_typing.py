"""Differential lock: a typo inside ``state_checks.hash`` buys no better grade.

The block is an untyped ``dict[str, Any]`` and every member is read with ``.get()``,
so a misspelled key is dropped without a word and the block requests *nothing* —
which is not a failing check but an absent one. Two authored shapes reach that,
both internally consistent enough that every rule the authoring gate carries
passes them:

- the author's own keys misspelled (``enalbed`` / ``expected_state_hsah``);
- the **runner's** flattened field names (``hash_enabled`` / ``expected_hash`` /
  ``hash_weight``) written into ``grading.yaml`` — the names an author meets in
  ``docs/GRADING.md``'s substrate tables, which the core block does not declare.

Each is measured beside the same block spelled correctly, through both entry points
into it: ``validate_grading_yaml`` — the gate a pack meets before a trial is paid
for, driven with an unresolvable inventory and replay world so the tool-aware rules
skip rather than decide the row — and a real ``GradingEngine.grade_trajectory`` over
state that satisfies the assertion.

**The locked predicate is that the typo cell either refuses at load or grades no
higher than the correct cell**, rather than that the two cells agree. The two are
not the same claim: a typo refused at load produces no score to agree with, so an
equality would have to be rewritten the moment the defect is fixed — and a
reproduction lock whose assertion gets edited to make it pass has stopped measuring
the thing it was written for. Phrased as "no better", the predicate is false today
(1.0 against 0.5, and nothing raises) and true once the key is refused, so removing
the ``xfail`` marker is the whole flip.

``combine.pass_threshold`` is the model's own default rather than the correct cell's
score: at 0.5 both cells would pass and the ``binary_pass`` half of the predicate
would assert nothing.

The second test is the other side of typing the block, and it passes today: an empty
``hash`` mapping, an explicit ``null`` and no ``hash`` key at all are one shape in
every observable — the gate's verdict and every field of the resulting ``Grade``.
An empty mapping is falsy and a model instance is not, so the three sites that ask
"is a hash block configured?" by truthiness have to start asking it another way; this
is what says that move changed nothing.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from tolokaforge.adapters._task_loader import validate_grading_yaml
from tolokaforge.core.grading.combine import GradingEngine
from tolokaforge.core.grading.config_validation import ReplayWorld, ToolInventory
from tolokaforge.core.models import (
    Grade,
    GradingConfig,
    Message,
    MessageRole,
    Trajectory,
    TrialStatus,
)

pytestmark = [pytest.mark.canonical, pytest.mark.grading]

_TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)

_TRAJECTORY = Trajectory(
    task_id="state-hash-block-typing",
    trial_index=0,
    start_ts=_TIMESTAMP,
    end_ts=_TIMESTAMP,
    status=TrialStatus.COMPLETED,
    messages=[Message(role=MessageRole.USER, content="place the order")],
    tool_log=[],
)

_FINAL_STATE: dict[str, Any] = {"db": {"orders": [{"id": 1, "status": "placed"}]}}

_PASSING_ASSERTION: dict[str, Any] = {
    "path": "$.db.orders[0].status",
    "equals": "placed",
    "description": "the order was placed",
}

# Well-formed and unreachable: the hash of any state is a sha256 digest, and no state
# hashes to all zeroes — so a correctly spelled block scores its half of the fold at
# 0.0 and the fold at 0.5, while a dropped key scores the assertion alone at 1.0.
_NEVER_MATCHING_HASH = "0" * 64

# The model's own default. A threshold at the correct cell's score (0.5) passes both
# cells, which leaves the binary_pass half of the predicate asserting nothing.
_PASS_THRESHOLD = 0.8

_CORRECTLY_SPELLED: dict[str, Any] = {
    "enabled": True,
    "expected_state_hash": _NEVER_MATCHING_HASH,
    "weight": 0.5,
}

_MISSPELLED_AUTHOR_KEYS: dict[str, Any] = {
    "enalbed": True,
    "expected_state_hsah": _NEVER_MATCHING_HASH,
    "weight": 0.5,
}

_RUNNER_FLATTENED_FIELD_NAMES: dict[str, Any] = {
    "hash_enabled": True,
    "expected_hash": _NEVER_MATCHING_HASH,
    "hash_weight": 0.5,
}

_TYPO_WINS_TODAY = pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "#730: state_checks.hash is an untyped dict, so a key it does not declare is "
        "dropped at every read and the block requests nothing — the trial grades 1.0 "
        "and PASSes where the same block spelled correctly grades 0.5 and fails."
    ),
)
"""Both rows carry this one marker, so no row can be xfail without ``strict``.

``raises=AssertionError`` is what keeps the rows honest: a bare ``xfail`` swallows
any exception, so a row whose config stopped loading would report as the divergence
it is supposed to be measuring. :class:`_FixtureDefect` is deliberately not an
``AssertionError`` for the same reason.
"""

_ABSENT = object()
"""No ``hash`` key at all — distinct from a key written with an empty or null value."""

_DEGENERATE_HASH_BLOCKS: dict[str, Any] = {
    "absent": _ABSENT,
    "empty-mapping": {},
    "null": None,
}

_GRADING_PATH = "<grading.yaml>"


class _FixtureDefect(Exception):
    """The row cannot measure what it claims, so its verdict is about the fixture.

    A correct cell that will not load, or that already scores full marks, leaves
    "no better than the correct cell" admitting every grade there is.
    """


@dataclass(frozen=True)
class _Cell:
    """What one authored ``hash`` block produced at each entry point into it.

    ``gate_refusal`` and ``load_refusal`` are the refusal text with the temporary
    grading path folded out, or ``None`` where that entry point accepted the block;
    ``grade`` is ``None`` exactly when the config refused to construct.
    """

    gate_refusal: str | None
    load_refusal: str | None
    grade: Grade | None


def _grading_config(hash_block: object, jsonpaths: list[dict[str, Any]]) -> dict[str, Any]:
    state_checks: dict[str, Any] = {"jsonpaths": list(jsonpaths)}
    if hash_block is not _ABSENT:
        state_checks["hash"] = hash_block
    return {
        "combine": {
            "method": "weighted",
            "weights": {"state_checks": 1.0},
            "pass_threshold": _PASS_THRESHOLD,
        },
        "state_checks": state_checks,
    }


def _measure(config: dict[str, Any], grading_path: Path) -> _Cell:
    """Drive one authored config through the authoring gate and the grading engine.

    The gate reads a file and the engine reads a mapping, so both are given the same
    authored text — one written to disk, one passed to ``GradingConfig`` — rather than
    a shared object neither substrate would have in a real run.
    """
    grading_path.write_text(yaml.safe_dump(config))

    gate_refusal: str | None = None
    try:
        validate_grading_yaml(
            grading_path,
            inventory=ToolInventory.unresolvable(),
            replay_world=ReplayWorld.unresolvable(),
        )
    except ValueError as refusal:
        gate_refusal = str(refusal).replace(str(grading_path), _GRADING_PATH)

    try:
        grading_config = GradingConfig(**config)
    except ValueError as refusal:
        return _Cell(gate_refusal=gate_refusal, load_refusal=str(refusal), grade=None)

    grade = GradingEngine(grading_config=grading_config).grade_trajectory(_TRAJECTORY, _FINAL_STATE)
    return _Cell(gate_refusal=gate_refusal, load_refusal=None, grade=grade)


def _observables(cell: _Cell) -> dict[str, Any]:
    """Everything a caller can see of one cell, as plain data."""
    return {
        "gate_refusal": cell.gate_refusal,
        "load_refusal": cell.load_refusal,
        "grade": None if cell.grade is None else cell.grade.model_dump(mode="json"),
    }


def _grades_no_higher(typo: _Cell, correct: Grade) -> bool:
    """Whether ``typo`` bought nothing over ``correct``.

    A cell that never produced a grade bought nothing by definition; one that did
    must neither out-score the correct spelling nor turn its failure into a pass.
    """
    if typo.grade is None:
        return True
    if typo.grade.score > correct.score:
        return False
    return not (typo.grade.binary_pass and not correct.binary_pass)


def _correctly_spelled_grade(tmp_path: Path) -> Grade:
    """The grade the typo rows are measured against, with its capacity checked."""
    cell = _measure(
        _grading_config(_CORRECTLY_SPELLED, [_PASSING_ASSERTION]), tmp_path / "correct.yaml"
    )
    if cell.gate_refusal is not None:
        raise _FixtureDefect(
            f"the correctly spelled block was refused at the gate: {cell.gate_refusal}"
        )
    if cell.grade is None:
        raise _FixtureDefect(f"the correctly spelled block would not load: {cell.load_refusal}")
    if cell.grade.components.state_checks is None:
        raise _FixtureDefect("the correctly spelled block scored no state_checks component")
    if cell.grade.binary_pass or cell.grade.score >= 1.0:
        raise _FixtureDefect(
            "the correctly spelled block already earns the best grade there is "
            f"(score {cell.grade.score}, passed {cell.grade.binary_pass}), so "
            "'no better than the correct cell' admits every grade a typo can reach"
        )
    return cell.grade


@pytest.mark.parametrize(
    "typo_block",
    [
        pytest.param(_MISSPELLED_AUTHOR_KEYS, id="misspelled-author-keys", marks=_TYPO_WINS_TODAY),
        pytest.param(
            _RUNNER_FLATTENED_FIELD_NAMES,
            id="runner-flattened-field-names",
            marks=_TYPO_WINS_TODAY,
        ),
    ],
)
def test_a_typo_inside_the_hash_block_buys_no_better_grade(
    typo_block: dict[str, Any], tmp_path: Path
) -> None:
    """A hash block the core config does not declare is refused, or it grades no higher."""
    correct = _correctly_spelled_grade(tmp_path)
    typo = _measure(_grading_config(typo_block, [_PASSING_ASSERTION]), tmp_path / "typo.yaml")

    assert typo.gate_refusal is not None or _grades_no_higher(typo, correct), (
        "the authoring gate passed a hash block carrying keys it does not declare, and "
        "the trial then graded higher than the same block spelled correctly"
    )
    assert typo.load_refusal is not None or _grades_no_higher(typo, correct), (
        "the grading config loaded a hash block carrying keys it does not declare, and "
        "the trial then graded higher than the same block spelled correctly"
    )


@pytest.mark.parametrize(
    ("jsonpaths", "baseline_score", "gate_refuses"),
    [
        pytest.param([], 0.0, True, id="no-assertions"),
        pytest.param([_PASSING_ASSERTION], 1.0, False, id="one-assertion"),
    ],
)
def test_a_degenerate_hash_block_is_indistinguishable_from_no_block(
    jsonpaths: list[dict[str, Any]], baseline_score: float, gate_refuses: bool, tmp_path: Path
) -> None:
    """An empty ``hash`` mapping, an explicit ``null`` and no key at all are one shape."""
    observed = {
        shape: _observables(_measure(_grading_config(block, jsonpaths), tmp_path / f"{shape}.yaml"))
        for shape, block in _DEGENERATE_HASH_BLOCKS.items()
    }

    baseline = observed["absent"]
    assert (baseline["gate_refusal"] is not None) is gate_refuses
    assert baseline["grade"]["score"] == baseline_score

    assert observed == dict.fromkeys(_DEGENERATE_HASH_BLOCKS, baseline), (
        "writing the hash key with a degenerate value is observably different from "
        "leaving it out, so something reads the block for truth rather than for what "
        "it declares"
    )
