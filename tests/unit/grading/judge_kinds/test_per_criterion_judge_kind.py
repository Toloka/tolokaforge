"""Unit tests for ``PerCriterionRubricJudgeKind``.

Locks the invariants a caller relies on when opting into the kind, end-to-end
against the real ``chunked_rubric`` merge path (scripted judge, no live LLM):

- Every criterion ends up in its own single-criterion chunk regardless of
  rubric length — ``result.chunk_boundaries`` is one tuple per criterion.
- Refuses any non-empty ``kind_config`` — an explicit ``chunk_size`` here
  would be silently overridden, so unknown keys fail loud eagerly.
- ``NAME`` matches the entry-point name (``per_criterion_rubric``).
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.utils.scripted_llm_client import ScriptedLLMClient
from tolokaforge.core.grading.judge_kinds.per_criterion import PerCriterionRubricJudgeKind
from tolokaforge.core.grading.judge_result import JudgeStatus
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import ModelConfig
from tolokaforge.runner.models import Criterion, Rubric

pytestmark = pytest.mark.unit


_JUDGE_MODEL = ModelConfig(provider="openai", name="gpt-4o-mini", temperature=0.0)


class _QueuedProvider:
    """Scripted JudgeModelProvider popping one client per ``build`` call."""

    def __init__(self, clients: list[ScriptedLLMClient]) -> None:
        self._clients = list(clients)

    def build(self, model_config: ModelConfig):  # noqa: ARG002 — mirrors Protocol
        return self._clients.pop(0)


def _single_criterion_submit_call(criterion: Criterion) -> list:
    args: dict[str, Any] = {"reasons": f"overall {criterion.id}"}
    args[criterion.id] = True
    args[f"{criterion.id}_justification"] = f"because {criterion.id}\nVERDICT: MET"
    return [("submit_report", args)]


def _evaluate_kwargs(rubric: Rubric, provider: _QueuedProvider, kind_config):
    return {
        "rubric": rubric,
        "agent_system_prompt": "you are an agent",
        "transcript": [{"role": "user", "content": "hi"}],
        "db_reader": None,
        "kb_search": None,
        "workspace_dir": None,
        "extra_read_tools": [],
        "state_diff": None,
        "judge_model_config": _JUDGE_MODEL,
        "judge_model_provider": provider,
        "disable_knowledge_search": False,
        "custom_system_prompt": None,
        "include_agent_system_prompt": True,
        "kind_config": kind_config,
        "logger": StructuredLogger(name="test-per-criterion"),
    }


def test_name_matches_entry_point() -> None:
    assert PerCriterionRubricJudgeKind.NAME == "per_criterion_rubric"


def test_every_criterion_lands_in_its_own_chunk_end_to_end() -> None:
    """Chunk boundaries are exactly one tuple per criterion, regardless of rubric length."""
    rubric = Rubric(
        criteria=[
            Criterion(id="c0", description="c0"),
            Criterion(id="c1", description="c1"),
            Criterion(id="c2", description="c2"),
        ]
    )
    scripts = [[_single_criterion_submit_call(c)] for c in rubric.criteria]
    provider = _QueuedProvider([ScriptedLLMClient(script) for script in scripts])

    result = PerCriterionRubricJudgeKind().evaluate(
        **_evaluate_kwargs(rubric, provider, kind_config=None)
    )

    assert result.status is JudgeStatus.COMPLETED
    assert result.chunk_boundaries == (("c0",), ("c1",), ("c2",))
    assert [cr.id for cr in result.criterion_results] == ["c0", "c1", "c2"]


def test_evaluate_refuses_non_empty_kind_config() -> None:
    """Explicit kind_config keys are refused loudly, not silently overridden."""
    rubric = Rubric(criteria=[Criterion(id="c0", description="c0")])
    provider = _QueuedProvider([])  # would fail if consulted
    with pytest.raises(ValueError) as exc_info:
        PerCriterionRubricJudgeKind().evaluate(
            **_evaluate_kwargs(rubric, provider, kind_config={"chunk_size": 5})
        )
    message = str(exc_info.value)
    assert "per_criterion_rubric" in message
    assert "no keys" in message
    assert "chunk_size" in message
