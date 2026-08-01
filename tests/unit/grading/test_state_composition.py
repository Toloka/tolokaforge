"""Composition rule for the ``state_checks`` component, pinned per presence case.

The single-source pass-through is pinned across the whole weight domain rather
than at one value: it is what makes a tau-style pack (hash on, no jsonpaths)
score its hash verdict alone, and a weight that leaked into that branch would
move those verdicts silently.
"""

from itertools import product

import pytest

from tolokaforge.core.grading.state_composition import (
    INERT_HASH_WEIGHT_REASON,
    MISSING_HASH_WEIGHT_MESSAGE,
    compose_state_checks_score,
    inert_hash_weight_reason,
    resolve_hash_weight,
    validate_hash_weight,
)

pytestmark = pytest.mark.unit

WEIGHT_DOMAIN = (0.0, 0.5, 0.6, 1.0, None)

_ASSERTIONS = [{"path": "$.db.widgets[0].status", "equals": "closed"}]
_GOLDEN_ACTIONS = [{"name": "close_widget"}]

# Every combination of the facts the predicate reads, swept over both ways of
# declaring a hash source. Exactly one shape is undecidable, so dropping any one
# condition changes which shapes reject.
_DECLARATION_SPACE = tuple(
    product(
        (True, False),  # hash.enabled
        ({"expected_state_hash": "aaaa"}, {"golden_actions": _GOLDEN_ACTIONS}, {}),
        (_ASSERTIONS, []),  # state_checks.jsonpaths
        (0.5, None),  # hash.weight
    ),
)


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


class TestResolveHashWeight:
    """A weight is mandatory exactly where the composer consults it.

    The declaration space is swept exhaustively rather than sampled: the predicate's
    whole content is *which* shape is undecidable, and a sampled set cannot show that
    dropping a condition widened the rejection. The sweep is over the untyped ``hash``
    block an author actually writes, so it covers what counts as a hash source too.
    """

    def test_exactly_one_declaration_shape_is_undecidable(self):
        rejected = set()
        for enabled, source, jsonpaths, weight in _DECLARATION_SPACE:
            hash_config = {"enabled": enabled, **source}
            if weight is not None:
                hash_config["weight"] = weight
            shape = (enabled, tuple(source), bool(jsonpaths), weight)
            try:
                returned = resolve_hash_weight(
                    hash_config,
                    jsonpaths=jsonpaths,
                    context="grading.yaml state_checks.hash.weight",
                )
            except ValueError as exc:
                assert str(exc) == MISSING_HASH_WEIGHT_MESSAGE, shape
                rejected.add(shape)
                continue
            assert returned == weight, shape

        assert rejected == {
            (True, ("expected_state_hash",), True, None),
            (True, ("golden_actions",), True, None),
        }, (
            "the rejected set is the whole predicate: hash grading on, a source to "
            "grade against, assertions to weigh it against, and no weight — for each "
            "of the two hash sources. Any other membership means a condition was "
            "dropped or added."
        )

    @pytest.mark.parametrize("declared_weight", (2.0, -0.1, "0.5", True))
    @pytest.mark.parametrize("jsonpaths", (_ASSERTIONS, []))
    def test_the_range_holds_even_where_the_weight_is_inert(self, declared_weight, jsonpaths):
        with pytest.raises(ValueError, match="state_checks.hash.weight"):
            resolve_hash_weight(
                {"enabled": True, "expected_state_hash": "aaaa", "weight": declared_weight},
                jsonpaths=jsonpaths,
                context="grading.yaml state_checks.hash.weight",
            )

    def test_an_absent_hash_block_declares_no_weight(self):
        assert resolve_hash_weight(None, jsonpaths=_ASSERTIONS, context="grading.yaml") is None


class TestInertHashWeightReason:
    """A declared weight the composer skipped is reported, not silently dropped."""

    @pytest.mark.parametrize(
        ("hash_score", "jsonpath_score"),
        ((1.0, None), (None, 0.5), (None, None)),
    )
    def test_a_declared_weight_the_composer_skipped_is_named(self, hash_score, jsonpath_score):
        assert (
            inert_hash_weight_reason(
                hash_score=hash_score, jsonpath_score=jsonpath_score, hash_weight=0.6
            )
            == INERT_HASH_WEIGHT_REASON
        )

    def test_a_consulted_weight_earns_no_reason(self):
        assert inert_hash_weight_reason(hash_score=1.0, jsonpath_score=0.5, hash_weight=0.6) is None

    @pytest.mark.parametrize(
        ("hash_score", "jsonpath_score"), ((1.0, 0.5), (1.0, None), (None, 0.5), (None, None))
    )
    def test_an_undeclared_weight_earns_no_reason(self, hash_score, jsonpath_score):
        assert (
            inert_hash_weight_reason(
                hash_score=hash_score, jsonpath_score=jsonpath_score, hash_weight=None
            )
            is None
        )
