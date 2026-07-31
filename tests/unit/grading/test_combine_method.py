"""What each declared ``combine.method`` returns, and what an undeclared one costs.

The fixture is chosen so the three aggregations disagree: components ``0.0`` and
``1.0`` give a min, a mean and a max of ``0.0 / 0.5 / 1.0``. On equal components
every method returns the same number and a table over them asserts nothing.

Expected values are written out per (method, threshold) cell rather than recomputed
from the implementation, and the table's key set is pinned to the whole cross product
of :data:`COMBINE_METHODS` and the swept thresholds — so a fourth declared method
cannot land without its own rows.
"""

from itertools import product

import pytest

from tolokaforge.core.grading.combine_method import (
    COMBINE_METHODS,
    RETIRED_COMBINE_METHOD_ALIASES,
    combine_by_method,
    validate_combine_method,
)

pytestmark = pytest.mark.unit

_COMPONENTS = {"state_checks": 0.0, "transcript_rules": 1.0}
_WEIGHTED_MEAN = 0.5
_PASS_THRESHOLDS = (0.0, 0.8, 1.0)

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


def _combine(method: str, pass_threshold: float) -> tuple[float, bool]:
    return combine_by_method(
        method=method,
        component_scores=_COMPONENTS,
        weighted_mean=_WEIGHTED_MEAN,
        pass_threshold=pass_threshold,
    )


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
    def test_an_alias_is_not_quietly_supported(self, alias, replacement):
        assert alias not in COMBINE_METHODS
        assert replacement in COMBINE_METHODS

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
