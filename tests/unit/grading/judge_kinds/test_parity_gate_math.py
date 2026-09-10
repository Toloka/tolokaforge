"""Gate-math unit tests for :mod:`tolokaforge.core.grading.judge_kinds.parity`.

Drives synthetic paired verdicts through :func:`build_report` +
:func:`decide_parity_gate` (and through the harness's
:func:`measure_cross_kind_agreement` where the driver-shape belongs at
the unit tier) so the gate's per-criterion three-level policy is locked
independently of any LLM stack, cassette I/O, or corpus load.

Every case constructs :class:`CriterionObservation` values directly;
Cohen's κ is exercised through :func:`build_report`. The cases here are
intentionally the boundary cases the plan surfaces — a full-agreement
corpus, a disagreement below the block bar, a κ in the warn band, an
undefined κ, plus a hand-rolled cross-kind driver and the
``replays >= 2`` guard on :func:`measure_self_consistency`.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.grading.agreement import (
    CalibrationReport,
    CriterionObservation,
    build_report,
)
from tolokaforge.core.grading.judge_kinds.parity import (
    ParityCorpusEntry,
    ParityGateThresholds,
    decide_parity_gate,
    measure_cross_kind_agreement,
    measure_self_consistency,
)
from tolokaforge.core.grading.judge_result import JudgeResult, JudgeStatus, JudgeUsage
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import ModelConfig
from tolokaforge.runner.models import Criterion, CriterionResult, Rubric

pytestmark = pytest.mark.unit


_JUDGE_MODEL = ModelConfig(provider="openai", name="gpt-4o-mini", temperature=0.0)


def _thresholds() -> ParityGateThresholds:
    return ParityGateThresholds()


def _binary_rubric(*criterion_ids: str) -> Rubric:
    return Rubric(
        criteria=[
            Criterion(id=cid, description=f"criterion {cid}", kind="binary", weight=1.0)
            for cid in criterion_ids
        ]
    )


def _obs(
    obs_id: str,
    criterion_id: str,
    *,
    reference_met: bool,
    candidate_met: bool,
) -> CriterionObservation:
    return CriterionObservation(
        observation_id=obs_id,
        criterion_id=criterion_id,
        reference_met=reference_met,
        candidate_met=candidate_met,
        reference_raw=reference_met,
        candidate_raw=candidate_met,
        justification="",
    )


def _report_from(observations: list[CriterionObservation]) -> CalibrationReport:
    return build_report(observations, [])


# ---------------------------------------------------------------------------
# Case 1 — identity corpus, cross-kind and self-consistency both ship
# ---------------------------------------------------------------------------


def test_identity_corpus_ships_under_cross_kind() -> None:
    """Every criterion agrees on every observation with two-label variation →
    κ = 1.0 → every verdict is ``pass`` and the gate ships."""
    observations = [
        _obs("bundle0", "refund_done", reference_met=True, candidate_met=True),
        _obs("bundle0", "tone", reference_met=False, candidate_met=False),
        _obs("bundle1", "refund_done", reference_met=False, candidate_met=False),
        _obs("bundle1", "tone", reference_met=True, candidate_met=True),
    ]
    decision = decide_parity_gate(
        _report_from(observations),
        thresholds=_thresholds(),
        measurement="cross_kind",
    )
    assert decision.shippable is True
    assert decision.blocking_criteria == ()
    assert decision.warning_criteria == ()
    assert all(v.status == "pass" for v in decision.per_criterion)


def test_identity_corpus_ships_under_self_consistency() -> None:
    """Same identity corpus under the self-consistency measurement — the
    stricter 0.7 bar is still cleared by κ = 1.0."""
    observations = [
        _obs("bundle0@replay1", "refund_done", reference_met=True, candidate_met=True),
        _obs("bundle0@replay1", "tone", reference_met=False, candidate_met=False),
        _obs("bundle1@replay1", "refund_done", reference_met=False, candidate_met=False),
        _obs("bundle1@replay1", "tone", reference_met=True, candidate_met=True),
    ]
    decision = decide_parity_gate(
        _report_from(observations),
        thresholds=_thresholds(),
        measurement="self_consistency",
    )
    assert decision.shippable is True
    assert decision.blocking_criteria == ()
    assert all(v.status == "pass" for v in decision.per_criterion)


# ---------------------------------------------------------------------------
# Case 2 — κ below block bar under both measurements
# ---------------------------------------------------------------------------


def _half_agree_observations(criterion_id: str) -> list[CriterionObservation]:
    """Balanced-marginal 50/50 disagreement → κ = 0.0 (well below either block bar).

    The reference alternates T/F/T/F; the candidate mirrors it in the first
    half (agree) then flips in the second (disagree). Marginal met-rate is
    0.5 on both sides, so chance agreement p_e = 0.5, observed agreement
    p_o = 0.5 → κ = 0.0."""
    return [
        _obs("b0", criterion_id, reference_met=True, candidate_met=True),
        _obs("b1", criterion_id, reference_met=False, candidate_met=False),
        _obs("b2", criterion_id, reference_met=True, candidate_met=False),
        _obs("b3", criterion_id, reference_met=False, candidate_met=True),
    ]


def test_below_cross_kind_block_bar_blocks_and_names_criterion() -> None:
    observations = _half_agree_observations("refund_done")
    decision = decide_parity_gate(
        _report_from(observations),
        thresholds=_thresholds(),
        measurement="cross_kind",
    )
    assert decision.shippable is False
    assert decision.blocking_criteria == ("refund_done",)
    (verdict,) = decision.per_criterion
    assert verdict.criterion_id == "refund_done"
    assert verdict.status == "block"
    assert verdict.kappa == pytest.approx(0.0)
    assert "0.000" in verdict.reason
    assert "0.600" in verdict.reason  # warn threshold — the "block-if-below" bar
    assert "cross_kind" in verdict.reason


def test_below_self_consistency_block_bar_blocks() -> None:
    """κ = 0.0 fails the block band on the self-consistency measurement
    too — same reason shape, ``self_consistency`` surfaced in the reason
    string."""
    observations = _half_agree_observations("refund_done")
    decision = decide_parity_gate(
        _report_from(observations),
        thresholds=_thresholds(),
        measurement="self_consistency",
    )
    assert decision.shippable is False
    assert decision.blocking_criteria == ("refund_done",)
    (verdict,) = decision.per_criterion
    assert verdict.status == "block"
    assert "0.600" in verdict.reason
    assert "self_consistency" in verdict.reason


# ---------------------------------------------------------------------------
# Case 3 — warn band (kappa above warn, below pass) under each measurement
# ---------------------------------------------------------------------------


def _warn_band_observations(criterion_id: str, *, mismatches: int) -> list[CriterionObservation]:
    """Balanced-marginal corpus with ``mismatches`` swapped labels.

    Reference is 5 mets + 5 not-mets (marginal 0.5). Candidate is the same
    label on ``10 - mismatches`` items and the opposite label on
    ``mismatches`` items, chosen so the candidate's marginal stays 0.5
    (equal T→F and F→T flips). This keeps chance agreement p_e = 0.5, so
    κ = (p_o - 0.5) / 0.5 for any 10-observation corpus in this shape.
    """
    assert 0 <= mismatches <= 10 and mismatches % 2 == 0, "keep marginals balanced"
    obs: list[CriterionObservation] = []
    for i in range(5):
        candidate_met = i >= (mismatches // 2)
        obs.append(_obs(f"t{i}", criterion_id, reference_met=True, candidate_met=candidate_met))
    for i in range(5):
        candidate_met = i < (mismatches // 2)
        obs.append(_obs(f"f{i}", criterion_id, reference_met=False, candidate_met=candidate_met))
    return obs


def test_warn_band_cross_kind_is_shippable_but_reported() -> None:
    """κ ≈ 0.6 sits in the warn band [0.6, 0.8) for cross-kind — the
    criterion clears the block bar so the gate ships, but the id lands in
    ``warning_criteria`` so a reviewer sees the near-miss."""
    observations = _warn_band_observations("tone", mismatches=2)  # p_o = 0.8, κ = 0.6
    decision = decide_parity_gate(
        _report_from(observations),
        thresholds=_thresholds(),
        measurement="cross_kind",
    )
    assert decision.shippable is True
    assert decision.blocking_criteria == ()
    assert decision.warning_criteria == ("tone",)
    (verdict,) = decision.per_criterion
    assert verdict.status == "warn"
    assert verdict.kappa == pytest.approx(0.6)
    assert "0.600" in verdict.reason
    assert "0.800" in verdict.reason


def test_warn_band_self_consistency_uses_stricter_pass_bar() -> None:
    """Under ``self_consistency`` the pass bar drops to
    ``self_consistency_block`` (0.7), so the warn band shrinks to
    [0.6, 0.7). A criterion at κ ≈ 0.6 still warns; a criterion at
    κ = 1.0 clears the tighter bar as ``pass``."""
    warn_observations = _warn_band_observations("tone", mismatches=2)  # κ = 0.6
    decision = decide_parity_gate(
        _report_from(warn_observations),
        thresholds=_thresholds(),
        measurement="self_consistency",
    )
    assert decision.warning_criteria == ("tone",)
    (verdict,) = decision.per_criterion
    assert verdict.status == "warn"
    assert "0.700" in verdict.reason

    pass_observations = _warn_band_observations("tone", mismatches=0)  # κ = 1.0
    pass_decision = decide_parity_gate(
        _report_from(pass_observations),
        thresholds=_thresholds(),
        measurement="self_consistency",
    )
    assert pass_decision.warning_criteria == ()
    assert pass_decision.per_criterion[0].status == "pass"


# ---------------------------------------------------------------------------
# Case 4 — insufficient evidence (κ undefined)
# ---------------------------------------------------------------------------


def test_kappa_undefined_reports_insufficient_evidence() -> None:
    """A label-invariant corpus (one side never varies AND the two agree
    everywhere) has chance agreement 1.0, so Cohen's κ is undefined and
    :func:`build_report` returns ``kappa=None``. The gate must surface
    ``insufficient_evidence``, refuse to ship, and quote ``"undefined"``
    verbatim so a grep-based CI parser can find it."""
    observations = [
        _obs("b0", "refund_done", reference_met=True, candidate_met=True),
        _obs("b1", "refund_done", reference_met=True, candidate_met=True),
        _obs("b2", "refund_done", reference_met=True, candidate_met=True),
    ]
    decision = decide_parity_gate(
        _report_from(observations),
        thresholds=_thresholds(),
        measurement="cross_kind",
    )
    assert decision.shippable is False
    assert decision.blocking_criteria == ("refund_done",)
    (verdict,) = decision.per_criterion
    assert verdict.status == "insufficient_evidence"
    assert verdict.kappa is None
    assert "undefined" in verdict.reason


def test_single_observation_reports_insufficient_evidence() -> None:
    """n = 1 → :func:`cohen_kappa` returns ``None`` for that criterion.
    Verdict must be ``insufficient_evidence``, not ``pass``."""
    observations = [
        _obs("b0", "tone", reference_met=True, candidate_met=True),
    ]
    decision = decide_parity_gate(
        _report_from(observations),
        thresholds=_thresholds(),
        measurement="cross_kind",
    )
    (verdict,) = decision.per_criterion
    assert verdict.status == "insufficient_evidence"
    assert "undefined" in verdict.reason


# ---------------------------------------------------------------------------
# Case 5 — cross-kind driver produces one observation per (entry, criterion)
# ---------------------------------------------------------------------------


class _ScriptedFixtureKind:
    """Hand-rolled :class:`JudgeKind` — returns preset :class:`JudgeResult`
    values in call order. Bypasses :class:`LLMJudge` entirely so the
    driver-shape test stays at the unit tier.

    Each ``.evaluate`` call pops the next entry off ``scripted_results``;
    the harness calls ``.evaluate`` once per corpus entry per leg, so a
    two-entry corpus needs two scripted results per leg. Running past the
    script raises ``IndexError`` rather than silently returning a stale
    result — a wrong call count fails loud.
    """

    NAME: ClassVar[str] = "static-fixture"

    def __init__(self, scripted_results: list[dict[str, bool]]) -> None:
        self._results = list(scripted_results)
        self._call_index = 0

    def evaluate(
        self,
        *,
        rubric: Rubric,
        agent_system_prompt: str,  # noqa: ARG002
        transcript: list[dict[str, Any]],  # noqa: ARG002
        db_reader,  # noqa: ARG002
        kb_search,  # noqa: ARG002
        workspace_dir: Path | None,  # noqa: ARG002
        extra_read_tools: list,  # noqa: ARG002
        state_diff: str | None,  # noqa: ARG002
        judge_model_config: ModelConfig,  # noqa: ARG002
        judge_model_provider,  # noqa: ARG002
        disable_knowledge_search: bool,  # noqa: ARG002
        custom_system_prompt: str | None,  # noqa: ARG002
        include_agent_system_prompt: bool,  # noqa: ARG002
        kind_config: Mapping[str, Any] | None,  # noqa: ARG002
        logger: StructuredLogger,  # noqa: ARG002
    ) -> JudgeResult:
        verdicts = self._results[self._call_index]
        self._call_index += 1
        return JudgeResult(
            status=JudgeStatus.COMPLETED,
            usage=JudgeUsage(),
            reasons="unit-fixture",
            score=1.0,
            criterion_results=tuple(
                CriterionResult(
                    id=c.id,
                    met=verdicts[c.id],
                    score=1.0 if verdicts[c.id] else 0.0,
                    justification="unit-fixture",
                )
                for c in rubric.criteria
            ),
        )


def _entry(entry_id: str, rubric: Rubric) -> ParityCorpusEntry:
    return ParityCorpusEntry(
        entry_id=entry_id,
        rubric=rubric,
        agent_system_prompt="you are a refund agent",
        transcript=[{"role": "user", "content": "please refund me"}],
        state_diff=None,
        disable_knowledge_search=False,
        custom_system_prompt=None,
        include_agent_system_prompt=True,
        judge_scripts={},
    )


def test_cross_kind_driver_produces_one_observation_per_entry_criterion() -> None:
    """Two synthetic entries × two-criterion binary rubric. The reference
    and candidate kinds return canned :class:`JudgeResult` values in call
    order; the harness should build a report with one per-criterion row
    per criterion id, each with ``n = 2`` (one observation per bundle
    × two bundles)."""
    rubric = _binary_rubric("refund_done", "tone")
    corpus = [_entry("entry-A", rubric), _entry("entry-B", rubric)]
    reference = _ScriptedFixtureKind(
        scripted_results=[
            {"refund_done": True, "tone": True},
            {"refund_done": False, "tone": True},
        ]
    )
    candidate = _ScriptedFixtureKind(
        scripted_results=[
            {"refund_done": True, "tone": True},
            {"refund_done": False, "tone": False},
        ]
    )
    provider = MagicMock()

    report = measure_cross_kind_agreement(
        reference_kind=reference,
        candidate_kind=candidate,
        corpus=corpus,
        judge_model_config=_JUDGE_MODEL,
        reference_provider=provider,
        candidate_provider=provider,
    )

    n_by_criterion = {row.criterion_id: row.n for row in report.per_criterion}
    assert n_by_criterion == {"refund_done": 2, "tone": 2}
    assert report.errored_fixture_ids == ()
    assert report.total_observations == 4


def test_cross_kind_driver_flags_short_candidate_result() -> None:
    """A candidate whose ``criterion_results`` misses a rubric criterion is
    a partial pair — no observations from that entry, and the entry_id +
    the missing criterion land in ``errored_fixture_ids``. Silent partial
    pairs are the failure mode :func:`measure_cross_kind_agreement`
    refuses."""
    rubric = _binary_rubric("refund_done", "tone")
    corpus = [_entry("entry-A", rubric)]
    reference = _ScriptedFixtureKind(scripted_results=[{"refund_done": True, "tone": True}])

    class _ShortCandidate:
        NAME: ClassVar[str] = "short-candidate"

        def evaluate(self, **kwargs: Any) -> JudgeResult:
            return JudgeResult(
                status=JudgeStatus.COMPLETED,
                usage=JudgeUsage(),
                reasons="only graded one criterion",
                score=1.0,
                criterion_results=(
                    CriterionResult(id="refund_done", met=True, score=1.0, justification="ok"),
                ),
            )

    report = measure_cross_kind_agreement(
        reference_kind=reference,
        candidate_kind=_ShortCandidate(),
        corpus=corpus,
        judge_model_config=_JUDGE_MODEL,
        reference_provider=MagicMock(),
        candidate_provider=MagicMock(),
    )
    assert report.total_observations == 0
    assert len(report.errored_fixture_ids) == 1
    reason = report.errored_fixture_ids[0]
    assert "entry-A" in reason
    assert "tone" in reason


# ---------------------------------------------------------------------------
# Case 6 — self-consistency requires replays >= 2
# ---------------------------------------------------------------------------


def test_self_consistency_rejects_single_replay() -> None:
    with pytest.raises(ValueError) as excinfo:
        measure_self_consistency(
            kind_factory=lambda _i: _ScriptedFixtureKind(scripted_results=[]),
            corpus=[],
            replays=1,
            judge_model_config=_JUDGE_MODEL,
            provider_factory=lambda _i: MagicMock(),
        )
    message = str(excinfo.value)
    assert "replays >= 2" in message
    assert "kappa is undefined" in message.lower()
