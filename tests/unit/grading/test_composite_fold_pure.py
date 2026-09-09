"""Table-driven behaviour tests for ``CompositeFold.finalise``.

The fold is the shared substrate-neutral reducer that both dispatchers
drive; the byte-parity 10-pack gate at
``tests/canonical/test_grader_parity_reference.py`` locks the *joined*
grade end-to-end. These cases lock the per-branch behaviours the fold is
responsible for, so drift in one arm surfaces here in seconds instead of
via a byte diff on ``expected_grade.json``.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.grading.composite_fold import (
    CompositeFold,
    CompositeFoldResult,
)
from tolokaforge.core.grading.state_composition import CONFLICTING_STATE_SOURCES_MESSAGE
from tolokaforge.runner.grading import project_state_checks_to_runner_wire

pytestmark = pytest.mark.unit


def _no_op_components() -> dict[str, float]:
    return {
        "hash_score": -1.0,
        "jsonpath_score": -1.0,
        "db_probe_score": -1.0,
        "transcript_score": -1.0,
        "trace_checks_score": -1.0,
        "llm_judge_score": -1.0,
        "custom_checks_score": -1.0,
    }


def _no_op_config() -> dict:
    return {"combine_method": "weighted", "weights": {}, "pass_threshold": 1.0}


def test_no_op_config_passes_and_names_that_nothing_was_asked() -> None:
    """Nothing configured and nothing weighted decides ``(1.0, True)`` and says so.

    A grade for a trial that scored none of the components is not silent: no
    component's reasons speak for it, so the fold's own sentence is what stops a
    bare ``0.0`` — or here a bare ``1.0`` — arriving without any explanation.
    """
    result = CompositeFold.finalise(
        components_dict=_no_op_components(),
        grading_config_dict=_no_op_config(),
        hash_weight=None,
        judge_gate_failed=False,
        trace_gate_failed=False,
    )
    assert isinstance(result, CompositeFoldResult)
    assert result.binary_pass is True
    assert result.score == 1.0
    assert result.verdict_reason is not None
    assert result.verdict_reason in result.reasons
    assert result.state_checks_component is None
    assert result.inert_weight_reason is None


def test_judge_gate_failure_zeroes_component_and_fails_trial() -> None:
    """A closed required-criterion gate zeros the judge before the fold reads it.

    The gated ``0.0`` — not the raw ``0.9`` aggregate — reaches ``build_grade_reasons``
    and the wire, and the trial fails whatever the weighted average returns.
    """
    components = _no_op_components() | {"llm_judge_score": 0.9}
    config = {
        "combine_method": "weighted",
        "weights": {"llm_judge": 1.0},
        "pass_threshold": 0.5,
        "llm_judge": {},
    }
    result = CompositeFold.finalise(
        components_dict=components,
        grading_config_dict=config,
        hash_weight=None,
        judge_gate_failed=True,
        trace_gate_failed=False,
    )
    assert result.binary_pass is False
    assert result.judge_component == 0.0
    assert result.verdict_reason is None
    assert "Judge: score=0.00" in result.reasons


def test_trace_gate_failure_leaves_score_untouched_and_fails_trial() -> None:
    """A closed trace gate fails ``binary_pass`` on its own; the score itself is untouched."""
    components = _no_op_components() | {"trace_checks_score": 1.0}
    config = {
        "combine_method": "weighted",
        "weights": {"trace_checks": 1.0},
        "pass_threshold": 0.5,
        "trace_checks": {},
    }
    result = CompositeFold.finalise(
        components_dict=components,
        grading_config_dict=config,
        hash_weight=None,
        judge_gate_failed=False,
        trace_gate_failed=True,
    )
    assert result.score == 1.0
    assert result.binary_pass is False


def test_two_state_sources_without_a_weight_refuse_the_fold() -> None:
    """A hash verdict and a JSONPath score without a ``hash_weight`` are undecidable."""
    components = _no_op_components() | {"hash_score": 1.0, "jsonpath_score": 0.5}
    config = {
        "combine_method": "weighted",
        "weights": {"state_checks": 1.0},
        "pass_threshold": 1.0,
        "state_checks": {},
    }
    with pytest.raises(ValueError):
        CompositeFold.finalise(
            components_dict=components,
            grading_config_dict=config,
            hash_weight=None,
            judge_gate_failed=False,
            trace_gate_failed=False,
        )


def test_probes_beside_another_source_refuse_the_fold() -> None:
    """A db-probe score beside a hash verdict is refused before the weight is consulted."""
    components = _no_op_components() | {"hash_score": 1.0, "db_probe_score": 0.5}
    config = {
        "combine_method": "weighted",
        "weights": {"state_checks": 1.0},
        "pass_threshold": 1.0,
        "state_checks": {},
    }
    with pytest.raises(ValueError, match=CONFLICTING_STATE_SOURCES_MESSAGE):
        CompositeFold.finalise(
            components_dict=components,
            grading_config_dict=config,
            hash_weight=None,
            judge_gate_failed=False,
            trace_gate_failed=False,
        )


def test_ledger_skip_notes_land_between_component_and_verdict_segments() -> None:
    """Populated ``ledger_skip_notes`` are joined and appended between the component
    segment and any verdict reason — mirroring the runner's segment order.
    """
    components = _no_op_components() | {"transcript_score": 1.0}
    config = {
        "combine_method": "weighted",
        "weights": {"transcript_rules": 1.0},
        "pass_threshold": 0.5,
        "transcript_rules": {},
    }
    result = CompositeFold.finalise(
        components_dict=components,
        grading_config_dict=config,
        hash_weight=None,
        judge_gate_failed=False,
        trace_gate_failed=False,
        transcript_result_dict={"details": [{"passed": True, "message": "ok"}]},
        ledger_skip_notes=["required_actions populated but no evaluator consumed it"],
    )
    assert "Transcript: all 1 rules passed" in result.reasons
    assert "required_actions populated but no evaluator consumed it" in result.reasons
    assert result.reasons.index("Transcript:") < result.reasons.index(
        "required_actions populated but no evaluator consumed it"
    )


def test_judge_errored_tail_names_the_judge_reasons_after_the_component_segment() -> None:
    """``judge_errored`` appends ``"JUDGE ERRORED: <reasons>"`` after the component segment."""
    components = _no_op_components() | {"llm_judge_score": -1.0, "transcript_score": 1.0}
    config = {
        "combine_method": "weighted",
        "weights": {"transcript_rules": 1.0},
        "pass_threshold": 0.5,
        "transcript_rules": {},
    }
    result = CompositeFold.finalise(
        components_dict=components,
        grading_config_dict=config,
        hash_weight=None,
        judge_gate_failed=False,
        trace_gate_failed=False,
        transcript_result_dict={"details": [{"passed": True}]},
        judge_reasons="upstream 503",
        judge_errored=True,
    )
    assert "JUDGE ERRORED: upstream 503" in result.reasons


def test_inert_hash_weight_appended_after_ledger_notes() -> None:
    """A declared ``hash_weight`` the fold never consulted is reported once, after
    any ledger skip notes and before the verdict reason.
    """
    components = _no_op_components() | {"hash_score": 1.0}
    config = {
        "combine_method": "weighted",
        "weights": {"state_checks": 1.0},
        "pass_threshold": 1.0,
        "state_checks": {"hash_weight": 0.6},
    }
    result = CompositeFold.finalise(
        components_dict=components,
        grading_config_dict=config,
        hash_weight=0.6,
        judge_gate_failed=False,
        trace_gate_failed=False,
    )
    assert result.inert_weight_reason is not None
    assert result.inert_weight_reason in result.reasons


def test_components_dict_mutation_carries_gated_judge_into_reasons() -> None:
    """Step 3 of ``finalise`` reassigns ``llm_judge_score`` to the gated component
    so ``build_grade_reasons`` reads the value that reaches the wire.
    """
    components = _no_op_components() | {"llm_judge_score": 0.9}
    config = {
        "combine_method": "weighted",
        "weights": {"llm_judge": 1.0},
        "pass_threshold": 0.5,
        "llm_judge": {},
    }
    result = CompositeFold.finalise(
        components_dict=components,
        grading_config_dict=config,
        hash_weight=None,
        judge_gate_failed=True,
        trace_gate_failed=False,
    )
    assert "Judge: score=0.00" in result.reasons
    assert "Judge: score=0.90" not in result.reasons


def test_project_state_checks_to_runner_wire_encodes_none_as_minus_one() -> None:
    assert project_state_checks_to_runner_wire(None) == -1.0


def test_project_state_checks_to_runner_wire_passes_through_scored_slots() -> None:
    assert project_state_checks_to_runner_wire(0.7) == 0.7
    assert project_state_checks_to_runner_wire(0.0) == 0.0
