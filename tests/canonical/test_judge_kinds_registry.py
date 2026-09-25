"""``tolokaforge.judge_kinds`` — resolver + Protocol satisfaction lock.

Locks the three invariants the typed-kind registry commits to:

1. Each shipped name (``single_shot_rubric``, ``multi_turn_rubric``,
   ``auto_rubric``, plus the internal building blocks ``voted_rubric``,
   ``auto_anchored_rubric``) resolves to its class with matching ``NAME``.
2. An unknown name fails loud via :class:`UnknownImplementationError`
   naming the offending key + the registered set + the group.
3. Each shipped class satisfies the runtime-checkable
   :class:`JudgeKind` Protocol.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.grading.judge_kinds import (
    AutoAnchoredRubricJudgeKind,
    AutoRubricJudgeKind,
    JudgeKind,
    MultiTurnRubricJudgeKind,
    SingleShotRubricJudgeKind,
    VotedRubricJudgeKind,
)
from tolokaforge.core.plugin_registry import (
    UnknownImplementationError,
    available_judge_kinds,
    load_judge_kind,
)

pytestmark = pytest.mark.canonical


def test_builtin_judge_kinds_resolve_to_their_class() -> None:
    assert load_judge_kind("single_shot_rubric") is SingleShotRubricJudgeKind
    assert load_judge_kind("voted_rubric") is VotedRubricJudgeKind
    assert load_judge_kind("auto_anchored_rubric") is AutoAnchoredRubricJudgeKind
    assert load_judge_kind("multi_turn_rubric") is MultiTurnRubricJudgeKind
    assert load_judge_kind("auto_rubric") is AutoRubricJudgeKind
    assert available_judge_kinds() == [
        "auto_anchored_rubric",
        "auto_rubric",
        "multi_turn_rubric",
        "single_shot_rubric",
        "voted_rubric",
    ]


def test_unknown_judge_kind_raises_named_error() -> None:
    with pytest.raises(UnknownImplementationError) as exc_info:
        load_judge_kind("does_not_exist")
    message = str(exc_info.value)
    assert "does_not_exist" in message
    assert "tolokaforge.judge_kinds" in message
    assert "single_shot_rubric" in message
    assert "voted_rubric" in message
    assert "auto_anchored_rubric" in message
    assert "multi_turn_rubric" in message
    assert "auto_rubric" in message


def test_judge_kind_classes_are_runtime_checkable_protocol_instances() -> None:
    assert isinstance(SingleShotRubricJudgeKind(), JudgeKind)
    assert isinstance(VotedRubricJudgeKind(), JudgeKind)
    assert isinstance(AutoAnchoredRubricJudgeKind(), JudgeKind)
    assert isinstance(MultiTurnRubricJudgeKind(), JudgeKind)
    assert isinstance(AutoRubricJudgeKind(), JudgeKind)
