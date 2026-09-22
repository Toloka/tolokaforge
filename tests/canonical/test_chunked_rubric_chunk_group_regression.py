"""End-to-end lock for ``chunk_group`` grouping on ``chunked_rubric``.

A rubric with three declared ``chunk_group``s — one of them oversize
enough to span two adjacent chunks — is driven through the full
``ChunkedRubricJudgeKind.evaluate()`` path. The suite pins:

- every same-group criterion id appears in the same
  ``chunk_boundaries`` tuple, except that the oversize group falls into
  two adjacent tuples with no other group's id in either;
- ``result.criterion_results`` covers every original criterion id in
  ``rubric.criteria`` order (the merge contract holds for grouped
  chunking exactly as it does for the ungrouped case already locked in
  ``test_chunked_rubric_judge_kind_regression.py``);
- ``result.status is JudgeStatus.COMPLETED``.

Cassette-only per the existing chunked-rubric regression pattern
(``_QueuedProvider`` + ``ScriptedLLMClient``, no live judge). Canonical
tier.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.utils.scripted_llm_client import ScriptedLLMClient
from tolokaforge.core.grading.judge_kinds import ChunkedRubricJudgeKind
from tolokaforge.core.grading.judge_kinds.chunked import _chunk_boundaries
from tolokaforge.core.grading.judge_result import JudgeStatus
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import ModelConfig
from tolokaforge.runner.models import Criterion, Rubric

pytestmark = pytest.mark.canonical


_JUDGE_MODEL = ModelConfig(provider="openai", name="gpt-4o-mini", temperature=0.0)


class _QueuedProvider:
    """Scripted :class:`JudgeModelProvider` popping one client per ``build``."""

    def __init__(self, clients: list[ScriptedLLMClient]) -> None:
        self._clients = list(clients)

    def build(self, model_config: ModelConfig):  # noqa: ARG002 — mirrors Protocol
        return self._clients.pop(0)


def _submit_call(criteria: list[Criterion]) -> list:
    args: dict[str, Any] = {"reasons": "overall"}
    for c in criteria:
        args[c.id] = True
        args[f"{c.id}_justification"] = f"because {c.id}\nVERDICT: MET"
    return [("submit_report", args)]


def _three_group_rubric() -> Rubric:
    """13 criteria across three groups — one oversize — declared non-contiguously.

    Groups (declaration-interleaved): ``wifi`` size 2, ``food`` size 8
    (oversize at ``chunk_size=5`` → spans two adjacent chunks), ``staff``
    size 3. Declaration order interleaves the three names so the grouping
    step is exercised, not just fixed-K packing.
    """
    return Rubric(
        criteria=[
            Criterion(id="wifi_1", description="wifi 1", chunk_group="wifi"),
            Criterion(id="food_1", description="food 1", chunk_group="food"),
            Criterion(id="wifi_2", description="wifi 2", chunk_group="wifi"),
            Criterion(id="staff_1", description="staff 1", chunk_group="staff"),
            Criterion(id="food_2", description="food 2", chunk_group="food"),
            Criterion(id="food_3", description="food 3", chunk_group="food"),
            Criterion(id="staff_2", description="staff 2", chunk_group="staff"),
            Criterion(id="food_4", description="food 4", chunk_group="food"),
            Criterion(id="food_5", description="food 5", chunk_group="food"),
            Criterion(id="food_6", description="food 6", chunk_group="food"),
            Criterion(id="food_7", description="food 7", chunk_group="food"),
            Criterion(id="food_8", description="food 8", chunk_group="food"),
            Criterion(id="staff_3", description="staff 3", chunk_group="staff"),
        ]
    )


def _evaluate_kwargs(rubric: Rubric, provider: _QueuedProvider, kind_config: dict | None):
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
        "logger": StructuredLogger(name="test-chunk-group-regression"),
    }


def test_three_groups_with_oversize_food_group_grouped_end_to_end() -> None:
    rubric = _three_group_rubric()
    chunks = _chunk_boundaries(rubric.criteria, 5)
    scripts = [[_submit_call(chunk)] for chunk in chunks]
    provider = _QueuedProvider([ScriptedLLMClient(script) for script in scripts])

    result = ChunkedRubricJudgeKind().evaluate(
        **_evaluate_kwargs(rubric, provider, {"chunk_size": 5})
    )

    assert result.status is JudgeStatus.COMPLETED
    assert result.chunk_boundaries == (
        ("wifi_1", "wifi_2"),
        ("food_1", "food_2", "food_3", "food_4", "food_5"),
        ("food_6", "food_7", "food_8"),
        ("staff_1", "staff_2", "staff_3"),
    )
    assert [cr.id for cr in result.criterion_results] == [c.id for c in rubric.criteria]


def test_same_group_ids_land_in_one_chunk_except_oversize_group() -> None:
    """Same-group ids share a chunk, except the oversize ``food`` group
    falls into two adjacent chunks with no foreign id in either slice."""
    rubric = _three_group_rubric()
    chunks = _chunk_boundaries(rubric.criteria, 5)
    scripts = [[_submit_call(chunk)] for chunk in chunks]
    provider = _QueuedProvider([ScriptedLLMClient(script) for script in scripts])

    result = ChunkedRubricJudgeKind().evaluate(
        **_evaluate_kwargs(rubric, provider, {"chunk_size": 5})
    )
    boundaries = result.chunk_boundaries

    group_of = {c.id: c.chunk_group for c in rubric.criteria}

    wifi_chunks = [
        i for i, chunk in enumerate(boundaries) if any(group_of[cid] == "wifi" for cid in chunk)
    ]
    assert len(wifi_chunks) == 1
    assert {group_of[cid] for cid in boundaries[wifi_chunks[0]]} == {"wifi"}

    staff_chunks = [
        i for i, chunk in enumerate(boundaries) if any(group_of[cid] == "staff" for cid in chunk)
    ]
    assert len(staff_chunks) == 1
    assert {group_of[cid] for cid in boundaries[staff_chunks[0]]} == {"staff"}

    food_chunks = [
        i for i, chunk in enumerate(boundaries) if any(group_of[cid] == "food" for cid in chunk)
    ]
    assert len(food_chunks) == 2
    assert food_chunks[1] == food_chunks[0] + 1
    for i in food_chunks:
        assert {group_of[cid] for cid in boundaries[i]} == {"food"}
