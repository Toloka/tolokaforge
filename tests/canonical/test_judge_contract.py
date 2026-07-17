"""Pin the ``Judge`` Protocol contract — runtime check + parity + fixture semantics.

Two implementations are checked: :class:`LLMJudge` (constructed with a run-level
``ModelConfig`` but never actually invoked on a real trial — ``run()`` would need
an LLM) and :class:`InMemoryJudge` (records calls + returns a configurable
:class:`JudgeResult` via the real ``aggregate_rubric``). Asserts on the Protocol
boundary, not on ``LLMJudge`` internals.
"""

from __future__ import annotations

import inspect

import pytest

from tolokaforge.core.grading.judge import (
    InMemoryJudge,
    Judge,
    JudgeCallLog,
    JudgeStatus,
    LLMJudge,
)
from tolokaforge.core.models import ModelConfig
from tolokaforge.runner.models import Rubric

pytestmark = pytest.mark.canonical


def _make_llm_judge() -> LLMJudge:
    """Build an :class:`LLMJudge` with a stub model config. Never invoked
    end-to-end in the contract tests — its presence is what matters for the
    Protocol check (a real ``run()`` needs an LLM)."""
    return LLMJudge(ModelConfig(provider="openai", name="gpt-4o-mini", temperature=0.0))


def _rubric() -> Rubric:
    """Two non-required criteria (binary weight 2.0 + graded weight 1.0) so the
    weighted aggregate is deterministic and distinguishes the verdict map."""
    return Rubric(
        criteria=[
            {"id": "refund_done", "description": "Refund issued", "kind": "binary", "weight": 2.0},
            {"id": "tone", "description": "Polite tone", "kind": "graded", "weight": 1.0},
        ]
    )


class TestProtocolRuntimeCheck:
    """The Protocol is ``@runtime_checkable``; both implementations satisfy it
    via ``isinstance`` (not just by structural type-hint compatibility)."""

    def test_llm_judge_passes_isinstance(self) -> None:
        assert isinstance(_make_llm_judge(), Judge)

    def test_in_memory_judge_passes_isinstance(self) -> None:
        assert isinstance(InMemoryJudge(), Judge)

    def test_random_object_does_not_pass_isinstance(self) -> None:
        class _NotAJudge:
            pass

        assert not isinstance(_NotAJudge(), Judge)


class TestRunMethodSignature:
    """All three surfaces expose the same ``run()`` parameter names (drop-in
    substitutability), and none of them carry a deterministic-oracle field."""

    def test_run_signatures_match_across_protocol_and_impls(self) -> None:
        protocol_params = list(inspect.signature(Judge.run).parameters)
        assert protocol_params == list(inspect.signature(LLMJudge.run).parameters)
        assert protocol_params == list(inspect.signature(InMemoryJudge.run).parameters)

    def test_run_surface_excludes_oracle_fields(self) -> None:
        params = set(inspect.signature(Judge.run).parameters)
        for forbidden in ("golden_actions", "expected_hash", "jsonpath_checks", "grading_config"):
            assert forbidden not in params


class TestInMemoryJudgeSemantics:
    """The in-memory judge records every ``run()`` and returns a configurable
    :class:`JudgeResult` aggregated through the real ``aggregate_rubric``."""

    def test_default_run_completes_with_full_score_and_records_call(self) -> None:
        judge = InMemoryJudge()
        result = judge.run(
            rubric=_rubric(),
            agent_system_prompt="",
            transcript=[{"role": "user", "content": "hi"}],
        )
        assert result.status is JudgeStatus.COMPLETED
        assert result.score == pytest.approx(1.0)
        assert {cr.id for cr in result.criterion_results} == {"refund_done", "tone"}
        assert len(judge.call_log.runs) == 1

    def test_verdict_map_produces_matching_aggregated_score(self) -> None:
        judge = InMemoryJudge(verdicts={"refund_done": True, "tone": 0.4})
        result = judge.run(rubric=_rubric(), agent_system_prompt="", transcript=[])
        # (2*1.0 + 1*0.4) / 3 = 0.8 — the same weighted mean aggregate_rubric computes.
        assert result.status is JudgeStatus.COMPLETED
        assert result.score == pytest.approx(0.8)

    def test_forced_error_returns_errored_with_no_score(self) -> None:
        judge = InMemoryJudge(force_errored=True)
        result = judge.run(rubric=_rubric(), agent_system_prompt="", transcript=[])
        assert result.status is JudgeStatus.ERRORED
        assert result.score is None
        assert result.criterion_results == ()

    def test_call_log_records_evidence_seam_presence(self) -> None:
        judge = InMemoryJudge()
        judge.run(
            rubric=_rubric(),
            agent_system_prompt="",
            transcript=[{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}],
            db_reader=object(),
            kb_search=object(),
            extra_read_tools=[object()],
            workspace_dir=None,
            state_diff="orders: 1 modified",
        )
        assert judge.call_log.runs == [
            {
                "criterion_ids": ("refund_done", "tone"),
                "transcript_len": 2,
                "db_reader": True,
                "kb_search": True,
                "extra_read_tools": True,
                "workspace_dir": False,
                "state_diff": True,
            }
        ]

    def test_fresh_judge_has_empty_call_log(self) -> None:
        assert InMemoryJudge().call_log == JudgeCallLog()
