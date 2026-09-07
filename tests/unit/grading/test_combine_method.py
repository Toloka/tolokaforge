"""What each declared ``combine.method`` returns, and what an undeclared one costs.

The fixture is chosen so the three aggregations disagree: components ``0.0`` and
``1.0`` give a min, a mean and a max of ``0.0 / 0.5 / 1.0``. On equal components
every method returns the same number and a table over them asserts nothing.

Expected values are written out per (method, threshold) cell rather than recomputed
from the implementation, and the table's key set is pinned to the whole cross product
of :data:`COMBINE_METHODS` and the swept thresholds — so a fourth declared method
cannot land without its own rows.

The runner's combine folds the same two components under the same weights, so it owes
the table's answers at the swept threshold it is driven at; core's own aggregation is
proven against both substrates at the canonical tier
(``tests/canonical/test_grading_substrate_parity.py``), and what is unit-locked here is
the verdict it reaches with nothing scored, where there is no map to aggregate.

The two models an author's method is loaded through close the same set, and their
rejection is asserted on the alias's named replacement and the "never worked" sentence
— the two things a bare ``Literal`` cannot say, since its own ``literal_error`` already
quotes the offending value and the whole permitted set.
"""

from itertools import product

import pytest

from tolokaforge.core.grading.combine import GradingEngine
from tolokaforge.core.grading.combine_method import (
    COMBINE_METHODS,
    RETIRED_COMBINE_METHOD_ALIASES,
    combine_by_method,
    validate_combine_method,
)
from tolokaforge.core.grading.composite_fold import combine_grade_components
from tolokaforge.core.models import GradingCombineConfig, GradingConfig, Trajectory
from tolokaforge.runner.models import RunnerGradingConfig

pytestmark = pytest.mark.unit

_COMPONENTS = {"state_checks": 0.0, "transcript_rules": 1.0}
_WEIGHTED_MEAN = 0.5
_PASS_THRESHOLDS = (0.0, 0.8, 1.0)
_RUNNER_THRESHOLD = 0.8

_EXPECTED: dict[tuple[str, float], tuple[float, bool]] = {
    ("weighted", 0.0): (0.5, True),
    ("weighted", 0.8): (0.5, False),
    ("weighted", 1.0): (0.5, False),
    ("all", 0.0): (0.0, True),
    ("all", 0.8): (0.0, False),
    ("all", 1.0): (0.0, False),
    ("any", 0.0): (1.0, True),
    ("any", 0.8): (1.0, True),
    ("any", 1.0): (1.0, True),
}

_UNSUPPORTED_VALUES = ("min", "bogus_method_xyz", "ALL", "", "weighted ", None, 1.0, True, ["all"])

# The runner scores ``state_checks`` from its JSONPath assertions and
# ``transcript_rules`` from its rule evaluation, at the weights that make its own mean
# the ``_WEIGHTED_MEAN`` above.
_RUNNER_COMPONENT_SCORES = {"jsonpath_score": 0.0, "transcript_score": 1.0}
_RUNNER_CONFIG = {
    "weights": {"state_checks": 1.0, "transcript_rules": 1.0},
    "pass_threshold": _RUNNER_THRESHOLD,
}

# The two models an author's method is loaded through — the ``grading.yaml`` block
# core reads and the field the runner is handed over the wire — as
# (model, field name).
_MODEL_GATES = ((GradingCombineConfig, "method"), (RunnerGradingConfig, "combine_method"))


def _combine(method: str, pass_threshold: float) -> tuple[float, bool]:
    return combine_by_method(
        method=method,
        component_scores=_COMPONENTS,
        weighted_mean=_WEIGHTED_MEAN,
        pass_threshold=pass_threshold,
    )


def _runner_combine(method: object) -> tuple[float, bool]:
    folded = combine_grade_components(
        _RUNNER_COMPONENT_SCORES, {**_RUNNER_CONFIG, "combine_method": method}
    )
    return folded.score, folded.binary_pass


def _unscored_trial_grade(*, method: str, pass_threshold: float) -> tuple[float, bool, str]:
    """Core's verdict on a trial where no configured component was scored."""
    grading_config = GradingConfig.model_validate(
        {
            "combine": {
                "method": method,
                "weights": {"state_checks": 1.0},
                "pass_threshold": pass_threshold,
            }
        }
    )
    trajectory = Trajectory(
        task_id="unscored",
        trial_index=0,
        start_ts="2026-01-01T00:00:00+00:00",
        end_ts="2026-01-01T00:00:00+00:00",
        messages=[],
        tool_log=[],
    )
    grade = GradingEngine(grading_config).grade_trajectory(trajectory, {})
    return grade.score, grade.binary_pass, grade.reasons


