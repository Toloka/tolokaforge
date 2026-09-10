"""``tolokaforge.judge_kinds`` — resolver + Protocol satisfaction lock.

Locks the three invariants the typed-kind registry commits to:

1. Each built-in name (``single_shot_rubric``, ``chunked_rubric``,
   ``agentic_rubric``) resolves to its class with matching ``NAME``.
2. An unknown name fails loud via :class:`UnknownImplementationError`
   naming the offending key + the registered set + the group.
3. Each built-in class satisfies the runtime-checkable
   :class:`JudgeKind` Protocol.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.grading.judge_kinds import (
    AgenticRubricJudgeKind,
    ChunkedRubricJudgeKind,
    JudgeKind,
    SingleShotRubricJudgeKind,
)
from tolokaforge.core.plugin_registry import (
    UnknownImplementationError,
    available_judge_kinds,
    load_judge_kind,
)

pytestmark = pytest.mark.canonical


def test_builtin_judge_kinds_resolve_to_their_class() -> None:
    assert load_judge_kind("single_shot_rubric") is SingleShotRubricJudgeKind
    assert load_judge_kind("chunked_rubric") is ChunkedRubricJudgeKind
    assert load_judge_kind("agentic_rubric") is AgenticRubricJudgeKind
    assert available_judge_kinds() == ["agentic_rubric", "chunked_rubric", "single_shot_rubric"]


def test_unknown_judge_kind_raises_named_error() -> None:
    with pytest.raises(UnknownImplementationError) as exc_info:
        load_judge_kind("does_not_exist")
    message = str(exc_info.value)
    assert "does_not_exist" in message
    assert "tolokaforge.judge_kinds" in message
    assert "single_shot_rubric" in message
    assert "chunked_rubric" in message
    assert "agentic_rubric" in message


def test_judge_kind_classes_are_runtime_checkable_protocol_instances() -> None:
    assert isinstance(SingleShotRubricJudgeKind(), JudgeKind)
    assert isinstance(ChunkedRubricJudgeKind(), JudgeKind)
    assert isinstance(AgenticRubricJudgeKind(), JudgeKind)
