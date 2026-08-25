"""``plugin_registry.load_rubric_evaluator`` — fail-loud resolution.

Locks the loader for the ``tolokaforge.rubric_evaluators`` entry-point
group:

* the shipped factory (``llm_judge``) resolves to the callable
  ``pyproject.toml`` registers it against, so a future refactor renaming
  the symbol trips this test before it lands;
* the two-step "loader → factory(ctx) → instance" chain returns the
  reference :class:`LLMJudgeRubricEvaluator`;
* an unknown name raises :class:`UnknownImplementationError` listing every
  known name in the group — the same fail-loud shape ``load_grading_substrate``
  and the other loaders use.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tolokaforge.core.grading.default_rubric_evaluator import (
    LLMJudgeRubricEvaluator,
    _llm_judge_rubric_evaluator_factory,
)
from tolokaforge.core.grading.rubric_evaluator import RubricEvaluatorContext
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.plugin_registry import (
    RUBRIC_EVALUATORS_GROUP,
    UnknownImplementationError,
    available_rubric_evaluators,
    load_rubric_evaluator,
)

pytestmark = pytest.mark.unit


def _context() -> RubricEvaluatorContext:
    """Minimum viable context — the reference impl reads
    :attr:`judge_model_provider` lazily inside ``.evaluate()``, so a
    :class:`MagicMock` is sufficient here."""
    return RubricEvaluatorContext(
        judge_model_provider=MagicMock(),
        logger=StructuredLogger(name="test-load-rubric-evaluator"),
    )


def test_llm_judge_resolves_to_the_shipped_factory() -> None:
    assert load_rubric_evaluator("llm_judge") is _llm_judge_rubric_evaluator_factory


def test_llm_judge_factory_returns_a_llm_judge_rubric_evaluator_instance() -> None:
    """Locks the two-step ``loader → factory(ctx) → instance`` chain end-to-end."""
    factory = load_rubric_evaluator("llm_judge")
    assert isinstance(factory(_context()), LLMJudgeRubricEvaluator)


def test_available_lists_the_shipped_name() -> None:
    assert "llm_judge" in available_rubric_evaluators()


def test_unknown_name_raises_unknown_implementation_error() -> None:
    with pytest.raises(UnknownImplementationError) as excinfo:
        load_rubric_evaluator("nonexistent")
    message = str(excinfo.value)
    assert RUBRIC_EVALUATORS_GROUP in message
    assert "llm_judge" in message
    assert excinfo.value.group == RUBRIC_EVALUATORS_GROUP
    assert "llm_judge" in excinfo.value.known
