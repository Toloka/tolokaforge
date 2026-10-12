"""Unit tests for :class:`MultiTurnRubricJudgeKind`.

Locks the baked-in composition:

- One warm-up ``auto_anchored`` call + K=3 grading calls per evaluation.
- All K grading calls receive the identical synthetic anchored rubric
  (auto_anchored's warm-up cache is inside voted's K-loop).
- Non-empty ``kind_config`` raises :class:`ValueError` before any judge
  dispatch runs.
- The composition audit line lands as a prefix on
  :attr:`JudgeResult.reasons`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.utils.scripted_llm_client import ScriptedLLMClient
from tolokaforge.core.grading.judge_kinds import JudgeTrialOptions, MultiTurnRubricJudgeKind
from tolokaforge.core.grading.judge_kinds.auto_anchored import clear_anchor_cache
from tolokaforge.core.grading.judge_result import JudgeStatus
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import ModelConfig
from tolokaforge.runner.models import Criterion, Rubric

pytestmark = pytest.mark.unit


_JUDGE_MODEL = ModelConfig(provider="openai", name="gpt-4o-mini", temperature=0.0)


class _QueuedProvider:
    """Pops one scripted client per ``build`` call in queue order."""

    def __init__(self, clients: list[ScriptedLLMClient]) -> None:
        self._clients = list(clients)
        self.built: list[ScriptedLLMClient] = []

    def build(self, model_config: ModelConfig) -> ScriptedLLMClient:  # noqa: ARG002
        if not self._clients:
            raise AssertionError(
                "provider ran out of scripted clients; the kind built more clients "
                "than the test expected."
            )
        client = self._clients.pop(0)
        self.built.append(client)
        return client


class _CaptureWrappedKind:
    """Records every rubric it was called with; produces a COMPLETED stub result."""

    NAME = "capture_wrapped_kind"

    def __init__(self) -> None:
        self.rubrics_seen: list[Rubric] = []

    def evaluate(self, **kwargs: Any):
        from tolokaforge.core.grading.judge_result import JudgeResult, JudgeUsage
        from tolokaforge.runner.models import CriterionResult

        rubric: Rubric = kwargs["rubric"]
        self.rubrics_seen.append(rubric)
        return JudgeResult(
            status=JudgeStatus.COMPLETED,
            usage=JudgeUsage(calls=1),
            reasons="capture-stub",
            score=1.0,
            binary_pass=True,
            criterion_results=tuple(
                CriterionResult(
                    id=c.id,
                    met=True,
                    score=1.0,
                    justification=(
                        f"{c.id}\nVERDICT: MET" if c.kind == "binary" else f"{c.id}\nSCORE: 1.0"
                    ),
                )
                for c in rubric.criteria
            ),
        )


def _rubric_with_unanchored_graded() -> Rubric:
    return Rubric(
        criteria=[
            Criterion(id="mentions_id", description="Mentions the id.", kind="binary"),
            Criterion(id="clarity", description="Reads clearly.", kind="graded"),
        ]
    )


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


@pytest.fixture(autouse=True)
def _reset_cache():
    clear_anchor_cache()
    yield
    clear_anchor_cache()


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
        "options": JudgeTrialOptions(),
        "kind_config": kind_config,
        "logger": StructuredLogger(name="test-multi-turn"),
    }


def test_name_matches_entry_point() -> None:
    assert MultiTurnRubricJudgeKind.NAME == "multi_turn_rubric"


def test_dispatch_count_is_one_warmup_plus_K_grading() -> None:
    """The stack is voted(K=3) → auto_anchored → single_shot. Each
    ``.evaluate`` builds one client per sample (single_shot's shape),
    K=3 samples → 3 grading clients; auto_anchored's warm-up cache is
    inside voted's K-loop so the warm-up client fires exactly once on
    the first sample. Total: 1 + 3 = 4 client builds."""
    rubric = _rubric_with_unanchored_graded()
    anchor_map = {"clarity": "one clear paragraph naming the entity"}
    clients = [
        ScriptedLLMClient([json.dumps(anchor_map)]),  # warm-up
        ScriptedLLMClient([_submit_call(rubric.criteria)]),  # sample 0 grading
        ScriptedLLMClient([_submit_call(rubric.criteria)]),  # sample 1 grading
        ScriptedLLMClient([_submit_call(rubric.criteria)]),  # sample 2 grading
    ]
    provider = _QueuedProvider(clients)

    result = MultiTurnRubricJudgeKind().evaluate(**_evaluate_kwargs(rubric, provider))

    assert result.status is JudgeStatus.COMPLETED
    assert len(provider.built) == 4
    assert provider._clients == []  # every scripted client was consumed


def test_non_empty_kind_config_raises() -> None:
    rubric = _rubric_with_unanchored_graded()
    provider = _QueuedProvider([])

    with pytest.raises(ValueError) as exc_info:
        MultiTurnRubricJudgeKind().evaluate(
            **_evaluate_kwargs(rubric, provider, kind_config={"foo": "bar"})
        )
    message = str(exc_info.value)
    assert "multi_turn_rubric" in message
    assert "kind_config" in message


def test_empty_mapping_kind_config_is_accepted() -> None:
    """An explicitly-empty mapping is not a request to override anything;
    honor it as a no-op (matches the ``None`` shape)."""
    rubric = _rubric_with_unanchored_graded()
    anchor_map = {"clarity": "one clear paragraph naming the entity"}
    clients = [
        ScriptedLLMClient([json.dumps(anchor_map)]),
        ScriptedLLMClient([_submit_call(rubric.criteria)]),
        ScriptedLLMClient([_submit_call(rubric.criteria)]),
        ScriptedLLMClient([_submit_call(rubric.criteria)]),
    ]
    provider = _QueuedProvider(clients)

    result = MultiTurnRubricJudgeKind().evaluate(
        **_evaluate_kwargs(rubric, provider, kind_config={})
    )
    assert result.status is JudgeStatus.COMPLETED


def test_all_K_samples_see_the_same_anchored_rubric(monkeypatch) -> None:
    """Wire a capture kind under the ``single_shot_rubric`` name so
    voted's K wrapped-kind calls all dispatch to it. Each dispatch's
    rubric should have ``clarity.expected`` filled from the warm-up
    anchor map."""
    from tolokaforge.core.grading.judge_kinds import (
        AutoAnchoredRubricJudgeKind,
        VotedRubricJudgeKind,
    )

    capture = _CaptureWrappedKind()

    class _CaptureCls:
        NAME = "single_shot_rubric"

        def evaluate(self, **kwargs: Any):
            return capture.evaluate(**kwargs)

    dispatch_table = {
        "single_shot_rubric": _CaptureCls,
        "auto_anchored_rubric": AutoAnchoredRubricJudgeKind,
        "voted_rubric": VotedRubricJudgeKind,
    }

    def _fake_load(name: str):
        return dispatch_table[name]

    monkeypatch.setattr("tolokaforge.core.plugin_registry.load_judge_kind", _fake_load)

    rubric = _rubric_with_unanchored_graded()
    anchor_map = {"clarity": "one clear paragraph naming the entity"}
    # Only the warm-up call is a real LLM dispatch; the K grading dispatches
    # route to the capture kind above and never touch the provider.
    provider = _QueuedProvider([ScriptedLLMClient([json.dumps(anchor_map)])])

    MultiTurnRubricJudgeKind().evaluate(**_evaluate_kwargs(rubric, provider))

    assert len(capture.rubrics_seen) == 3
    for observed in capture.rubrics_seen:
        by_id = {c.id: c for c in observed.criteria}
        assert by_id["clarity"].expected == anchor_map["clarity"]


def test_audit_prefix_names_the_composition() -> None:
    rubric = _rubric_with_unanchored_graded()
    anchor_map = {"clarity": "one clear paragraph"}
    clients = [
        ScriptedLLMClient([json.dumps(anchor_map)]),
        ScriptedLLMClient([_submit_call(rubric.criteria)]),
        ScriptedLLMClient([_submit_call(rubric.criteria)]),
        ScriptedLLMClient([_submit_call(rubric.criteria)]),
    ]
    provider = _QueuedProvider(clients)

    result = MultiTurnRubricJudgeKind().evaluate(**_evaluate_kwargs(rubric, provider))
    assert result.reasons.startswith("multi_turn_rubric composition:")
    assert "voted(n=3, geometric_median)" in result.reasons
    assert "auto_anchored" in result.reasons
    assert "single_shot" in result.reasons
