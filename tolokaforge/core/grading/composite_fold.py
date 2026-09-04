"""Shared substrate-neutral composite fold.

Owns the reduction of per-component scores + gate signals into a trial verdict
and its author-facing reasons string. Pure library: stdlib plus
``tolokaforge.core.grading.*`` inputs only — no ``tolokaforge.runner``, no
``tolokaforge.grader``, no ``tolokaforge.core.grading.substrate_live``, no
``pb2``. The ``composite-fold-purity`` import-linter contract enforces the
boundary.
"""

import logging
from dataclasses import dataclass
from typing import Any

from tolokaforge.core.grading.combine_method import (
    combine_by_method,
    validate_combine_method,
)
from tolokaforge.core.grading.combine_weights import (
    FoldedGrade,
    require_component_weight,
    resolve_uncounted_fold,
)
from tolokaforge.core.grading.golden_replay import (
    GoldenReplayRecord,
    incomplete_replay_reason,
)
from tolokaforge.core.grading.grade_components import (
    GRADE_COMPONENTS,
    component_requested,
    runner_score_field,
)
from tolokaforge.core.grading.state_composition import (
    CONFLICTING_STATE_SOURCES_MESSAGE,
    compose_state_checks_score,
    inert_hash_weight_reason,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StateChecksOutcome:
    """The runner's ``state_checks`` slot, and what its declared weight decided.

    ``component`` is ``None`` when no state source produced a score, which the
    combine treats differently from a ``0.0`` it would fold in as a failure.
    ``inert_weight_reason`` carries the author-facing note for a weight the fold
    never consulted, so the runner reports the skip the way the core engine does.
    """

    component: float | None
    inert_weight_reason: str | None


def resolve_state_checks_component(
    *,
    hash_score: float,
    jsonpath_score: float,
    db_probe_score: float,
    hash_weight: float | None,
) -> StateChecksOutcome:
    """Fold the runner's state sources into one ``state_checks`` score.

    Translates the runner's ``-1.0``-means-not-evaluated sentinel into the ``None``
    the shared composer reads. ``db_probes`` is the block's only state source: a probe
    score beside a hash verdict or a JSONPath score is two verdicts for one component
    with no declared share between them, so the pair is refused rather than one of them
    discarded. A probe deciding alone reports a declared weight as unconsulted, the way
    any single-source fold does.

    Reads the scores rather than a config, because that is what this fold holds; the
    same rule over the keys an author writes is
    ``refuse_probes_beside_another_state_source``.

    Raises:
        ValueError: a probe score arrived beside another source, carrying
            ``CONFLICTING_STATE_SOURCES_MESSAGE`` — raised before the weight is read, so
            a block being refused outright is never answered with a demand for a
            ``hash.weight``; or a hash verdict and a JSONPath score are both real and no
            ``hash_weight`` says how to fold them.
    """
    hash_source = None if hash_score < 0 else hash_score
    jsonpath_source = None if jsonpath_score < 0 else jsonpath_score
    probes_decide = db_probe_score >= 0
    if probes_decide and (hash_source is not None or jsonpath_source is not None):
        raise ValueError(CONFLICTING_STATE_SOURCES_MESSAGE)
    return StateChecksOutcome(
        component=(
            db_probe_score
            if probes_decide
            else compose_state_checks_score(
                hash_score=hash_source,
                jsonpath_score=jsonpath_source,
                hash_weight=hash_weight,
            )
        ),
        inert_weight_reason=inert_hash_weight_reason(
            hash_score=hash_source,
            jsonpath_score=jsonpath_source,
            hash_weight=hash_weight,
        ),
    )


def combine_grade_components(
    components: dict[str, Any], grading_config: dict[str, Any]
) -> FoldedGrade:
    """
    Combine component scores into final grade.

    Supports combination methods:
    - "all": All components must pass (score >= threshold)
    - "weighted": Weighted average of component scores
    - "any": Any component passing is sufficient

    Every evaluated component carries a declared weight or the fold raises, and a fold
    with no weighted evaluated component decides before the aggregation and reports why
    — the two rules core's own fold applies, from the one shared definition.

    Args:
        components: Dict with component scores:
            {
                "hash_match": bool,
                "hash_score": float,
                "transcript_pass": bool,
                "transcript_score": float,
            }
        grading_config: Grading configuration from task description:
            {
                "combine_method": "all" | "weighted" | "any",
                "weights": {"state_checks": 1.0, "transcript_rules": 0.5},
                "pass_threshold": 1.0,
                "state_checks": {"hash_weight": 0.6}
            }

    Returns:
        The verdict, carrying the reason where the fold counted nothing.

    Raises:
        MissingComponentWeight: an evaluated component ``combine.weights`` declares no
            share for.
        ValueError: a hash verdict and a JSONPath score are both real and
            ``state_checks.hash_weight`` does not say how to fold them; or
            ``combine_method`` is missing or names no supported aggregation.
    """
    # Ahead of the zero-active-components return below, which never reaches the fold:
    # a request naming no supported aggregation must fail the grade rather than take
    # that path's verdict.
    method = validate_combine_method(
        grading_config.get("combine_method"), context="grading config combine_method"
    )
    weights = grading_config.get("weights", {})
    threshold = grading_config.get("pass_threshold", 1.0)

    # Determine which components are active (score >= 0 means evaluated)
    active_components: dict[str, float] = {}
    state_checks_slot = resolve_state_checks_component(
        hash_score=components.get("hash_score", -1.0),
        jsonpath_score=components.get("jsonpath_score", -1.0),
        db_probe_score=components.get("db_probe_score", -1.0),
        hash_weight=(grading_config.get("state_checks") or {}).get("hash_weight"),
    )
    if state_checks_slot.component is not None:
        active_components["state_checks"] = state_checks_slot.component
    for spec in GRADE_COMPONENTS:
        # state_checks is the composed slot resolved above; it has no single field here.
        if spec.runner_score_field is None:
            continue
        score = components.get(spec.runner_score_field, -1.0)
        if score >= 0:
            active_components[spec.name] = score

    shares = {name: require_component_weight(name, weights) for name in active_components}

    # A refusal task (empty golden_actions), a misconfigured pack and a deliberately
    # non-scoring one are three different answers, and the shared rule tells them apart.
    uncounted = resolve_uncounted_fold(
        scored=active_components,
        requested={
            spec.name
            for spec in GRADE_COMPONENTS
            if component_requested(spec, grading_config.get(spec.config_section))
        },
        weights=weights,
        method=method,
    )
    if uncounted is not None:
        if uncounted.reason:
            logger.warning("Grading counted nothing — failing: %s", uncounted.reason)
        return uncounted

    # Computed for every method, read only by ``weighted``: the shared dispatch
    # decides the aggregation and this substrate keeps its own mean.
    total_weight = sum(shares.values())
    weighted_sum = sum(score * shares[name] for name, score in active_components.items())
    score, binary_pass = combine_by_method(
        method=method,
        component_scores=active_components,
        weighted_mean=weighted_sum / total_weight if total_weight else 0.0,
        pass_threshold=threshold,
    )
    return FoldedGrade(score=score, binary_pass=binary_pass)


_JUDGE_SCORE_FIELD = runner_score_field("llm_judge")


@dataclass(frozen=True)
class TrialVerdict:
    """One trial's verdict: the two gates applied around the weighted fold.

    ``judge_component`` is the score the judge component carries *after* the required-criterion
    gate, which is what the wire grade and the reasons string report — not the weighted average
    the judge's own aggregate returned. ``reason`` is the fold's own sentence where it counted
    nothing, which no component's reasons would otherwise state.
    """

    judge_component: float
    score: float
    binary_pass: bool
    reason: str | None
    refusal: bool = False


def compose_trial_verdict(
    components: dict[str, Any],
    grading_config: dict[str, Any],
    *,
    judge_gate_failed: bool,
    trace_gate_failed: bool,
) -> TrialVerdict:
    """Fold ``components`` into a trial verdict, applying both gates around the combine.

    One rule, two gates, in the order they bind. A failed **required** rubric criterion is a
    hard fail of the judge *component*: its score is zeroed before the fold, so a high weighted
    average cannot rescue it and every downstream reader of the component sees the gate. A
    failed **trace** gate leaves the score alone and fails the trial outright. Either gate
    therefore fails the trial independently of ``pass_threshold`` and of how heavily any other
    component is weighted.

    ``components`` carries the judge's *raw* aggregate score under its runner field; the
    zeroing is this function's, so a caller reproducing a recorded verdict offline reaches the
    same verdict as the runner without repeating either gate. The core substrate composes
    independently (:mod:`~tolokaforge.core.grading.combine`) and never computes ``llm_judge``.

    Raises:
        ValueError: propagated from :func:`combine_grade_components` — an evaluated component
            with no declared weight, an undecidable ``state_checks`` fold, or a
            ``combine_method`` naming no supported aggregation.
    """
    folded = dict(components)
    if judge_gate_failed:
        folded[_JUDGE_SCORE_FIELD] = 0.0
    combined = combine_grade_components(folded, grading_config)
    return TrialVerdict(
        judge_component=folded.get(_JUDGE_SCORE_FIELD, -1.0),
        score=combined.score,
        binary_pass=combined.binary_pass and not (judge_gate_failed or trace_gate_failed),
        reason=combined.reason,
        refusal=combined.refusal,
    )


def build_grade_reasons(
    components: dict[str, Any],
    state_diff: dict[str, Any] | None = None,
    transcript_result: dict[str, Any] | None = None,
    judge_reasons: str | None = None,
    trace_checks_result: dict[str, Any] | None = None,
    golden_replay: GoldenReplayRecord | None = None,
    custom_checks_reasons: str | None = None,
) -> str:
    """
    Build human-readable reasons string for the grade.

    Args:
        components: Component scores dict
        state_diff: State diff if hash comparison failed
        transcript_result: Transcript evaluation result
        trace_checks_result: Trace checks evaluation result
        golden_replay: The golden replay behind the hash verdict, when one ran. An
            incomplete replay is named beside the verdict it produced, in the sentence
            the core engine emits too.
        custom_checks_reasons: The custom-checks suite's own account, rendered by
            :func:`~tolokaforge.core.grading.checks_helpers.custom_checks_reason`.
            Passed on the strength of the evaluator having something to say rather
            than on the component's score, so a suite that failed to run says why
            even though it scored nothing.

    Returns:
        The scored components' segments, joined — and empty where the trial scored
        none of them. A grade for such a trial is not silent: the fold decides it
        without reading a score and its own sentence says why, which the caller
        appends. A placeholder here would state the opposite of what a renderer
        omission means, and would be a second producer of the same account.
    """
    reasons = []

    # State checks reason — hash
    hash_score = components.get("hash_score", -1.0)
    if hash_score >= 0:
        if components.get("hash_match", False):
            reasons.append("State: hash match")
        else:
            if state_diff and state_diff.get("summary"):
                reasons.append(f"State: {state_diff['summary']}")
            else:
                reasons.append("State: hash mismatch")

    replay_reason = incomplete_replay_reason(golden_replay) if golden_replay is not None else None
    if replay_reason:
        reasons.append(replay_reason)

    # State checks reason — jsonpath file assertions
    jsonpath_score = components.get("jsonpath_score", -1.0)
    if jsonpath_score >= 0:
        jsonpath_reasons = components.get("jsonpath_reasons", "")
        if jsonpath_reasons:
            reasons.append(f"JSONPath: {jsonpath_reasons}")
        elif jsonpath_score == 1.0:
            reasons.append("JSONPath: all checks passed")
        else:
            reasons.append(f"JSONPath: score={jsonpath_score:.2f}")

    # State checks reason — db probes
    db_probe_score = components.get("db_probe_score", -1.0)
    if db_probe_score >= 0:
        db_probe_reasons = components.get("db_probe_reasons", "")
        if db_probe_reasons:
            reasons.append(f"DB probes: {db_probe_reasons}")
        elif db_probe_score == 1.0:
            reasons.append("DB probes: all probes passed")
        else:
            reasons.append(f"DB probes: score={db_probe_score:.2f}")

    # Transcript rules reason
    transcript_score = components.get("transcript_score", -1.0)
    if transcript_score >= 0:
        if transcript_result:
            details = transcript_result.get("details", [])
            failures = [d for d in details if not d.get("passed")]
            total = len(details)
            if not failures:
                reasons.append(f"Transcript: all {total} rules passed")
            else:
                # Name every failing sub-check: "2 of 5 failed" alone leaves the
                # author guessing which rule and why.
                failure_text = "; ".join(
                    str(d.get("message", d.get("rule_type"))) for d in failures
                )
                reasons.append(
                    f"Transcript: {len(failures)} of {total} rules failed — {failure_text}"
                )
        else:
            if components.get("transcript_pass", False):
                reasons.append("Transcript: passed")
            else:
                reasons.append("Transcript: failed")

    # Trace checks reason — the score and the route it was scored on, the gates
    # that shut, then every failing constraint by name. The gate and constraint
    # lines are the ones core's engine emits too, so a grade reads the same on
    # both substrates.
    trace_checks_score = components.get("trace_checks_score", -1.0)
    if trace_checks_score >= 0:
        trace_checks = trace_checks_result or {}
        route = trace_checks.get("winning_path") or ""
        reasons.append(
            f"Trace checks: score={trace_checks_score:.2f}" + (f" (route {route})" if route else "")
        )
        failed_gate_ids = trace_checks.get("failed_gate_ids") or []
        if failed_gate_ids:
            reasons.append(f"FAILED trace gates: {', '.join(failed_gate_ids)}")
        reasons.extend(
            f"Trace check {item['id']}: {item['message']}"
            for item in trace_checks.get("constraints", [])
            if not item["passed"]
        )

    # LLM judge reason
    llm_judge_score = components.get("llm_judge_score", -1.0)
    if llm_judge_score >= 0:
        if judge_reasons:
            reasons.append(f"Judge: score={llm_judge_score:.2f} ({judge_reasons})")
        else:
            reasons.append(f"Judge: score={llm_judge_score:.2f}")

    # Custom checks reason — registry order puts it last, and the segment is the
    # shared renderer's output verbatim so the two substrates carry one text.
    if custom_checks_reasons:
        reasons.append(custom_checks_reasons)

    return " | ".join(reasons)


@dataclass(frozen=True)
class CompositeFoldResult:
    """The neutral result of the shared composite fold.

    Both runner and grader receive this and each does its own wire encoding.
    ``state_checks_component`` carries ``None`` for "not evaluated"; the runner
    projects that to its wire ``-1.0`` sentinel, the grader keeps the ``None``
    on the Python model. ``judge_component`` is the score the judge component
    carries *after* the required-criterion gate — every downstream reader sees
    the gated value, not the judge's raw aggregate.
    """

    score: float
    binary_pass: bool
    verdict_reason: str | None
    judge_component: float
    state_checks_component: float | None
    inert_weight_reason: str | None
    reasons: str
    refusal: bool = False


class CompositeFold:
    """Shared substrate-neutral fold. One definition; two dispatchers.

    Both the runner-side ``_grade_trial_async`` and the grader-side
    ``_run_composite`` produce component scores and gate signals; each passes
    them through :meth:`finalise` and projects the returned
    :class:`CompositeFoldResult` onto its own wire type. The runner encodes
    into ``pb2.GradeTrialResponse`` and translates ``state_checks_component ==
    None`` into ``-1.0`` on the wire; the grader constructs a Python
    :class:`~tolokaforge.core.models.Grade` and keeps ``None``.
    """

    @staticmethod
    def finalise(
        *,
        components_dict: dict[str, Any],
        grading_config_dict: dict[str, Any],
        hash_weight: float | None,
        judge_gate_failed: bool,
        trace_gate_failed: bool,
        state_diff_dict: dict[str, Any] | None = None,
        transcript_result_dict: dict[str, Any] | None = None,
        judge_reasons: str | None = None,
        trace_checks_result_dict: dict[str, Any] | None = None,
        golden_replay: GoldenReplayRecord | None = None,
        custom_checks_reasons: str | None = None,
        judge_errored: bool = False,
        ledger_skip_notes: list[str] | None = None,
    ) -> CompositeFoldResult:
        """Fold verdict + state-checks slot + reasons in one call.

        Order:

        1. :func:`resolve_state_checks_component` — reduce the runner's hash /
           jsonpath / db_probes scores into one ``state_checks`` slot, keeping
           the ``None``-means-not-evaluated sentinel out of the aggregate.
        2. :func:`compose_trial_verdict` — apply the judge and trace gates
           around the weighted combine.
        3. Reassign ``components_dict['llm_judge_score']`` to the gated
           component so :func:`build_grade_reasons` reads the gated value that
           reaches the wire, not the judge's raw aggregate. Callers own the
           dict — both dispatchers pass a fresh ``model_dump()`` copy.
        4. :func:`build_grade_reasons` on the mutated components + ancillary
           inputs (state diff, transcript result, trace-check result, golden
           replay, custom-checks reasons).
        5. Append tail segments — ``"JUDGE ERRORED: <reasons>"`` (when
           ``judge_errored``), ``ledger_skip_notes`` joined with ``"; "``,
           ``inert_weight_reason``, ``verdict.reason`` — and join every
           non-empty segment with ``" | "``.

        Raises:
            ValueError: propagated from :func:`resolve_state_checks_component`
                or :func:`compose_trial_verdict` — an undecidable state-source
                fold, an evaluated component with no declared weight, or a
                ``combine_method`` naming no supported aggregation. Both
                dispatchers translate this to a ``success=False`` response
                naming the trial.
        """
        state_slot = resolve_state_checks_component(
            hash_score=components_dict.get("hash_score", -1.0),
            jsonpath_score=components_dict.get("jsonpath_score", -1.0),
            db_probe_score=components_dict.get("db_probe_score", -1.0),
            hash_weight=hash_weight,
        )
        verdict = compose_trial_verdict(
            components_dict,
            grading_config_dict,
            judge_gate_failed=judge_gate_failed,
            trace_gate_failed=trace_gate_failed,
        )
        components_dict[_JUDGE_SCORE_FIELD] = verdict.judge_component
        base_reasons = build_grade_reasons(
            components_dict,
            state_diff_dict,
            transcript_result_dict,
            judge_reasons=judge_reasons or None,
            trace_checks_result=trace_checks_result_dict,
            golden_replay=golden_replay,
            custom_checks_reasons=custom_checks_reasons,
        )
        segments = [base_reasons]
        if judge_errored:
            segments.append(f"JUDGE ERRORED: {judge_reasons}")
        if ledger_skip_notes:
            segments.append("; ".join(ledger_skip_notes))
        if state_slot.inert_weight_reason:
            segments.append(state_slot.inert_weight_reason)
        if verdict.reason:
            segments.append(verdict.reason)
        return CompositeFoldResult(
            score=verdict.score,
            binary_pass=verdict.binary_pass,
            verdict_reason=verdict.reason,
            judge_component=verdict.judge_component,
            state_checks_component=state_slot.component,
            inert_weight_reason=state_slot.inert_weight_reason,
            reasons=" | ".join(segment for segment in segments if segment),
            refusal=verdict.refusal,
        )
