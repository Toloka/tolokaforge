"""``GRADE_COMPONENTS`` is the only place a grading component is enumerated.

Five independently maintained sources have to agree on what a component is: the
registry, the core ``GradeComponents`` model, the proto ``GradeComponents``
descriptor, the score-carrying fields on the runner's own model, and the
``grading.yaml`` sections ``GradingConfig`` declares. Three are compared by
name; the other two are resolution checks, because the runner names its scores
``*_score`` and one component has no single runner field at all.

A sixth component added to any one source without the others is what these tests
exist to catch — that is the shape the plan for #678 found seven times over,
including a wire key whose absence silently wrote ``-1.0`` into a real score.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.utils.wire_grades import lower_wire_grade
from tolokaforge.core.grading.grade_components import GRADE_COMPONENTS, component_requested
from tolokaforge.core.models import GradeComponents as CoreGradeComponents
from tolokaforge.core.models import GradingConfig, StateChecksConfig
from tolokaforge.runner import runner_pb2
from tolokaforge.runner.models import RunnerGradeComponents, RunnerGradingConfig

pytestmark = [pytest.mark.canonical, pytest.mark.grading]

_REGISTERED_NAMES = {spec.name for spec in GRADE_COMPONENTS}


def test_registry_core_fields_are_exactly_the_core_model_slots() -> None:
    assert {spec.core_field for spec in GRADE_COMPONENTS} == set(CoreGradeComponents.model_fields)


def test_registry_names_are_exactly_the_wire_message_fields() -> None:
    wire_fields = {field.name for field in runner_pb2.GradeComponents.DESCRIPTOR.fields}
    assert wire_fields == _REGISTERED_NAMES


def test_every_registered_component_arrives_under_its_own_key() -> None:
    """Distinct scores per component, so a swapped mapping fails as loudly as a dropped key."""
    per_component = {
        spec.name: round(0.1 * (index + 1), 2) for index, spec in enumerate(GRADE_COMPONENTS)
    }
    lowered = lower_wire_grade(
        runner_pb2.Grade(
            binary_pass=True, score=1.0, components=runner_pb2.GradeComponents(**per_component)
        )
    )
    assert lowered["components"] == pytest.approx(per_component)


def test_every_runner_score_field_resolves_on_the_runner_model() -> None:
    declared = set(RunnerGradeComponents.model_fields)
    unresolved = [
        spec.runner_score_field
        for spec in GRADE_COMPONENTS
        if spec.runner_score_field is not None and spec.runner_score_field not in declared
    ]
    assert unresolved == []


def test_state_checks_is_the_only_composed_slot() -> None:
    composed = [spec.name for spec in GRADE_COMPONENTS if spec.runner_score_field is None]
    assert composed == ["state_checks"]


def test_every_config_section_resolves_on_the_grading_config() -> None:
    """A section that resolves nowhere can never be "configured", so its component
    would fall through to the pass-by-default branch instead of failing unevaluated."""
    declared = set(GradingConfig.model_fields)
    unresolved = [
        spec.config_section for spec in GRADE_COMPONENTS if spec.config_section not in declared
    ]
    assert unresolved == []


def test_every_gated_component_carries_its_section_raw_on_both_config_models() -> None:
    """A component's opt-in gate reads the keys the *author* wrote.

    So its section cannot arrive already constructed: a sub-model has applied the
    flag's default before the gate sees it, and the gate would then answer for a block
    nobody wrote — reading an unflagged ``custom_checks`` as an explicit opt-out, which
    is the same silent unscoring a mistyped key would cause. Both models are asserted
    because both are producers, and retyping either section to a sub-model would
    otherwise surface only at grade time.

    The raise is the runtime backstop for a caller outside these two models, and it
    names the component rather than failing inside the gate's own validation.
    """
    gated = [spec for spec in GRADE_COMPONENTS if spec.opt_in_gate is not None]
    assert [spec.name for spec in gated] == ["custom_checks"]

    for spec in gated:
        for model in (GradingConfig, RunnerGradingConfig):
            annotation = model.model_fields[spec.config_section].annotation
            assert annotation == dict[str, Any] | None, (spec.name, model.__name__, annotation)

    with pytest.raises(TypeError, match="arrived as a constructed"):
        component_requested(gated[0], StateChecksConfig())
