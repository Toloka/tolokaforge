"""Canonical snapshot of the judge-replay per-run comparison report.

Builds a batch of synthetic replayed outcomes over recorded originals covering
every bucket — a matching pair, a disagreeing pair, an errored original, an
original with no verdict, and an errored replay — then snapshots
:func:`build_replay_report`'s output. Pins the agreement math (denominator over
``COMPARABLE`` criteria only), the aggregate ``llm_judge`` delta, the judge-only
usage totals, the bucket assignment, and the report shape in one golden file.

Regenerate the golden snapshot with:
    uv run pytest tests/canonical/test_replay_report.py --update-canon
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.core.grading.judge import JudgeResult, JudgeStatus, JudgeUsage
from tolokaforge.core.grading.replay import (
    FidelityMode,
    KnowledgeSearchMode,
    ProvenanceSource,
    ReplayOutcomeStatus,
    ReplayProvenance,
    TrialReplayOutcome,
    build_replay_report,
)
from tolokaforge.core.models import (
    CriterionResult as ModelCriterionResult,
)
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
)
from tolokaforge.core.models import (
    JudgeStatus as ModelJudgeStatus,
)
from tolokaforge.core.output.artifacts import FileArtifactWriter
from tolokaforge.runner.models import CriterionResult

pytestmark = pytest.mark.canonical

_JUDGE_MODEL = "openrouter/openai/gpt-4.1-mini"


def _provenance() -> ReplayProvenance:
    return ReplayProvenance(
        judge_model=_JUDGE_MODEL,
        judge_model_source=ProvenanceSource.OVERRIDE,
        rubric_source=ProvenanceSource.RECORDED,
        knowledge_search_mode=KnowledgeSearchMode.RECORDED,
        knowledge_search_disabled=False,
        custom_system_prompt=False,
        custom_prompt_source=None,
        fidelity_mode=FidelityMode.FULL,
    )


def _usage() -> JudgeUsage:
    return JudgeUsage(calls=1, prompt_tokens=100, completion_tokens=20, cost_usd=0.01)


def _write_original(
    source: Path,
    name: str,
    *,
    status: ModelJudgeStatus,
    criteria: list[ModelCriterionResult] | None,
    llm_judge: float,
) -> Path:
    bundle = source / "trials" / name / "0"
    FileArtifactWriter().write_grade(
        bundle,
        Grade(
            binary_pass=status is ModelJudgeStatus.COMPLETED,
            score=max(llm_judge, 0.0),
            components=GradeComponents(llm_judge=llm_judge),
            judge_status=status,
            criterion_results=criteria,
        ),
    )
    return bundle


def _completed_result(criteria: list[CriterionResult], score: float) -> JudgeResult:
    return JudgeResult(
        status=JudgeStatus.COMPLETED,
        usage=_usage(),
        reasons="replayed",
        score=score,
        binary_pass=score >= 0.5,
        criterion_results=tuple(criteria),
    )


def _errored_result() -> JudgeResult:
    return JudgeResult(status=JudgeStatus.ERRORED, usage=_usage(), reasons="judge crashed")


def _outcome(bundle: Path, result: JudgeResult) -> TrialReplayOutcome:
    return TrialReplayOutcome(
        bundle=bundle,
        status=ReplayOutcomeStatus.REPLAYED,
        provenance=_provenance(),
        result=result,
    )


def test_replay_report_snapshot(tmp_path: Path, canon_snapshot) -> None:
    source = tmp_path / "run"

    # A matching pair (comparable, agree) and a disagreeing pair (comparable).
    agree = _outcome(
        _write_original(
            source,
            "agree",
            status=ModelJudgeStatus.COMPLETED,
            criteria=[
                ModelCriterionResult(id="refund_amount", met=True, score=1.0, justification="j")
            ],
            llm_judge=1.0,
        ),
        _completed_result(
            [CriterionResult(id="refund_amount", met=True, score=1.0, justification="j")], 1.0
        ),
    )
    disagree = _outcome(
        _write_original(
            source,
            "disagree",
            status=ModelJudgeStatus.COMPLETED,
            criteria=[ModelCriterionResult(id="tone", met=True, score=1.0, justification="j")],
            llm_judge=1.0,
        ),
        _completed_result(
            [CriterionResult(id="tone", met=False, score=0.0, justification="j")], 0.0
        ),
    )
    # Original errored → excluded from agreement; only the replay side is carried.
    original_errored = _outcome(
        _write_original(
            source, "orig_errored", status=ModelJudgeStatus.ERRORED, criteria=None, llm_judge=-1.0
        ),
        _completed_result([CriterionResult(id="x", met=True, score=1.0, justification="j")], 1.0),
    )
    # Original completed but recorded no criteria → no verdict to diff against.
    original_no_verdict = _outcome(
        _write_original(
            source,
            "orig_no_verdict",
            status=ModelJudgeStatus.COMPLETED,
            criteria=None,
            llm_judge=-1.0,
        ),
        _completed_result([CriterionResult(id="y", met=True, score=1.0, justification="j")], 1.0),
    )
    # Replay errored → its own bucket, never a fabricated 0.
    replay_errored = _outcome(
        _write_original(
            source,
            "replay_errored",
            status=ModelJudgeStatus.COMPLETED,
            criteria=[ModelCriterionResult(id="z", met=True, score=1.0, justification="j")],
            llm_judge=1.0,
        ),
        _errored_result(),
    )

    report = build_replay_report(
        [agree, disagree, original_errored, original_no_verdict, replay_errored],
        source=source,
        replay_id="snap",
    )
    assert report is not None

    # Agreement is over the two COMPARABLE criteria only: 1 of 2 match.
    assert report.criteria_compared == 2
    assert report.criteria_agreed == 1
    assert report.agreement_rate == 0.5

    canon_snapshot("replay_report").assert_match(report.model_dump(mode="json"), "report.json")
