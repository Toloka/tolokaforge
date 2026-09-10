"""30-criterion truncation regression: ``single_shot_rubric`` ERRORS, ``chunked_rubric`` COMPLETES.

The failure class :class:`ChunkedRubricJudgeKind` targets is a
``single_shot_rubric`` judge whose lone ``submit_report`` call exceeds the
judge model's output-token ceiling — ``parse_submit_report`` fails on the
missing verdicts, the retry budget exhausts, whole-trial
:attr:`JudgeStatus.ERRORED`. This suite pins that behaviour with a
cassette-only regression:

- Cassette A drives ``single_shot_rubric`` with one ``submit_report`` call
  that emits only 4 of 30 criterion verdicts (simulating truncation).
  ``parse_submit_report`` raises :class:`SubmitReportValidationError` on
  every retry attempt (the cassette script keeps returning the truncated
  payload), and the whole trial returns ERRORED.
- Cassette B drives ``chunked_rubric`` with ``chunk_size=5`` — six clean
  chunk scripts, each ``submit_report`` covering its five criteria — and
  returns COMPLETED with all 30 verdicts merged and six 5-tuples in
  ``chunk_boundaries``.

Runs cassette-only, no live judge, canonical tier.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.utils.scripted_llm_client import ScriptedLLMClient
from tolokaforge.core.grading.judge_kinds import ChunkedRubricJudgeKind
from tolokaforge.core.grading.judge_result import JudgeStatus
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.plugin_registry import load_judge_kind
from tolokaforge.runner.models import Criterion, Rubric

pytestmark = pytest.mark.canonical


_JUDGE_MODEL = ModelConfig(provider="openai", name="gpt-4o-mini", temperature=0.0)


class _QueuedProvider:
    """Scripted :class:`JudgeModelProvider` popping one client per ``build``."""

    def __init__(self, clients: list[ScriptedLLMClient]) -> None:
        self._clients = list(clients)

    def build(self, model_config: ModelConfig):  # noqa: ARG002 — mirrors Protocol
        return self._clients.pop(0)


def _thirty_criterion_rubric() -> Rubric:
    return Rubric(
        criteria=[
            Criterion(id=f"c{i:02d}", description=f"criterion {i}", kind="binary", weight=1.0)
            for i in range(30)
        ]
    )


def _submit_call(criteria: list[Criterion]) -> list:
    args: dict[str, Any] = {"reasons": "overall"}
    for c in criteria:
        args[c.id] = True
        args[f"{c.id}_justification"] = f"because {c.id}\nVERDICT: MET"
    return [("submit_report", args)]


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
        "logger": StructuredLogger(name="test-chunked-regression"),
    }


def test_single_shot_errors_on_truncated_thirty_criterion_report() -> None:
    """A single ``submit_report`` covering only 4 of 30 criteria → ERRORED."""
    rubric = _thirty_criterion_rubric()
    truncated_call = _submit_call(rubric.criteria[:4])
    single_shot_script = [truncated_call] * 20
    provider = _QueuedProvider([ScriptedLLMClient(single_shot_script)])

    single_shot = load_judge_kind("single_shot_rubric")()
    result = single_shot.evaluate(**_evaluate_kwargs(rubric, provider, None))

    assert result.status is JudgeStatus.ERRORED
    assert result.score is None
    assert result.criterion_results == ()


def test_chunked_completes_thirty_criterion_rubric() -> None:
    """Six 5-criterion chunks, each a clean ``submit_report`` → COMPLETED."""
    rubric = _thirty_criterion_rubric()
    chunks = [rubric.criteria[i : i + 5] for i in range(0, len(rubric.criteria), 5)]
    scripts = [[_submit_call(chunk)] for chunk in chunks]
    provider = _QueuedProvider([ScriptedLLMClient(script) for script in scripts])

    chunked = ChunkedRubricJudgeKind()
    result = chunked.evaluate(**_evaluate_kwargs(rubric, provider, {"chunk_size": 5}))

    assert result.status is JudgeStatus.COMPLETED
    assert result.score == pytest.approx(1.0)
    assert len(result.criterion_results) == 30
    assert [cr.id for cr in result.criterion_results] == [c.id for c in rubric.criteria]
    assert result.chunk_boundaries == tuple(tuple(c.id for c in chunk) for chunk in chunks)
    assert all(len(chunk_ids) == 5 for chunk_ids in result.chunk_boundaries)
