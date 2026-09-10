"""``SingleShotRubricJudgeKind`` — byte-parity with :class:`LLMJudgeRubricEvaluator`.

Drives one fixture's inputs through:

(a) the pre-seam :class:`LLMJudgeRubricEvaluator` path (kept alive by the
    still-registered ``tolokaforge.rubric_evaluators`` group), and
(b) the new ``load_judge_kind("single_shot_rubric")()`` path.

Asserts both :class:`JudgeResult` instances match field-by-field on the
same seed + same scripted judge model. Locks the "no observable
behaviour change" contract this seam rewire commits to.

**State-sharing warning:** :class:`ScriptedLLMClient` advances an index
on each ``.generate()`` and ``_ScriptedJudgeModelProvider.build()``
caches its client, so the two legs MUST NOT share one provider instance
— this suite builds a fresh :class:`ScriptedLLMClient` (seeded from the
same script list) and a fresh ``_ScriptedJudgeModelProvider(client)``
for each leg, otherwise the second leg reads exhausted-script output
and byte-parity is spuriously violated.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.utils.scripted_llm_client import ScriptedLLMClient
from tolokaforge.core.grading.default_rubric_evaluator import LLMJudgeRubricEvaluator
from tolokaforge.core.grading.judge_result import JudgeResult, JudgeStatus
from tolokaforge.core.grading.substrate import InProcessGradingSubstrate
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.plugin_registry import load_judge_kind
from tolokaforge.runner.models import (
    Criterion,
    LLMJudgeConfig,
    Rubric,
)

pytestmark = pytest.mark.canonical


_JUDGE_MODEL = ModelConfig(provider="openai", name="gpt-4o-mini", temperature=0.0)


class _ScriptedJudgeModelProvider:
    """Test :class:`JudgeModelProvider` — returns a preloaded scripted client
    as the ``JudgeModel``. Bypasses the shipped ``litellm`` transport so the
    canonical suite drives the judge loop deterministically."""

    def __init__(self, client: ScriptedLLMClient) -> None:
        self._client = client

    def build(self, model_config: ModelConfig):
        return self._client


def _rubric() -> Rubric:
    """Two non-required criteria (binary weight 2.0 + graded weight 1.0)."""
    return Rubric(
        criteria=[
            Criterion(
                id="refund_done",
                description="Refund issued",
                kind="binary",
                weight=2.0,
            ),
            Criterion(
                id="tone",
                description="Polite tone",
                kind="graded",
                weight=1.0,
            ),
        ]
    )


def _submit_args(**criteria: Any) -> dict[str, Any]:
    """Build a well-formed ``submit_report`` payload from ``{id: verdict}``."""
    args: dict[str, Any] = {"reasons": "overall summary"}
    for cid, verdict in criteria.items():
        args[cid] = verdict
        if isinstance(verdict, bool):
            marker = "VERDICT: MET" if verdict else "VERDICT: NOT MET"
        else:
            marker = f"SCORE: {verdict}"
        args[f"{cid}_justification"] = f"because {cid}\n{marker}"
    return args


def _substrate() -> InProcessGradingSubstrate:
    """Substrate exposing the reads the judge touches: DB reader seam
    (the judge's read-only tools bridge to it); no KB, no filesystem."""
    return InProcessGradingSubstrate(
        db_reader=MagicMock(),
        knowledge_search=None,
        filesystem_root=None,
        initial_state={},
        final_state={},
    )


def _logger() -> StructuredLogger:
    return StructuredLogger(name="test-judge-kind-byte-parity")


def _script() -> list[Any]:
    """One-turn submit_report: refund_done=True, tone=1.0."""
    return [[("submit_report", _submit_args(refund_done=True, tone=1.0))]]


def _run_pre_seam(config: LLMJudgeConfig) -> JudgeResult:
    """Pre-seam path: :class:`LLMJudgeRubricEvaluator` (still registered
    under ``tolokaforge.rubric_evaluators`` — the group is intentionally
    kept live so this parity check can drive both paths side by side)."""
    client = ScriptedLLMClient(_script())
    evaluator = LLMJudgeRubricEvaluator(
        _ScriptedJudgeModelProvider(client),
        disable_knowledge_search=False,
        custom_system_prompt=None,
        include_agent_system_prompt=True,
        logger=_logger(),
    )
    return evaluator.evaluate(
        rubric=config.rubric,
        agent_system_prompt="you are a refund agent",
        transcript=[
            {"role": "user", "content": "please refund me"},
            {"role": "assistant", "content": "refund processed"},
        ],
        substrate=_substrate(),
        judge_model_config=_JUDGE_MODEL,
        extra_read_tools=[],
        state_diff=None,
    )


def _run_new_seam(config: LLMJudgeConfig) -> JudgeResult:
    """New seam: ``load_judge_kind("single_shot_rubric")()`` — the
    :class:`SingleShotRubricJudgeKind` wraps the same
    :class:`LLMJudge` construction the pre-seam path uses. A fresh
    :class:`ScriptedLLMClient` is required so ``.generate()`` starts
    from script index 0 on this leg — see module docstring."""
    client = ScriptedLLMClient(_script())
    kind = load_judge_kind("single_shot_rubric")()
    substrate = _substrate()
    return kind.evaluate(
        rubric=config.rubric,
        agent_system_prompt="you are a refund agent",
        transcript=[
            {"role": "user", "content": "please refund me"},
            {"role": "assistant", "content": "refund processed"},
        ],
        db_reader=substrate.db_reader(),
        kb_search=substrate.knowledge_search(),
        workspace_dir=substrate.filesystem_root(),
        extra_read_tools=[],
        state_diff=None,
        judge_model_config=_JUDGE_MODEL,
        judge_model_provider=_ScriptedJudgeModelProvider(client),
        disable_knowledge_search=False,
        custom_system_prompt=None,
        include_agent_system_prompt=True,
        kind_config=None,
        logger=_logger(),
    )


def test_single_shot_kind_matches_rubric_evaluator_field_by_field() -> None:
    """The two legs produce field-identical :class:`JudgeResult` values
    on the same rubric + scripted judge, proving the seam rewire changes
    zero observable behaviour."""
    config = LLMJudgeConfig(rubric=_rubric())
    pre = _run_pre_seam(config)
    new = _run_new_seam(config)

    assert pre.status is JudgeStatus.COMPLETED
    assert new.status is JudgeStatus.COMPLETED
    assert pre.status == new.status
    assert pre.score == new.score
    assert pre.binary_pass == new.binary_pass
    assert pre.gate_failed == new.gate_failed
    assert pre.failed_required_ids == new.failed_required_ids
    assert pre.reasons == new.reasons
    assert pre.criterion_results == new.criterion_results
    assert pre.kb_tools_offered == new.kb_tools_offered
    assert pre.kb_tools_withheld == new.kb_tools_withheld
    assert pre.knowledge_search_disabled == new.knowledge_search_disabled
    assert pre.custom_system_prompt == new.custom_system_prompt
    assert pre.include_agent_system_prompt == new.include_agent_system_prompt
    assert pre.read_tools_offered == new.read_tools_offered
    assert pre.state_diff == new.state_diff
    assert pre.transcript == new.transcript
    assert pre.usage == new.usage