class TestDispatchTable:
    """Every declared method, at every swept threshold, against a written-out answer."""

    def test_the_table_covers_every_declared_method_at_every_threshold(self):
        assert {method for method, _ in _EXPECTED} == set(COMBINE_METHODS), (
            f"_EXPECTED covers {sorted({method for method, _ in _EXPECTED})} but "
            f"COMBINE_METHODS declares {sorted(COMBINE_METHODS)}. A declared method with "
            "no row is a method with no evidence: give it one row per swept threshold."
        )
        assert set(_EXPECTED) == set(product(COMBINE_METHODS, _PASS_THRESHOLDS)), (
            "_EXPECTED must hold the whole (method, threshold) cross product. A method "
            "pinned at one threshold cannot show whether its pass flag reads the "
            "threshold at all."
        )

    @pytest.mark.parametrize(("method", "pass_threshold"), tuple(_EXPECTED))
    def test_each_cell_returns_its_written_out_score_and_flag(self, method, pass_threshold):
        assert _combine(method, pass_threshold) == _EXPECTED[(method, pass_threshold)]

    def test_the_declared_methods_disagree_on_the_fixture(self):
        scored = {method: _combine(method, 0.8)[0] for method in COMBINE_METHODS}
        assert len(set(scored.values())) == len(COMBINE_METHODS), (
            f"the declared methods scored {scored} on components {_COMPONENTS}. Two "
            "aggregations returning one number means the dispatch is not being measured."
        )


class TestRetiredAliases:
    """A name that was declared but never dispatched is rejected, not translated."""

    @pytest.mark.parametrize(
        ("alias", "replacement"), tuple(RETIRED_COMBINE_METHOD_ALIASES.items())
    )
    def test_validation_names_the_replacement_and_that_the_alias_never_worked(
        self, alias, replacement
    ):
        with pytest.raises(ValueError) as excinfo:
            validate_combine_method(alias, context="grading.yaml combine.method")
        message = str(excinfo.value)
        assert f"Use {replacement!r}" in message, message
        assert "never worked" in message, message

    @pytest.mark.parametrize("alias", tuple(RETIRED_COMBINE_METHOD_ALIASES))
    def test_combining_by_an_alias_raises_rather_than_folding(self, alias):
        with pytest.raises(ValueError, match="never worked"):
            _combine(alias, 0.8)


class TestUnsupportedValues:
    """Anything else is rejected, listing what an author may write instead."""

    @pytest.mark.parametrize("value", _UNSUPPORTED_VALUES)
    def test_validation_names_the_value_the_context_and_every_supported_method(self, value):
        with pytest.raises(ValueError) as excinfo:
            validate_combine_method(value, context="core GradingCombineConfig.method")
        message = str(excinfo.value)
        assert repr(value) in message, message
        assert "core GradingCombineConfig.method" in message, message
        for method in COMBINE_METHODS:
            assert repr(method) in message, message

    @pytest.mark.parametrize("value", _UNSUPPORTED_VALUES)
    def test_only_a_retired_alias_earns_the_never_worked_sentence(self, value):
        with pytest.raises(ValueError) as excinfo:
            validate_combine_method(value, context="grading.yaml combine.method")
        assert "never worked" not in str(excinfo.value), (
            "a sentence appended to every rejection carries no information about "
            "aliases, which is the one thing a bare Literal cannot say"
        )

    @pytest.mark.parametrize("value", _UNSUPPORTED_VALUES)
    def test_combining_by_an_unsupported_value_raises(self, value):
        with pytest.raises(ValueError, match="not a supported combine method"):
            _combine(value, 0.8)

    @pytest.mark.parametrize("method", COMBINE_METHODS)
    def test_a_supported_value_is_returned_unchanged(self, method):
        assert validate_combine_method(method, context="grading.yaml combine.method") == method


class TestEmptyComponentScores:
    """Nothing scored is not something to aggregate, on any method."""

    @pytest.mark.parametrize("method", COMBINE_METHODS)
    def test_an_empty_component_map_raises_for_every_method(self, method):
        with pytest.raises(ValueError, match="none were scored"):
            combine_by_method(
                method=method,
                component_scores={},
                weighted_mean=_WEIGHTED_MEAN,
                pass_threshold=0.8,
            )

    @pytest.mark.parametrize("alias", tuple(RETIRED_COMBINE_METHOD_ALIASES))
    def test_the_method_is_answered_before_the_empty_map(self, alias):
        """Of the two things wrong with the call, the caller hears the one it can act on.

        "Nothing was scored" is a fact about the trial; the alias is the line to change.
        """
        with pytest.raises(ValueError, match="never worked"):
            combine_by_method(
                method=alias,
                component_scores={},
                weighted_mean=_WEIGHTED_MEAN,
                pass_threshold=0.8,
            )

    @pytest.mark.parametrize("pass_threshold", _PASS_THRESHOLDS)
    def test_core_fails_an_unscored_trial_at_every_threshold_and_names_the_weight(
        self, pass_threshold
    ):
        """``all`` over an empty map has no answer, so the engine must not ask for one —
        and its own answer is not a threshold comparison either.

        The config weights ``state_checks`` and configures no component, so nothing was
        scored and there is no number for a threshold to admit. Swept over every declared
        threshold, ``0.0`` included: a comparison against ``0.0`` passes there, so an
        implementation answering from the threshold reds on that row alone.
        """
        score, binary_pass, reasons = _unscored_trial_grade(
            method="all", pass_threshold=pass_threshold
        )

        assert (score, binary_pass) == (0.0, False)
        assert "state_checks" in reasons, reasons


