"""``tolokaforge.judge_kinds`` — resolver + Protocol satisfaction lock.

Locks the three invariants the typed-kind registry commits to:

1. The one built-in name (``single_shot_rubric``) resolves to
   :class:`SingleShotRubricJudgeKind` with matching ``NAME``.
2. An unknown name fails loud via :class:`UnknownImplementationError`
   naming the offending key + the registered set + the group.
3. The built-in class satisfies the runtime-checkable
   :class:`JudgeKind` Protocol.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.grading.judge_kinds import JudgeKind, SingleShotRubricJudgeKind
from tolokaforge.core.plugin_registry import (
    UnknownImplementationError,
    available_judge_kinds,
    load_judge_kind,
)

pytestmark = pytest.mark.canonical


def test_builtin_judge_kind_resolves_to_its_class() -> None:
    assert load_judge_kind("single_shot_rubric") is SingleShotRubricJudgeKind
    assert available_judge_kinds() == ["single_shot_rubric"]


def test_unknown_judge_kind_raises_named_error() -> None:
    with pytest.raises(UnknownImplementationError) as exc_info:
        load_judge_kind("does_not_exist")
    message = str(exc_info.value)
    assert "does_not_exist" in message
    assert "tolokaforge.judge_kinds" in message
    assert "single_shot_rubric" in message


def test_judge_kind_class_is_runtime_checkable_protocol_instance() -> None:
    assert isinstance(SingleShotRubricJudgeKind(), JudgeKind)
