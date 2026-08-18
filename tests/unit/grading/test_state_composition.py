"""Composition rule for the ``state_checks`` component, pinned per presence case.

The single-source pass-through is pinned across the whole weight domain rather
than at one value: it is what makes a tau-style pack (hash on, no jsonpaths)
score its hash verdict alone, and a weight that leaked into that branch would
move those verdicts silently.
"""

from itertools import product

import pytest

from tolokaforge.core.grading.state_composition import (
    CONFLICTING_STATE_SOURCES_MESSAGE,
    INERT_HASH_WEIGHT_REASON,
    MISSING_HASH_WEIGHT_MESSAGE,
    StateHashConfig,
    compose_state_checks_score,
    inert_hash_weight_reason,
    refuse_probes_beside_another_state_source,
    resolve_hash_weight,
    validate_hash_weight,
)

pytestmark = pytest.mark.unit

WEIGHT_DOMAIN = (0.0, 0.5, 0.6, 1.0, None)

_ASSERTIONS = [{"path": "$.db.widgets[0].status", "equals": "closed"}]
_GOLDEN_ACTIONS = [{"name": "close_widget"}]
_PROBES = [
    {
        "name": "widget_closed",
        "dsn": "postgresql://grader@app-db:5432/app",
        "query": "SELECT status FROM widgets WHERE id = 'W1'",
    }
]

# Every combination of the facts the predicate reads, swept over both ways of
# declaring a hash source. Exactly one shape is undecidable, so dropping any one
# condition changes which shapes reject.
_DECLARATION_SPACE = tuple(
    product(
        (True, False),  # hash.enabled
        ({"expect_initial_state": True}, {"golden_actions": _GOLDEN_ACTIONS}, {}),
        (_ASSERTIONS, []),  # state_checks.jsonpaths
        (0.5, None),  # hash.weight
    ),
)

# The same facts a block declares, swept for the exclusivity rule instead. No weight
# axis: a weight divides two shares of one component, and these shapes have no fold to
# divide.
_EXCLUSIVITY_SPACE = tuple(
    product(
        (_PROBES, []),  # state_checks.db_probes
        (True, False),  # hash.enabled
        ({"expect_initial_state": True}, {"golden_actions": _GOLDEN_ACTIONS}, {}),
        (_ASSERTIONS, []),  # state_checks.jsonpaths
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
    dropping a condition widened the rejection. The sweep is over the ``hash`` block an
    author actually writes, so it covers what counts as a hash source too.
    """

    def test_exactly_one_declaration_shape_is_undecidable(self):
        rejected = set()
        for enabled, source, jsonpaths, weight in _DECLARATION_SPACE:
            hash_config = StateHashConfig(enabled=enabled, weight=weight, **source)
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
            (True, ("expect_initial_state",), True, None),
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
        """The fold re-checks a weight the block would never have loaded with.

        Written past the block's own validator because that is the only way the value
        arrives: the block refuses each of these at construction, and this read is what
        answers a caller that reached past load and wrote one anyway.
        """
        hash_config = StateHashConfig(enabled=True, expect_initial_state=True, weight=0.5)
        hash_config.weight = declared_weight

        with pytest.raises(ValueError, match="state_checks.hash.weight"):
            resolve_hash_weight(
                hash_config,
                jsonpaths=jsonpaths,
                context="grading.yaml state_checks.hash.weight",
            )

    def test_an_absent_hash_block_declares_no_weight(self):
        assert resolve_hash_weight(None, jsonpaths=_ASSERTIONS, context="grading.yaml") is None


class TestRefuseProbesBesideAnotherStateSource:
    """Which shapes carry two state verdicts with nothing to choose between them.

    Swept exhaustively and asserted as a set for the reason :class:`TestResolveHashWeight`
    gives: the predicate's whole content is *which* shapes it refuses, and a sampled set
    cannot show that dropping a condition widened the rejection. The accepted rows are the
    narrow reading — probes beside a disabled hash, and probes beside an enabled hash with
    nothing to compare against, produce one verdict between them and stay loadable.
    """

    _CONTEXT = "grading.yaml state_checks"

    def test_exactly_the_shapes_carrying_two_verdicts_are_refused(self):
        rejected = set()
        for db_probes, enabled, source, jsonpaths in _EXCLUSIVITY_SPACE:
            shape = (bool(db_probes), enabled, tuple(source), bool(jsonpaths))
            try:
                refuse_probes_beside_another_state_source(
                    db_probes=db_probes,
                    jsonpaths=jsonpaths,
                    hash_config=StateHashConfig(enabled=enabled, **source),
                    context=self._CONTEXT,
                )
            except ValueError as exc:
                assert str(exc) == f"{self._CONTEXT}: {CONFLICTING_STATE_SOURCES_MESSAGE}", shape
                rejected.add(shape)

        assert rejected == {
            (True, True, ("expect_initial_state",), True),
            (True, True, ("expect_initial_state",), False),
            (True, True, ("golden_actions",), True),
            (True, True, ("golden_actions",), False),
            (True, True, (), True),
            (True, False, ("expect_initial_state",), True),
            (True, False, ("golden_actions",), True),
            (True, False, (), True),
        }, (
            "the rejected set is the whole predicate: probes declared, and beside them "
            "either assertions to score — whatever the hash block says — or a hash that "
            "is enabled with something to compare against. Any other membership means a "
            "condition was dropped or added."
        )

    def test_an_absent_hash_block_is_not_a_second_source(self):
        assert (
            refuse_probes_beside_another_state_source(
                db_probes=_PROBES, jsonpaths=[], hash_config=None, context=self._CONTEXT
            )
            is None
        )

    @pytest.mark.parametrize(
        "key", ("state_checks.db_probes", "state_checks.jsonpaths", "state_checks.hash")
    )
    def test_the_message_names_every_source_in_the_conflict(self, key):
        assert key in CONFLICTING_STATE_SOURCES_MESSAGE


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
