"""Post-run fidelity rules over synthetic bundles.

Every fixture here is the shape of a real bundle: the clean ones are copied
from arms that passed a corpus audit, and each regression case reproduces a
bundle the audit found reporting a number nothing measured. The point of the
pairs is that the rule separates them — a rule that fires on the clean half is
worse than no rule, because an operator learns to route around it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from tolokaforge.core.failure_attribution import EXCLUDED_TYPED_REASONS
from tolokaforge.core.output.measurement_fidelity import (
    FidelityRule,
    check_run_bundle,
    expected_infrastructure_abort_keys,
)

_ABORT_KEYS = {reason.value: 0 for reason in EXCLUDED_TYPED_REASONS}

_CLEAN_COMPONENTS = {
    "state_checks": None,
    "transcript_rules": None,
    "trace_checks": None,
    "llm_judge": None,
    "custom_checks": 1.0,
}

_USAGE = {"prompt_tokens": 1276524, "completion_tokens": 24519, "reasoning_tokens": 20883}
_NO_USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}


def _write_bundle(
    root: Path,
    *,
    aggregate: dict[str, Any] | None = None,
    rows: list[dict[str, Any]] | None = None,
    trials: dict[tuple[str, str], tuple[dict[str, Any] | None, dict[str, Any]]] | None = None,
    task_yaml: dict[str, Any] | None = None,
) -> Path:
    """Lay out a run bundle: aggregate, per-task rows, and trial directories.

    *trials* maps ``(task_id, trial_index)`` to its ``grade.yaml`` payload
    (``None`` writes no grade) and its ``metrics.yaml`` payload.
    """
    if aggregate is not None:
        (root / "aggregate.json").write_text(json.dumps(aggregate), encoding="utf-8")
    if rows is not None:
        (root / "per_task_metrics.json").write_text(json.dumps(rows), encoding="utf-8")
    for (task_id, trial_index), (grade, metrics) in (trials or {}).items():
        trial_dir = root / "trials" / task_id / trial_index
        trial_dir.mkdir(parents=True, exist_ok=True)
        if grade is not None:
            (trial_dir / "grade.yaml").write_text(yaml.safe_dump(grade), encoding="utf-8")
        (trial_dir / "metrics.yaml").write_text(yaml.safe_dump(metrics), encoding="utf-8")
        (trial_dir / "task.yaml").write_text(
            yaml.safe_dump(task_yaml or _task_yaml(task_id)), encoding="utf-8"
        )
    return root


def _task_yaml(task_id: str, harness: str = "terminus-2") -> dict[str, Any]:
    return {
        "task_id": task_id,
        "model_config": {
            "agent": {
                "name": harness,
                "resolved": {"harness": harness, "model": "kimi-k2", "provider": "openrouter"},
            }
        },
    }


def _aggregate(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 8,
        "total_trials": 1,
        "measured_trials": 1,
        "scored_trials": 1,
        "infrastructure_aborts": dict(_ABORT_KEYS),
        "harness_errors": 0,
        "ungradeable": 0,
    }
    payload.update(overrides)
    return payload


def _row(task_id: str, **overrides: Any) -> dict[str, Any]:
    payload = _aggregate(task_id=task_id)
    payload.update(overrides)
    return payload


def _clean_bundle(tmp_path: Path) -> Path:
    """One task, one trial, a measured reward, real spend."""
    return _write_bundle(
        tmp_path,
        aggregate=_aggregate(),
        rows=[_row("fix-celery-chord-body-group-hang")],
        trials={
            ("fix-celery-chord-body-group-hang", "0"): (
                {
                    "binary_pass": True,
                    "score": 1.0,
                    "components": dict(_CLEAN_COMPONENTS),
                    "reasons": "test-execution reward: 1.0000",
                    "synthesized_by_termination_reason": None,
                },
                {"cost_usd": 0.4352164468, "turns": 52, "usage": dict(_USAGE)},
            )
        },
    )


def test_clean_bundle_has_no_violations(tmp_path: Path) -> None:
    assert check_run_bundle(_clean_bundle(tmp_path)) == []


def test_abort_keys_are_derived_from_excluded_typed_reasons() -> None:
    """R2's key set is the exclusion set, not a copy of it.

    A termination reason added to ``EXCLUDED_TYPED_REASONS`` becomes required
    here on the same commit, so it cannot become silently uncountable.
    """
    assert expected_infrastructure_abort_keys() == {
        reason.value for reason in EXCLUDED_TYPED_REASONS
    }
    assert expected_infrastructure_abort_keys()


# ---------------------------------------------------------------------------
# R1 — a grade may not assert a measurement it did not take
# ---------------------------------------------------------------------------


def test_r1_flags_a_score_with_nothing_measured_and_no_marker(tmp_path: Path) -> None:
    """Components all ``None`` says nothing ran; a score says something did.

    The shape an adapter lands on when it adopts the all-``None`` components
    half of the convention and keeps writing a zero.
    """
    bundle = _write_bundle(
        tmp_path,
        aggregate=_aggregate(),
        rows=[_row("fix-goroutine-leaks")],
        trials={
            ("fix-goroutine-leaks", "0"): (
                {
                    "binary_pass": False,
                    "score": 0.0,
                    "components": dict.fromkeys(_CLEAN_COMPONENTS),
                    "reasons": "trial produced no verifier reward",
                    "synthesized_by_termination_reason": None,
                },
                {"cost_usd": 0.51, "turns": 12, "usage": dict(_USAGE)},
            )
        },
    )
    rules = [v.rule for v in check_run_bundle(bundle)]
    assert FidelityRule.UNMEASURED_SCORE in rules


def test_r1_allows_a_declared_harness_synthesised_grade(tmp_path: Path) -> None:
    """A fabrication that declares itself is not a fabrication in hiding.

    The stuck-detection auto-fail: nothing was measured, the marker says so,
    and downstream can tell it from a measured zero.
    """
    bundle = _write_bundle(
        tmp_path,
        aggregate=_aggregate(),
        rows=[_row("fix-goroutine-leaks")],
        trials={
            ("fix-goroutine-leaks", "0"): (
                {
                    "binary_pass": False,
                    "score": 0.0,
                    "components": dict.fromkeys(_CLEAN_COMPONENTS),
                    "reasons": "Agent got stuck (repeated actions without progress)",
                    "synthesized_by_termination_reason": "stuck_detected",
                },
                {"cost_usd": 1.17, "turns": 250, "usage": dict(_USAGE)},
            )
        },
    )
    rules = [v.rule for v in check_run_bundle(bundle)]
    assert FidelityRule.UNMEASURED_SCORE not in rules
    assert FidelityRule.MARKER_COHERENCE not in rules


# ---------------------------------------------------------------------------
# R2 — denominator closure
# ---------------------------------------------------------------------------


def test_r2_flags_an_abort_reason_with_no_slot_to_be_counted_in(tmp_path: Path) -> None:
    """A bundle that cannot report one of the excluded reasons.

    The corpus shape: bundles written before ``reasoning_without_action``
    existed carry four keys where five are required, so a trial excluded for
    that reason has nowhere to land and silently leaves the denominator.
    """
    aborts = dict(_ABORT_KEYS)
    dropped = sorted(aborts)[0]
    del aborts[dropped]
    bundle = _write_bundle(
        tmp_path,
        aggregate=_aggregate(infrastructure_aborts=aborts),
        rows=[_row("fix-goroutine-leaks", infrastructure_aborts=aborts)],
    )
    violations = [v for v in check_run_bundle(bundle) if v.rule is FidelityRule.DENOMINATOR_CLOSURE]
    assert violations
    assert any(dropped in v.detail for v in violations)


def test_r2_flags_a_denominator_that_does_not_close(tmp_path: Path) -> None:
    """``total_trials`` must be the measured trials plus the aborts, exactly."""
    bundle = _write_bundle(
        tmp_path,
        aggregate=_aggregate(total_trials=50, measured_trials=47, scored_trials=47),
        rows=[],
    )
    violations = [v for v in check_run_bundle(bundle) if v.rule is FidelityRule.DENOMINATOR_CLOSURE]
    assert any("total_trials 50" in v.detail for v in violations)


def test_r2_flags_more_scored_than_measured(tmp_path: Path) -> None:
    bundle = _write_bundle(
        tmp_path,
        aggregate=_aggregate(total_trials=5, measured_trials=4, scored_trials=5),
        rows=[],
    )
    violations = [v for v in check_run_bundle(bundle) if v.rule is FidelityRule.DENOMINATOR_CLOSURE]
    assert any("exceeds measured_trials" in v.detail for v in violations)


def test_r2_skips_a_bundle_written_before_the_measured_denominator(tmp_path: Path) -> None:
    """An archived bundle has no denominator to close, and says so by omission.

    Scoping on ``measured_trials`` is what keeps ``check-run`` usable over old
    artifacts without the rule going off on every one of them. Every bundle the
    current orchestrator writes carries the field.
    """
    bundle = _write_bundle(
        tmp_path,
        aggregate={"schema_version": 1, "total_trials": 50, "passed": 31},
    )
    assert check_run_bundle(bundle) == []


def test_missing_aggregate_is_itself_a_violation(tmp_path: Path) -> None:
    assert [v.rule for v in check_run_bundle(tmp_path)] == [FidelityRule.DENOMINATOR_CLOSURE]


# ---------------------------------------------------------------------------
# R3 — disk reconciliation
# ---------------------------------------------------------------------------


def test_r3_flags_a_task_row_counting_trials_that_are_not_on_disk(tmp_path: Path) -> None:
    """The ghost-row shape: 18 trials reported where 16 exist.

    An abandoned retry attempt stays in the results list and leaves no
    directory, so the row's denominator grows without any evidence behind it.
    """
    bundle = _write_bundle(
        tmp_path,
        aggregate=_aggregate(total_trials=4, measured_trials=4, scored_trials=2),
        rows=[_row("git-recovery-challenge", total_trials=4, measured_trials=4, scored_trials=2)],
        trials={
            ("git-recovery-challenge", str(index)): (
                {
                    "binary_pass": True,
                    "score": 1.0,
                    "components": dict(_CLEAN_COMPONENTS),
                    "synthesized_by_termination_reason": None,
                },
                {"cost_usd": 0.2, "turns": 10, "usage": dict(_USAGE)},
            )
            for index in range(2)
        },
    )
    violations = [v for v in check_run_bundle(bundle) if v.rule is FidelityRule.DISK_RECONCILIATION]
    assert len(violations) == 1
    assert "total_trials 4 but 2 trial directories on disk" in violations[0].detail


def test_r3_accepts_a_row_that_matches_the_directories(tmp_path: Path) -> None:
    assert not [
        v
        for v in check_run_bundle(_clean_bundle(tmp_path))
        if v.rule is FidelityRule.DISK_RECONCILIATION
    ]


# ---------------------------------------------------------------------------
# R4 — marker / component coherence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("components", "synthesized"),
    [
        pytest.param(dict(_CLEAN_COMPONENTS), "api_error", id="marker-with-measured-component"),
        pytest.param(dict.fromkeys(_CLEAN_COMPONENTS), None, id="no-marker-nothing-measured"),
    ],
)
def test_r4_flags_the_two_halves_disagreeing(
    tmp_path: Path, components: dict[str, Any], synthesized: str | None
) -> None:
    """The marker and all-``None`` components are one statement, not two.

    Either half alone leaves a grade that downstream cannot classify: a marker
    beside a measured component claims both that no evaluator ran and that one
    scored, and components with no marker is the shape #1829 describes.
    """
    bundle = _write_bundle(
        tmp_path,
        aggregate=_aggregate(),
        rows=[_row("fix-goroutine-leaks")],
        trials={
            ("fix-goroutine-leaks", "0"): (
                {
                    "binary_pass": False,
                    "score": 0.0,
                    "components": components,
                    "synthesized_by_termination_reason": synthesized,
                },
                {"cost_usd": 0.51, "turns": 12, "usage": dict(_USAGE)},
            )
        },
    )
    rules = [v.rule for v in check_run_bundle(bundle)]
    assert FidelityRule.MARKER_COHERENCE in rules


# ---------------------------------------------------------------------------
# R5 — spend sanity
# ---------------------------------------------------------------------------


def test_r5_flags_a_scored_trial_that_never_reached_the_model(tmp_path: Path) -> None:
    """The fabricated-zero shape, caught without reading grading internals.

    The corpus case: the container stack never came up, the agent never ran,
    and the adapter wrote a zero anyway — ``cost_usd: null`` with no tokens in
    either direction.
    """
    bundle = _write_bundle(
        tmp_path,
        aggregate=_aggregate(),
        rows=[_row("fix-goroutine-leaks")],
        trials={
            ("fix-goroutine-leaks", "0"): (
                {
                    "binary_pass": False,
                    "score": 0.0,
                    "components": {**_CLEAN_COMPONENTS, "custom_checks": 0.0},
                    "reasons": "trial produced no verifier reward: Docker compose command failed",
                    "synthesized_by_termination_reason": None,
                },
                {"cost_usd": None, "turns": 0, "usage": dict(_NO_USAGE)},
            )
        },
    )
    violations = [v for v in check_run_bundle(bundle) if v.rule is FidelityRule.SPEND_SANITY]
    assert len(violations) == 1
    assert violations[0].where == "trials/fix-goroutine-leaks/0"


def test_r5_allows_a_declared_oracle(tmp_path: Path) -> None:
    """An oracle spends nothing by design and still earns a real verdict.

    The one honest ``cost_usd: null`` beside a score, and the reason R5 reads
    the trial's declared harness rather than assuming no spend means no run.
    """
    bundle = _write_bundle(
        tmp_path,
        aggregate=_aggregate(),
        rows=[_row("add-devdocs-search")],
        trials={
            ("add-devdocs-search", "0"): (
                {
                    "binary_pass": True,
                    "score": 1.0,
                    "components": dict(_CLEAN_COMPONENTS),
                    "synthesized_by_termination_reason": None,
                },
                {"cost_usd": None, "turns": 0, "usage": dict(_NO_USAGE)},
            )
        },
        task_yaml=_task_yaml("add-devdocs-search", harness="oracle"),
    )
    assert not [v for v in check_run_bundle(bundle) if v.rule is FidelityRule.SPEND_SANITY]


def test_r5_allows_an_unpriced_trial_that_really_ran(tmp_path: Path) -> None:
    """Pricing can fail on a trial that burned tokens; that score is earned.

    Without the token half of the rule this is the false positive R5 would
    produce on every provider the price list does not cover.
    """
    bundle = _write_bundle(
        tmp_path,
        aggregate=_aggregate(),
        rows=[_row("fix-crm-sync-throughput")],
        trials={
            ("fix-crm-sync-throughput", "0"): (
                {
                    "binary_pass": True,
                    "score": 0.933333,
                    "components": {**_CLEAN_COMPONENTS, "custom_checks": 0.933333},
                    "synthesized_by_termination_reason": None,
                },
                {"cost_usd": None, "turns": 51, "usage": dict(_USAGE)},
            )
        },
    )
    assert not [v for v in check_run_bundle(bundle) if v.rule is FidelityRule.SPEND_SANITY]


def test_a_trial_with_no_grade_is_not_scored_and_not_flagged(tmp_path: Path) -> None:
    """An ungradeable trial reports no number, which is the honest outcome."""
    bundle = _write_bundle(
        tmp_path,
        aggregate=_aggregate(scored_trials=0, ungradeable=1),
        rows=[_row("fix-dsp-audio-pipeline", scored_trials=0, ungradeable=1)],
        trials={
            ("fix-dsp-audio-pipeline", "0"): (
                None,
                {"cost_usd": 10.59883155, "turns": 223, "usage": dict(_USAGE)},
            )
        },
    )
    assert check_run_bundle(bundle) == []
