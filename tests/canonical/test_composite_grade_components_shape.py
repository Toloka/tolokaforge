"""Shape lock for :class:`CompositeGradeComponents`.

The wire-side score container both composite dispatchers populate is
substrate-neutral by construction: the runner-side ``_grade_trial_async``,
the grader-side :class:`GraderCompositeDispatch`, and the offline
:class:`CompositeGraderKind` all fold sub-component scores through this
one Pydantic class before the composite fold combines them. A drift in
its identity, field set, or strictness would silently change what every
dispatcher is allowed to write into a Grade — hence the lock.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tolokaforge.core.grading.grade_components import CompositeGradeComponents
from tolokaforge.runner import models as runner_models

pytestmark = pytest.mark.canonical


_EXPECTED_FIELDS = frozenset(
    {
        "hash_match",
        "hash_score",
        "jsonpath_score",
        "jsonpath_reasons",
        "db_probe_score",
        "db_probe_reasons",
        "transcript_pass",
        "transcript_score",
        "trace_checks_score",
        "llm_judge_score",
        "llm_judge_reasons",
        "custom_checks_score",
    }
)


def test_lives_in_core_grading_grade_components() -> None:
    """The class's canonical home is co-located with ``GRADE_COMPONENTS``.

    A re-import from a moved-away shim would change ``__module__`` and
    silently reintroduce a cross-boundary import into ``runner.models``.
    """
    assert CompositeGradeComponents.__module__ == "tolokaforge.core.grading.grade_components"


def test_field_set_is_the_twelve_composite_slots() -> None:
    assert set(CompositeGradeComponents.model_fields) == _EXPECTED_FIELDS


def test_extra_forbid_is_the_strict_contract() -> None:
    assert CompositeGradeComponents.model_config["extra"] == "forbid"


def test_unknown_key_fails_loud() -> None:
    with pytest.raises(ValidationError):
        CompositeGradeComponents.model_validate({"definitely_unknown_key": 1})


def test_runner_models_does_not_export_runner_grade_components() -> None:
    """``tolokaforge.runner.models`` does not expose ``RunnerGradeComponents``.

    A re-export (module attribute, ``from ... import ... as`` alias, or
    ``__getattr__`` shim) would create a cross-boundary import from the
    composite grading dispatchers into ``runner.models`` — the exact
    coupling ``CompositeGradeComponents`` lives in ``core.grading`` to
    prevent.
    """
    assert not hasattr(runner_models, "RunnerGradeComponents")
