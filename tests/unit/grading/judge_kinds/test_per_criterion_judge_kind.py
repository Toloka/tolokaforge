"""Unit tests for ``PerCriterionRubricJudgeKind``.

Locks the invariants a caller relies on when opting into the kind:

- Delegates to ``chunked_rubric`` with ``chunk_size=1``, so every criterion
  ends up in its own single-criterion chunk regardless of rubric length.
- Refuses any non-empty ``kind_config`` — an explicit ``chunk_size`` here
  would be silently overridden, so unknown keys fail loud eagerly.
- ``NAME`` matches the entry-point name (``per_criterion_rubric``).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tolokaforge.core.grading.judge_kinds.per_criterion import PerCriterionRubricJudgeKind

pytestmark = pytest.mark.unit


def test_name_matches_entry_point() -> None:
    assert PerCriterionRubricJudgeKind.NAME == "per_criterion_rubric"


def test_evaluate_delegates_to_chunked_rubric_with_chunk_size_one() -> None:
    """The chunk_size that reaches ChunkedRubricJudgeKind is always 1."""
    captured: dict[str, object] = {}

    def fake_evaluate(_self: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()  # opaque sentinel; caller passes it through

    with patch(
        "tolokaforge.core.grading.judge_kinds.per_criterion.ChunkedRubricJudgeKind.evaluate",
        fake_evaluate,
    ):
        PerCriterionRubricJudgeKind().evaluate(
            rubric=None,  # type: ignore[arg-type]
            agent_system_prompt="",
            transcript=[],
            db_reader=None,
            kb_search=None,
            workspace_dir=None,
            extra_read_tools=[],
            state_diff=None,
            judge_model_config=None,  # type: ignore[arg-type]
            judge_model_provider=None,  # type: ignore[arg-type]
            disable_knowledge_search=False,
            custom_system_prompt=None,
            include_agent_system_prompt=True,
            kind_config=None,
            logger=None,  # type: ignore[arg-type]
        )

    assert captured["kind_config"] == {"chunk_size": 1}


def test_evaluate_refuses_non_empty_kind_config() -> None:
    """Explicit kind_config keys are refused loudly, not silently overridden."""
    with pytest.raises(ValueError) as exc_info:
        PerCriterionRubricJudgeKind().evaluate(
            rubric=None,  # type: ignore[arg-type]
            agent_system_prompt="",
            transcript=[],
            db_reader=None,
            kb_search=None,
            workspace_dir=None,
            extra_read_tools=[],
            state_diff=None,
            judge_model_config=None,  # type: ignore[arg-type]
            judge_model_provider=None,  # type: ignore[arg-type]
            disable_knowledge_search=False,
            custom_system_prompt=None,
            include_agent_system_prompt=True,
            kind_config={"chunk_size": 5},
            logger=None,  # type: ignore[arg-type]
        )
    message = str(exc_info.value)
    assert "per_criterion_rubric" in message
    assert "no keys" in message
    assert "chunk_size" in message
