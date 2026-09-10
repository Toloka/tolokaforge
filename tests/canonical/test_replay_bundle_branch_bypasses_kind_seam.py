"""Escape-hatch predicate lock — ``explicit_system_prompt is not None``
short-circuits :func:`replay_trial` to a direct :class:`LLMJudge`,
bypassing the :class:`JudgeKind` seam.

Direct probe: monkeypatch
``tolokaforge.core.grading.replay.load_judge_kind`` to raise if the seam
is reached. With ``explicit_system_prompt`` set the raise must NOT
trigger — the escape-hatch predicate takes the direct-``LLMJudge``
branch. With ``explicit_system_prompt=None`` the raise MUST trigger —
the kind seam is the correct dispatch. A future contributor deleting
the ``if inputs.explicit_system_prompt is not None`` short-circuit
would silently route every bundle-branch trial through
:meth:`JudgeKind.evaluate`, which has no matching
``explicit_system_prompt`` kwarg today; this test fails loudly instead
(:meth:`JudgeKind.evaluate` widening tracked at #1583).
"""

from __future__ import annotations

import pytest

from tests.unit.grading.test_judge import ScriptedClient
from tolokaforge.core.grading import replay as replay_mod
from tolokaforge.core.grading.judge_result import JudgeStatus as JudgeRunStatus
from tolokaforge.core.grading.replay import (
    FidelityMode,
    KnowledgeSearchMode,
    ProvenanceSource,
    ReplayInputs,
    ReplayProvenance,
    replay_trial,
)
from tolokaforge.core.models import ModelConfig
from tolokaforge.runner.models import Criterion, Rubric

pytestmark = pytest.mark.canonical


_JUDGE_MODEL = ModelConfig(provider="openrouter", name="openai/gpt-4.1-mini", temperature=0.0)


def _submit_report_step(chunk_ids: tuple[str, ...]) -> list[tuple[str, dict]]:
    args: dict = {"reasons": "verdicts"}
    for cid in chunk_ids:
        args[cid] = True
        args[f"{cid}_justification"] = f"{cid}: VERDICT: MET"
    return [("submit_report", args)]


def _rubric() -> Rubric:
    return Rubric(
        criteria=[
            Criterion(id="a", description="a", kind="binary"),
            Criterion(id="b", description="b", kind="binary"),
        ]
    )


def _provenance() -> ReplayProvenance:
    return ReplayProvenance(
        judge_model="openrouter/openai/gpt-4.1-mini",
        judge_model_source=ProvenanceSource.RECORDED,
        rubric_source=ProvenanceSource.RECORDED,
        knowledge_search_mode=KnowledgeSearchMode.RECORDED,
        knowledge_search_disabled=False,
        custom_system_prompt=False,
        custom_prompt_source=None,
        judge_prompt_source=None,
        include_agent_system_prompt=True,
        agent_prompt_source=None,
        fidelity_mode=FidelityMode.FULL,
        judge_kind="chunked_rubric",
        judge_kind_source=ProvenanceSource.RECORDED,
    )


def _inputs(*, explicit_system_prompt: str | None) -> ReplayInputs:
    return ReplayInputs(
        rubric=_rubric(),
        agent_system_prompt="You are the agent.",
        transcript=[{"role": "user", "content": "Refund."}],
        state_diff=None,
        judge_model_config=_JUDGE_MODEL,
        disable_knowledge_search=False,
        custom_system_prompt=None,
        explicit_system_prompt=explicit_system_prompt,
        include_agent_system_prompt=True,
        judge_kind="chunked_rubric",
        kind_config={"chunk_size": 5},
        provenance=_provenance(),
    )


class TestEscapeHatchTaken:
    """``explicit_system_prompt is not None`` → replay dispatches through
    the direct :class:`LLMJudge` branch, never resolving through the
    :class:`JudgeKind` seam."""

    def test_explicit_prompt_short_circuits_load_judge_kind(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(_name: str) -> None:
            raise AssertionError(
                "load_judge_kind must NOT be called when explicit_system_prompt is set"
            )

        monkeypatch.setattr(replay_mod, "load_judge_kind", _raise)

        # With chunk_size=5 and 2 criteria, single_shot semantics via direct
        # LLMJudge produces one submit_report — the escape hatch bypasses the
        # chunked kind entirely (that's the whole point of the short-circuit).
        client = ScriptedClient([_submit_report_step(("a", "b"))])
        result = replay_trial(
            _inputs(explicit_system_prompt="recorded composed prompt"),
            judge_client=client,
        )

        assert result.status is JudgeRunStatus.COMPLETED


class TestKindSeamTaken:
    """``explicit_system_prompt is None`` → replay resolves through
    ``load_judge_kind`` and dispatches on the seam. A monkeypatch that
    raises there catches the seam being reached."""

    def test_no_explicit_prompt_reaches_load_judge_kind(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        marker = "load_judge_kind was reached"

        def _raise(_name: str) -> None:
            raise AssertionError(marker)

        monkeypatch.setattr(replay_mod, "load_judge_kind", _raise)

        client = ScriptedClient([_submit_report_step(("a", "b"))])
        with pytest.raises(AssertionError, match=marker):
            replay_trial(_inputs(explicit_system_prompt=None), judge_client=client)