class TestTheRunnerCombineDispatchesOnTheDeclaredMethod:
    """``combine_grade_components`` aggregates by the method or refuses to grade."""

    @pytest.mark.parametrize("method", COMBINE_METHODS)
    def test_each_declared_method_folds_the_runner_components_to_the_shared_answer(self, method):
        assert _runner_combine(method) == _EXPECTED[(method, _RUNNER_THRESHOLD)]

    @pytest.mark.parametrize(
        ("alias", "replacement"), tuple(RETIRED_COMBINE_METHOD_ALIASES.items())
    )
    def test_an_alias_fails_the_grade_naming_its_replacement(self, alias, replacement):
        with pytest.raises(ValueError) as excinfo:
            _runner_combine(alias)
        message = str(excinfo.value)
        assert f"Use {replacement!r}" in message, message
        assert "never worked" in message, message

    @pytest.mark.parametrize("value", _UNSUPPORTED_VALUES)
    def test_an_unsupported_value_fails_the_grade_listing_every_supported_method(self, value):
        with pytest.raises(ValueError) as excinfo:
            _runner_combine(value)
        message = str(excinfo.value)
        for method in COMBINE_METHODS:
            assert repr(method) in message, message

    def test_a_config_that_declares_no_method_fails_the_grade(self):
        with pytest.raises(ValueError, match="not a supported combine method"):
            combine_grade_components(_RUNNER_COMPONENT_SCORES, _RUNNER_CONFIG)


class TestBothModelsCloseTheDeclaredSet:
    """The two load gates admit the same three methods and answer alike outside them."""

    def test_every_aggregation_a_retired_alias_names_is_one_an_author_may_write(self):
        """The rejection message promises a replacement, so the promise must load.

        Keyed off the alias map rather than :data:`COMBINE_METHODS`: a lock reading the
        declared set would narrow with it, and a set narrowed out from under the
        migration is what left ``any`` implemented, documented and unwritable.
        """
        assert set(RETIRED_COMBINE_METHOD_ALIASES.values()) == {"all", "any"}

    @pytest.mark.parametrize(("model", "field"), _MODEL_GATES)
    @pytest.mark.parametrize("replacement", sorted(set(RETIRED_COMBINE_METHOD_ALIASES.values())))
    def test_a_replacement_loads_on_both_models(self, model, field, replacement):
        assert getattr(model(**{field: replacement}), field) == replacement

    @pytest.mark.parametrize(("model", "field"), _MODEL_GATES)
    @pytest.mark.parametrize("method", COMBINE_METHODS)
    def test_a_declared_method_survives_the_dump_as_a_plain_string(self, model, field, method):
        """Four committed trial bundles record this key in YAML, and an enum member is
        not a ``str``: ``yaml.safe_dump`` raises ``RepresenterError`` on one."""
        dumped = model(**{field: method}).model_dump()[field]

        assert type(dumped) is str, f"{model.__name__}.{field} dumped {dumped!r}"

    @pytest.mark.parametrize(("model", "field"), _MODEL_GATES)
    @pytest.mark.parametrize(
        ("alias", "replacement"), tuple(RETIRED_COMBINE_METHOD_ALIASES.items())
    )
    def test_an_alias_is_rejected_naming_its_replacement_on_both_models(
        self, model, field, alias, replacement
    ):
        """Asserted on the two things Pydantic cannot say for itself.

        A bare ``Literal`` rejects the alias too, and its ``literal_error`` already
        quotes the offending value and the whole permitted set — so an assertion on
        those passes with the validator deleted.
        """
        with pytest.raises(ValueError) as excinfo:
            model(**{field: alias})
        message = str(excinfo.value)
        assert f"Use {replacement!r}" in message, message
        assert "never worked" in message, message

    @pytest.mark.parametrize(("model", "field"), _MODEL_GATES)
    @pytest.mark.parametrize("value", _UNSUPPORTED_VALUES)
    def test_an_unsupported_value_is_rejected_listing_every_supported_method(
        self, model, field, value
    ):
        with pytest.raises(ValueError) as excinfo:
            model(**{field: value})
        message = str(excinfo.value)
        for method in COMBINE_METHODS:
            assert repr(method) in message, message
