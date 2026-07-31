"""Composition rule for the ``state_checks`` component, pinned per presence case.

The single-source pass-through is pinned across the whole weight domain rather
than at one value: it is what makes a tau-style pack (hash on, no jsonpaths)
score its hash verdict alone, and a weight that leaked into that branch would
move those verdicts silently.
"""

import pytest

from tolokaforge.core.grading.state_composition import (
    MISSING_HASH_WEIGHT_MESSAGE,
    compose_state_checks_score,
    validate_hash_weight,
)

pytestmark = pytest.mark.unit

WEIGHT_DOMAIN = (0.0, 0.5, 0.6, 1.0, None)


class TestPresenceCases:
    """Which sources were evaluated decides the shape of the answer."""

    @pytest.mark.parametrize("hash_weight", WEIGHT_DOMAIN)
    def test_neither_source_evaluated_composes_to_not_evaluated(self, hash_weight):
        assert (
            compose_state_checks_score(
                hash_score=None, jsonpath_score=None, hash_weight=hash_weight
            )
            is None
        )

    @pytest.mark.parametrize("hash_weight", WEIGHT_DOMAIN)
    @pytest.mark.parametrize("hash_score", (0.0, 0.5, 1.0))
    def test_hash_alone_passes_through_at_every_weight(self, hash_score, hash_weight):
        assert (
            compose_state_checks_score(
                hash_score=hash_score, jsonpath_score=None, hash_weight=hash_weight
            )
            == hash_score
        )

    @pytest.mark.parametrize("hash_weight", WEIGHT_DOMAIN)
    @pytest.mark.parametrize("jsonpath_score", (0.0, 0.5, 1.0))
    def test_jsonpaths_alone_pass_through_at_every_weight(self, jsonpath_score, hash_weight):
        assert (
            compose_state_checks_score(
                hash_score=None, jsonpath_score=jsonpath_score, hash_weight=hash_weight
            )
            == jsonpath_score
        )


class TestBlendArithmetic:
    """Both sources evaluated: the author's weight decides the shares."""

    @pytest.mark.parametrize(
        ("hash_score", "jsonpath_score", "hash_weight", "expected"),
        (
            (1.0, 0.5, 0.0, 0.5),
            (1.0, 0.5, 0.5, 0.75),
            (1.0, 0.5, 0.6, 0.8),
            (1.0, 0.5, 1.0, 1.0),
            (0.0, 0.5, 0.0, 0.5),
            (0.0, 0.5, 0.5, 0.25),
            (0.0, 0.5, 0.6, 0.2),
            (0.0, 0.5, 1.0, 0.0),
            (1.0, 0.2, 0.25, 0.4),
            (0.0, 1.0, 0.25, 0.75),
        ),
    )
    def test_blend(self, hash_score, jsonpath_score, hash_weight, expected):
        assert compose_state_checks_score(
            hash_score=hash_score, jsonpath_score=jsonpath_score, hash_weight=hash_weight
        ) == pytest.approx(expected)

    def test_missing_weight_with_both_sources_raises_the_shared_message(self):
        with pytest.raises(ValueError) as excinfo:
            compose_state_checks_score(hash_score=1.0, jsonpath_score=0.5, hash_weight=None)
        assert str(excinfo.value) == MISSING_HASH_WEIGHT_MESSAGE

    @pytest.mark.parametrize("choice", ("1.0", "0.0", "0.5"))
    def test_shared_message_names_every_meaningful_choice(self, choice):
        assert f"weight: {choice}" in MISSING_HASH_WEIGHT_MESSAGE


class TestValidateHashWeight:
    """The weight's domain, enforced rather than assumed."""

    @pytest.mark.parametrize("value", (0.0, 0.25, 0.5, 1.0))
    def test_accepts_the_closed_unit_interval(self, value):
        assert validate_hash_weight(value, context="grading.yaml") == value

    @pytest.mark.parametrize("value", (0, 1))
    def test_accepts_an_integer_weight_as_a_float(self, value):
        accepted = validate_hash_weight(value, context="grading.yaml")
        assert isinstance(accepted, float)
        assert accepted == float(value)

    @pytest.mark.parametrize(
        "value", (2.0, -0.1, 1.5, float("nan"), "0.5", None, True, False, [0.5])
    )
    def test_rejects_anything_outside_the_domain(self, value):
        with pytest.raises(ValueError, match="core StateChecksConfig"):
            validate_hash_weight(value, context="core StateChecksConfig")

    def test_rejection_names_the_offending_value(self):
        with pytest.raises(ValueError, match="2.0"):
            validate_hash_weight(2.0, context="grading.yaml")
