"""Which grading components exist, and where each one's score lives on each substrate.

Substrate-neutral by construction: the core engine, the runner and the wire
transcription between them read one enumeration instead of each carrying its own
copy of the component names. A component missing from :data:`GRADE_COMPONENTS` is
a component no substrate can score, no author can weight, and no wire message can
carry. Its only project dependency is :func:`custom_checks_enabled`, itself the
gate both substrates already share.

``tests/canonical/test_grade_component_registry_canon.py`` makes the enumeration
load-bearing: the names here are checked against the core ``GradeComponents``
model, the proto ``GradeComponents`` descriptor and the keys the gRPC client
lowers a wire grade into.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from tolokaforge.core.grading.checks_helpers import custom_checks_enabled


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
    opt_in_gate: Callable[[Any], bool] | None = None
    """The component's own opt-in gate, where its section carries an enable flag.

    ``None`` where the section's presence is the whole answer. A gate lives here
    rather than at the call sites because the flag's default is the component's own
    knowledge: ``CustomChecksConfig.enabled`` defaults to ``False``, so a caller
    reading the key generically would read an unflagged block as requested.
    """


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
        opt_in_gate=custom_checks_enabled,
    ),
)
"""Every component a grade may carry, in the order ``GradingConfig`` declares them."""

COMPONENT_BY_NAME: Mapping[str, GradeComponentSpec] = MappingProxyType(
    {spec.name: spec for spec in GRADE_COMPONENTS}
)
"""The enumeration keyed by wire name, so a caller needing one component looks it up here
rather than re-deriving the field names it lives under."""


def component_requested(spec: GradeComponentSpec, section: Any) -> bool:
    """Whether the author asked *spec*'s component to be scored.

    *section* is whatever the artifact in hand holds for :attr:`config_section` —
    the authored ``grading.yaml`` value, the ``GradingConfig`` attribute, or the
    runner's wire dict entry.

    An explicit opt-out (``custom_checks: {enabled: false}``) is *not* requested, and
    it reads that way from every artifact: unlike an empty mapping, it survives the
    wire intact rather than arriving as ``None``.

    Raises:
        ValidationError: propagated from a component's own opt-in gate, for a
            non-empty section that is not a valid block. A mistyped key would
            otherwise read as its default and silently unrequest a scored component.
    """
    if spec.opt_in_gate is None:
        return section is not None
    return spec.opt_in_gate(section)


def runner_score_field(name: str) -> str:
    """The runner field one component's score lives on.

    :attr:`GradeComponentSpec.runner_score_field` is ``None`` for a composed slot, so a
    caller that wants a *key* — to write a component into the runner's fold, or to read one
    back out of it — would otherwise carry a branch for a value it cannot use, or pass
    ``None`` on as though it were a field name.

    Raises:
        KeyError: with ``name`` as its only argument, for a composed component or for a
            name no component declares. Both are the same answer to the caller: there is no
            field, so a message naming the component is the whole of what it can say.
    """
    spec = COMPONENT_BY_NAME[name]
    if spec.runner_score_field is None:
        raise KeyError(name)
    return spec.runner_score_field
