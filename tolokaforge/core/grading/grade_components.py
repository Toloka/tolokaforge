"""Which grading components exist, and where each one's score lives on each substrate.

Substrate-neutral by construction: stdlib-only declarations, so the core engine,
the runner and the wire transcription between them read one enumeration instead
of each carrying its own copy of the component names. A component missing from
:data:`GRADE_COMPONENTS` is a component no substrate can score, no author can
weight, and no wire message can carry.

``tests/canonical/test_grade_component_registry_canon.py`` makes the enumeration
load-bearing: the names here are checked against the core ``GradeComponents``
model, the proto ``GradeComponents`` descriptor and the keys the gRPC client
lowers a wire grade into.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GradeComponentSpec:
    """One grading component's identity across the config, the wire and both substrates.

    ``name`` is the single wire-facing identity: the ``combine.weights`` key an
    author writes, the proto ``GradeComponents`` field, and the key the gRPC
    client emits. ``config_section`` is the ``grading.yaml`` section whose
    presence means the author configured the component — distinct from being
    weighted, and both are required before an unevaluated component fails a
    trial. ``core_field`` and ``runner_score_field`` are the score-carrying
    attributes on each substrate's own ``GradeComponents`` model.

    ``runner_score_field`` is ``None`` for a composed slot: no single runner
    field holds the score, because the runner folds several sources into it
    before the component exists.
    """

    name: str
    config_section: str
    core_field: str
    runner_score_field: str | None


GRADE_COMPONENTS: tuple[GradeComponentSpec, ...] = (
    GradeComponentSpec(
        name="state_checks",
        config_section="state_checks",
        core_field="state_checks",
        runner_score_field=None,
    ),
    GradeComponentSpec(
        name="transcript_rules",
        config_section="transcript_rules",
        core_field="transcript_rules",
        runner_score_field="transcript_score",
    ),
    GradeComponentSpec(
        name="trace_checks",
        config_section="trace_checks",
        core_field="trace_checks",
        runner_score_field="trace_checks_score",
    ),
    GradeComponentSpec(
        name="llm_judge",
        config_section="llm_judge",
        core_field="llm_judge",
        runner_score_field="llm_judge_score",
    ),
    GradeComponentSpec(
        name="custom_checks",
        config_section="custom_checks",
        core_field="custom_checks",
        runner_score_field="custom_checks_score",
    ),
)
"""Every component a grade may carry, in the order ``GradingConfig`` declares them."""
