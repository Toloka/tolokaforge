"""Calibration runner — drives the REAL rubric judge over golden fixtures.

For each fixture this:

1. builds an in-memory, jsonpath-evaluating :class:`DictDBReader` over the
   fixture's ``final_db_state`` (the exact live-judge-test pattern, reused so
   calibration runs the real judge with no runner / gRPC stack);
2. calls :meth:`tolokaforge.core.grading.judge.LLMJudge.run`;
3. on COMPLETED, pairs each criterion's judge verdict against the fixture's human
   label into a :class:`~rubric_calibrator.metrics.CriterionObservation`;
4. on ERRORED, records the fixture id as a calibration *failure* (no pairs) — an
   errored judge is never silently scored.

The LLM call is confined here; the metric maths lives in ``metrics.py`` and is
tested without inference. A scripted ``llm_client`` may be injected (the same
``LoopLLMClient`` shape the judge unit tests use) to exercise this plumbing
deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tolokaforge.core.grading.judge import (
    JudgeResult,
    JudgeStatus,
    JudgeUsage,
    LLMJudge,
    model_config_from_ref,
)
from tolokaforge.core.grading.kb_search import KnowledgeSearch, RagServiceKnowledgeSearch

from .fixture import GoldenFixture
from .metrics import (
    CalibrationReport,
    CriterionObservation,
    binarise,
    build_report,
)


class DictDBReader:
    """In-memory read-only ``DBReader`` over a fixed final state.

    Reused verbatim in spirit from ``tests/integration/test_rubric_judge_live.py``
    so the judge can inspect final DB state with no runner stack. ``query``
    evaluates the JSONPath against the state (mirroring the production DB service)
    so a filtering judge gets real per-field answers rather than the whole blob.
    """

    def __init__(self, state: dict[str, Any]):
        self._state = state

    def get_state(self, tables: list[str] | None = None) -> dict[str, Any]:
        if tables:
            return {t: self._state.get(t, []) for t in tables}
        return self._state

    def query(self, jsonpath: str) -> dict[str, Any]:
        from jsonpath_ng.ext import parse

        matches = [m.value for m in parse(jsonpath).find(self._state)]
        return {"results": matches}


def _kb_search_for_fixture(fixture: GoldenFixture) -> KnowledgeSearch | None:
    """Build the per-trial ``KnowledgeSearch`` for a RAG-backed fixture, if any.

    The judge now consumes a backend-neutral :class:`KnowledgeSearch` (not a raw
    ``rag_url``). A fixture that declares ``rag_url`` is treated as a per-trial
    rag-service index keyed by the fixture id; absent ``rag_url`` ⇒ no KB tool
    (faithful gating). The fixture must have been indexed at ``rag_url`` under
    ``trial_id == fixture.id`` for the judge to retrieve hits.
    """
    if not fixture.rag_url:
        return None
    from tolokaforge.runner.rag_client import RAGServiceClient

    return RagServiceKnowledgeSearch(RAGServiceClient(base_url=fixture.rag_url), fixture.id)


@dataclass(frozen=True)
class FixtureOutcome:
    """The judge's outcome for one fixture, plus the paired observations.

    ``observations`` is empty when the judge errored (``errored is True``).
    ``judge_result`` is always carried for audit (its transcript / reasons are
    the disagreement-triage input the plan calls for).
    """

    fixture_id: str
    errored: bool
    judge_result: JudgeResult
    observations: tuple[CriterionObservation, ...]


def _pair_fixture(fixture: GoldenFixture, result: JudgeResult) -> list[CriterionObservation]:
    """Pair the judge's per-criterion verdicts against the human labels.

    Binarises both sides at the shared met/not-met threshold (graded scores) so
    accuracy and κ are computed on a single consistent label space. Fails loud if
    the judge omitted a criterion the rubric (and thus the fixture) requires — a
    COMPLETED judge that dropped a criterion is itself a calibration defect.
    """
    judged_by_id = {cr.id: cr for cr in result.criterion_results}
    kinds = {c.id: c.kind for c in fixture.rubric.criteria}

    observations: list[CriterionObservation] = []
    for criterion in fixture.rubric.criteria:
        cid = criterion.id
        if cid not in judged_by_id:
            raise ValueError(
                f"Judge COMPLETED for fixture {fixture.id!r} but omitted criterion {cid!r}."
            )
        judged = judged_by_id[cid]
        is_graded = kinds[cid] == "graded"
        expected_raw = fixture.expected_raw(cid)
        observations.append(
            CriterionObservation(
                fixture_id=fixture.id,
                criterion_id=cid,
                expected_met=binarise(expected_raw, is_graded=is_graded),
                judged_met=binarise(judged.score if is_graded else judged.met, is_graded=is_graded),
                expected_raw=expected_raw,
                judged_raw=judged.score if is_graded else judged.met,
                justification=judged.justification,
            )
        )
    return observations


def judge_fixture(
    fixture: GoldenFixture,
    *,
    model_ref: str,
    fixture_file: Path | None = None,
    llm_client: Any | None = None,
    max_turns: int | None = None,
    episode_timeout_s: int | None = None,
) -> FixtureOutcome:
    """Run the real judge on one fixture and pair its verdicts against the labels."""
    db_reader = DictDBReader(fixture.final_db_state) if fixture.final_db_state else None
    workspace_dir = fixture.workspace_path(fixture_file) if fixture_file else None
    kb_search = _kb_search_for_fixture(fixture)

    # The calibrator's CLI surface keeps a string ``--model-ref``; convert it to
    # a run-level ModelConfig at this boundary (the judge no longer takes a ref).
    # Model, injected client, and budgets are construction-time config; the
    # per-fixture evidence rides ``run()``.
    judge_kwargs: dict[str, Any] = {"llm_client": llm_client}
    if max_turns is not None:
        judge_kwargs["max_turns"] = max_turns
    if episode_timeout_s is not None:
        judge_kwargs["episode_timeout_s"] = episode_timeout_s

    result = LLMJudge(model_config_from_ref(model_ref), **judge_kwargs).run(
        rubric=fixture.rubric,
        agent_system_prompt=fixture.agent_system_prompt,
        transcript=fixture.transcript,
        db_reader=db_reader,
        kb_search=kb_search,
        workspace_dir=workspace_dir,
    )

    if result.status is not JudgeStatus.COMPLETED:
        return FixtureOutcome(
            fixture_id=fixture.id, errored=True, judge_result=result, observations=()
        )

    observations = _pair_fixture(fixture, result)
    return FixtureOutcome(
        fixture_id=fixture.id,
        errored=False,
        judge_result=result,
        observations=tuple(observations),
    )


@dataclass(frozen=True)
class CalibrationRun:
    """The full calibration result over a fixture set: report + outcomes + usage."""

    report: CalibrationReport
    outcomes: tuple[FixtureOutcome, ...]
    total_usage: JudgeUsage


def _sum_usage(outcomes: list[FixtureOutcome]) -> JudgeUsage:
    """Total the judge's own token usage / cost across every fixture run."""
    return JudgeUsage(
        calls=sum(o.judge_result.usage.calls for o in outcomes),
        prompt_tokens=sum(o.judge_result.usage.prompt_tokens for o in outcomes),
        completion_tokens=sum(o.judge_result.usage.completion_tokens for o in outcomes),
        reasoning_tokens=sum(o.judge_result.usage.reasoning_tokens for o in outcomes),
        cost_usd=sum(o.judge_result.usage.cost_usd for o in outcomes),
        tool_calls=sum(o.judge_result.usage.tool_calls for o in outcomes),
        consistency_rejections=sum(o.judge_result.usage.consistency_rejections for o in outcomes),
    )


