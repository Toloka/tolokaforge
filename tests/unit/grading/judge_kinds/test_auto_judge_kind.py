"""Unit tests for :class:`AutoRubricJudgeKind`.

Locks the per-rubric selection contract:

- A rubric with any ``graded`` criterion whose ``expected is None`` →
  dispatches ``multi_turn_rubric``.
- A fully anchored rubric (every graded criterion has ``expected``
  set) → dispatches ``single_shot_rubric``.
- A binary-only rubric → dispatches ``single_shot_rubric``.
- The selection reason lands as a prefix on :attr:`JudgeResult.reasons`,
  including the sorted list of unanchored criterion ids when
  multi_turn was chosen.
- Non-empty ``kind_config`` raises :class:`ValueError`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.utils.scripted_llm_client import ScriptedLLMClient
from tolokaforge.core.grading.judge_kinds import AutoRubricJudgeKind
from tolokaforge.core.grading.judge_kinds.auto_anchored import clear_anchor_cache
from tolokaforge.core.grading.judge_result import JudgeStatus
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import ModelConfig
from tolokaforge.runner.models import Criterion, Rubric

pytestmark = pytest.mark.unit


_JUDGE_MODEL = ModelConfig(provider="openai", name="gpt-4o-mini", temperature=0.0)


class _QueuedProvider:
    def __init__(self, clients: list[ScriptedLLMClient]) -> None:
        self._clients = list(clients)
        self.built: list[ScriptedLLMClient] = []

    def build(self, model_config: ModelConfig) -> ScriptedLLMClient:  # noqa: ARG002
        if not self._clients:
            raise AssertionError("provider ran out of scripted clients")
        client = self._clients.pop(0)
        self.built.append(client)
        return client


@pytest.fixture(autouse=True)
def _reset_cache():
    clear_anchor_cache()
    yield
    clear_anchor_cache()


def _submit_call(criteria: list[Criterion]) -> list[tuple[str, dict[str, Any]]]:
    args: dict[str, Any] = {"reasons": "ok"}
    for c in criteria:
        if c.kind == "graded":
            args[c.id] = 0.9
            args[f"{c.id}_justification"] = f"j {c.id}\nSCORE: 0.9"
        else:
            args[c.id] = True
            args[f"{c.id}_justification"] = f"j {c.id}\nVERDICT: MET"
    return [("submit_report", args)]


def _evaluate_kwargs(rubric: Rubric, provider: _QueuedProvider, kind_config=None):
    return {
        "rubric": rubric,
        "agent_system_prompt": "",
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
        "logger": StructuredLogger(name="test-auto"),
    }


def test_name_matches_entry_point() -> None:
    assert AutoRubricJudgeKind.NAME == "auto_rubric"


def test_fully_anchored_rubric_dispatches_single_shot() -> None:
    """Every graded criterion carries an author-written ``expected:`` →
    single_shot is selected → exactly 1 client build (single_shot has
    no warm-up)."""
    rubric = Rubric(
        criteria=[
            Criterion(id="mentions_id", description="Mentions id.", kind="binary"),
            Criterion(
                id="clarity",
                description="Reads clearly.",
                kind="graded",
                expected="A clear reply is one paragraph.",
            ),
        ]
    )
    provider = _QueuedProvider([ScriptedLLMClient([_submit_call(rubric.criteria)])])

    result = AutoRubricJudgeKind().evaluate(**_evaluate_kwargs(rubric, provider))

    assert result.status is JudgeStatus.COMPLETED
    assert len(provider.built) == 1
    assert result.reasons.startswith("auto_rubric selected single_shot_rubric")
    assert "all graded criteria are anchored" in result.reasons


def test_binary_only_rubric_dispatches_single_shot() -> None:
    rubric = Rubric(
        criteria=[
            Criterion(id="a", description="A.", kind="binary"),
            Criterion(id="b", description="B.", kind="binary"),
        ]
    )
    provider = _QueuedProvider([ScriptedLLMClient([_submit_call(rubric.criteria)])])
    result = AutoRubricJudgeKind().evaluate(**_evaluate_kwargs(rubric, provider))

    assert result.status is JudgeStatus.COMPLETED
    assert len(provider.built) == 1
    assert result.reasons.startswith("auto_rubric selected single_shot_rubric")


def test_anchored_graded_plus_binary_dispatches_single_shot() -> None:
    rubric = Rubric(
        criteria=[
            Criterion(id="mentions_id", description="Mentions id.", kind="binary"),
            Criterion(
                id="tone",
                description="Is professional.",
                kind="graded",
                expected="Professional tone is neither casual nor stiff.",
            ),
        ]
    )
    provider = _QueuedProvider([ScriptedLLMClient([_submit_call(rubric.criteria)])])
    result = AutoRubricJudgeKind().evaluate(**_evaluate_kwargs(rubric, provider))

    assert result.status is JudgeStatus.COMPLETED
    assert len(provider.built) == 1
    assert result.reasons.startswith("auto_rubric selected single_shot_rubric")


def test_unanchored_graded_criterion_dispatches_multi_turn() -> None:
    """A ``graded`` criterion with ``expected is None`` → multi_turn
    is selected → exactly 4 client builds (1 warm-up + K=3 grading)."""
    rubric = Rubric(
        criteria=[
            Criterion(id="mentions_id", description="Mentions id.", kind="binary"),
            Criterion(id="clarity", description="Reads clearly.", kind="graded"),
        ]
    )
    anchor_map = {"clarity": "one clear paragraph naming the entity"}
    clients = [
        ScriptedLLMClient([json.dumps(anchor_map)]),
        ScriptedLLMClient([_submit_call(rubric.criteria)]),
        ScriptedLLMClient([_submit_call(rubric.criteria)]),
        ScriptedLLMClient([_submit_call(rubric.criteria)]),
    ]
    provider = _QueuedProvider(clients)

    result = AutoRubricJudgeKind().evaluate(**_evaluate_kwargs(rubric, provider))

    assert result.status is JudgeStatus.COMPLETED
    assert len(provider.built) == 4
    assert result.reasons.startswith("auto_rubric selected multi_turn_rubric")
    assert "clarity" in result.reasons


def test_selection_reason_lists_unanchored_ids_sorted() -> None:
    rubric = Rubric(
        criteria=[
            Criterion(id="writeup_quality", description="Writeup.", kind="graded"),
            Criterion(id="clarity", description="Reads clearly.", kind="graded"),
            Criterion(
                id="tone",
                description="Tone.",
                kind="graded",
                expected="Professional tone.",
            ),
        ]
    )
    anchor_map = {
        "clarity": "one clear paragraph",
        "writeup_quality": "one polished writeup",
    }
    clients = [
        ScriptedLLMClient([json.dumps(anchor_map)]),
        ScriptedLLMClient([_submit_call(rubric.criteria)]),
        ScriptedLLMClient([_submit_call(rubric.criteria)]),
        ScriptedLLMClient([_submit_call(rubric.criteria)]),
    ]
    provider = _QueuedProvider(clients)

    result = AutoRubricJudgeKind().evaluate(**_evaluate_kwargs(rubric, provider))

    assert result.status is JudgeStatus.COMPLETED
    # Sorted alphabetically: clarity, writeup_quality (tone is anchored, omitted).
    assert "clarity, writeup_quality" in result.reasons
    assert "tone" not in result.reasons.split("\n\n")[0]


def test_non_empty_kind_config_raises() -> None:
    rubric = Rubric(
        criteria=[Criterion(id="clarity", description="clarity", kind="graded")],
    )
    provider = _QueuedProvider([])
    with pytest.raises(ValueError) as exc_info:
        AutoRubricJudgeKind().evaluate(
            **_evaluate_kwargs(rubric, provider, kind_config={"foo": "bar"})
        )
    message = str(exc_info.value)
    assert "auto_rubric" in message
    assert "kind_config" in message