def run_calibration(
    fixtures: list[tuple[Path, GoldenFixture]],
    *,
    model_ref: str,
    llm_client: Any | None = None,
    max_turns: int | None = None,
    episode_timeout_s: int | None = None,
    on_fixture_done: Any | None = None,
) -> CalibrationRun:
    """Run the judge over every fixture and assemble the calibration report.

    ``on_fixture_done(outcome)`` is an optional progress callback. ``llm_client``
    is shared across fixtures when injected (scripted tests); production passes
    ``None`` so each judge builds its own client from the ``ModelConfig`` derived
    from ``model_ref`` at the judge boundary.
    """
    outcomes: list[FixtureOutcome] = []
    all_observations: list[CriterionObservation] = []
    errored_ids: list[str] = []

    for fixture_file, fixture in fixtures:
        outcome = judge_fixture(
            fixture,
            model_ref=model_ref,
            fixture_file=fixture_file,
            llm_client=llm_client,
            max_turns=max_turns,
            episode_timeout_s=episode_timeout_s,
        )
        outcomes.append(outcome)
        if outcome.errored:
            errored_ids.append(outcome.fixture_id)
        else:
            all_observations.extend(outcome.observations)
        if on_fixture_done is not None:
            on_fixture_done(outcome)

    report = build_report(all_observations, errored_ids)
    return CalibrationRun(
        report=report,
        outcomes=tuple(outcomes),
        total_usage=_sum_usage(outcomes),
    )
