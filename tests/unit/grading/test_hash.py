"""Unit tests for tolokaforge.core.hash module.

Tests verify:
- filter_unstable_fields handles nested table.field patterns
- compute_stable_hash matches mcp_core's calculate_database_hash for same stable state
"""

import pytest

pytestmark = pytest.mark.unit

from tolokaforge.core.grading.state_checks import state_digest
from tolokaforge.core.hash import (
    AUTO_MASKED_CLOCK_COLUMNS,
    ColumnCompareRule,
    apply_auto_clock_mask,
    apply_compare_columns_equivalences,
    apply_compare_columns_extras,
    apply_compare_columns_ordering,
    canonical_number,
    compute_stable_hash,
    filter_unstable_fields,
)

# ---------------------------------------------------------------------------
# Test 7: filter_unstable_fields handles nested table.field patterns
# ---------------------------------------------------------------------------


class TestFilterUnstableFields:
    """Verify filter_unstable_fields strips nested table.field patterns."""

    def test_nested_table_field_patterns(self):
        """Dot-notation unstable fields like 'tickets.subject' filter correctly."""
        state = {
            "tickets": [
                {"id": "1", "subject": "test ticket", "status": "open"},
            ],
            "users": [
                {"id": "2", "name": "Bob", "created_at": "2025-01-01"},
            ],
        }
        unstable = ["tickets.subject", "users.created_at"]

        filtered = filter_unstable_fields(state, unstable)

        # Subject should be removed from tickets
        assert "subject" not in filtered["tickets"][0]
        assert filtered["tickets"][0]["id"] == "1"
        assert filtered["tickets"][0]["status"] == "open"

        # created_at should be removed from users
        assert "created_at" not in filtered["users"][0]
        assert filtered["users"][0]["id"] == "2"
        assert filtered["users"][0]["name"] == "Bob"

    def test_no_unstable_fields_returns_original(self):
        """When unstable_fields is None or empty, state is returned unchanged."""
        state = {"orders": [{"id": "1", "status": "pending", "total": 42.0}]}

        assert filter_unstable_fields(state, None) is state
        assert filter_unstable_fields(state, []) is state

    def test_multiple_fields_per_table(self):
        """Multiple unstable fields in one table are all stripped."""
        state = {
            "tickets": [
                {
                    "id": "1",
                    "subject": "stripped",
                    "description": "stripped",
                    "status": "open",
                    "priority": "high",
                },
            ],
        }
        unstable = ["tickets.subject", "tickets.description"]

        filtered = filter_unstable_fields(state, unstable)
        ticket = filtered["tickets"][0]

        assert "subject" not in ticket
        assert "description" not in ticket
        assert ticket["status"] == "open"
        assert ticket["priority"] == "high"

    def test_unmatched_patterns_leave_data_intact(self):
        """Unstable patterns for nonexistent tables/fields don't corrupt data."""
        state = {"orders": [{"id": "1", "status": "pending"}]}
        unstable = ["nonexistent_table.field", "orders.nonexistent_field"]

        filtered = filter_unstable_fields(state, unstable)
        assert filtered["orders"][0] == {"id": "1", "status": "pending"}


# ---------------------------------------------------------------------------
# apply_compare_columns_extras: per-(table, column) subset semantics
# ---------------------------------------------------------------------------


class TestApplyCompareColumnsExtras:
    """Per-column ``mode: subset`` drops permitted extras the golden did not carry."""

    @staticmethod
    def _rule(*extras: str) -> ColumnCompareRule:
        return ColumnCompareRule(mode="subset", extras_allowed_for=list(extras))

    def test_unset_is_a_no_op(self):
        actual = {"notifications": [{"id": "n1", "params": {"body": "hi"}}]}
        expected = {"notifications": [{"id": "n1", "params": {"body": "hi"}}]}
        assert apply_compare_columns_extras(actual, expected, None) == actual
        assert apply_compare_columns_extras(actual, expected, {}) == actual

    def test_declared_extra_absent_from_golden_is_dropped_from_actual(self):
        """The motivating case: SOP permits ``param_case_number``, golden omits it,
        model adds it — hash should not fail on this key."""
        actual = {
            "notifications": [{"id": "n1", "params": {"body": "hi", "param_case_number": "C-1"}}]
        }
        expected = {"notifications": [{"id": "n1", "params": {"body": "hi"}}]}
        rules = {"notifications": {"params": self._rule("param_case_number")}}
        filtered = apply_compare_columns_extras(actual, expected, rules)
        assert filtered["notifications"][0]["params"] == {"body": "hi"}
        assert state_digest(filtered) == state_digest(expected)

    def test_extra_not_in_allowlist_still_fails(self):
        """Only keys the pack named are dropped; other model-added keys stay."""
        actual = {"notifications": [{"id": "n1", "params": {"body": "hi", "unlisted_key": "x"}}]}
        expected = {"notifications": [{"id": "n1", "params": {"body": "hi"}}]}
        rules = {"notifications": {"params": self._rule("param_case_number")}}
        filtered = apply_compare_columns_extras(actual, expected, rules)
        assert filtered["notifications"][0]["params"] == {
            "body": "hi",
            "unlisted_key": "x",
        }
        assert state_digest(filtered) != state_digest(expected)

    def test_key_declared_in_golden_is_compared_value_for_value(self):
        """Extras allowed only when golden lacks the key — when golden has it,
        a value mismatch on the same key still fails the hash."""
        actual = {"notifications": [{"id": "n1", "params": {"param_case_number": "MODEL"}}]}
        expected = {"notifications": [{"id": "n1", "params": {"param_case_number": "GOLDEN"}}]}
        rules = {"notifications": {"params": self._rule("param_case_number")}}
        filtered = apply_compare_columns_extras(actual, expected, rules)
        assert filtered["notifications"][0]["params"] == {"param_case_number": "MODEL"}
        assert state_digest(filtered) != state_digest(expected)

    def test_composes_with_unstable_fields_mask(self):
        """``unstable_fields`` drops the whole column (symmetric); ``compare_columns``
        drops permitted extra keys inside a surviving column. The two do not fight."""
        actual = {
            "notifications": [
                {
                    "id": "n1",
                    "created_at": "2026-09-08",
                    "params": {"body": "hi", "param_case_number": "C-1"},
                }
            ]
        }
        expected = {
            "notifications": [{"id": "n1", "created_at": "2020-01-01", "params": {"body": "hi"}}]
        }
        rules = {"notifications": {"params": self._rule("param_case_number")}}
        filtered_actual = apply_compare_columns_extras(actual, expected, rules)
        masked_actual = filter_unstable_fields(filtered_actual, ["notifications.created_at"])
        masked_expected = filter_unstable_fields(expected, ["notifications.created_at"])
        assert state_digest(masked_actual) == state_digest(masked_expected)

    def test_non_dict_column_value_is_left_alone(self):
        """The rule only makes sense for dict columns. Non-dict values pass through."""
        actual = {"rows": [{"id": "r1", "params": "just-a-string"}]}
        expected = {"rows": [{"id": "r1", "params": "just-a-string"}]}
        rules = {"rows": {"params": self._rule("anything")}}
        filtered = apply_compare_columns_extras(actual, expected, rules)
        assert filtered == actual

    def test_missing_table_or_column_is_skipped_silently(self):
        """A rule for a table that either state omits is a no-op — the containing
        hash still catches genuine schema drift."""
        actual = {"other": [{"id": "o1"}]}
        expected = {"other": [{"id": "o1"}]}
        rules = {"notifications": {"params": self._rule("param_case_number")}}
        assert apply_compare_columns_extras(actual, expected, rules) == actual

    def test_list_table_rows_paired_positionally(self):
        """Table rows are paired index-by-index: row N's actual against row N's expected."""
        actual = {
            "notifications": [
                {"id": "n1", "params": {"body": "a", "param_case_number": "C-1"}},
                {"id": "n2", "params": {"body": "b", "param_case_number": "C-2"}},
            ]
        }
        expected = {
            "notifications": [
                {"id": "n1", "params": {"body": "a"}},
                {"id": "n2", "params": {"body": "b"}},
            ]
        }
        rules = {"notifications": {"params": self._rule("param_case_number")}}
        filtered = apply_compare_columns_extras(actual, expected, rules)
        assert state_digest(filtered) == state_digest(expected)

    def test_dict_column_pair_shape_also_supported(self):
        """A table stored as a single dict (not list-of-rows) is handled too."""
        actual = {"config": {"id": "c1", "params": {"a": 1, "param_case_number": "X"}}}
        expected = {"config": {"id": "c1", "params": {"a": 1}}}
        rules = {"config": {"params": self._rule("param_case_number")}}
        filtered = apply_compare_columns_extras(actual, expected, rules)
        assert state_digest(filtered) == state_digest(expected)


# ---------------------------------------------------------------------------
# apply_compare_columns_equivalences: per-(table, column) scalar folds
# ---------------------------------------------------------------------------


def _hashes_agree(a: dict, b: dict) -> bool:
    return state_digest(a) == state_digest(b)


class TestTreatNullAsEmptyCollection:
    """H1 — a column may be ``null`` in one state and ``[]`` (or ``{}``) in the other
    without failing the hash when the pack declares that equivalence."""

    def test_default_off_still_fails_on_mismatch(self):
        """Baseline: without the flag, null vs [] disagree at the hash."""
        actual = {"d365_cases": [{"id": "c1", "custom_tags": None}]}
        expected = {"d365_cases": [{"id": "c1", "custom_tags": []}]}
        assert not _hashes_agree(actual, expected)

    def test_declared_flag_folds_null_and_empty_list(self):
        actual = {"d365_cases": [{"id": "c1", "custom_tags": None}]}
        expected = {"d365_cases": [{"id": "c1", "custom_tags": []}]}
        rules = {
            "d365_cases": {"custom_tags": ColumnCompareRule(treat_null_as_empty_collection=True)}
        }
        folded_actual = apply_compare_columns_equivalences(actual, rules)
        folded_expected = apply_compare_columns_equivalences(expected, rules)
        assert _hashes_agree(folded_actual, folded_expected)

    def test_declared_flag_folds_null_and_empty_dict(self):
        actual = {"d365_cases": [{"id": "c1", "custom_tags": None}]}
        expected = {"d365_cases": [{"id": "c1", "custom_tags": {}}]}
        rules = {
            "d365_cases": {"custom_tags": ColumnCompareRule(treat_null_as_empty_collection=True)}
        }
        assert _hashes_agree(
            apply_compare_columns_equivalences(actual, rules),
            apply_compare_columns_equivalences(expected, rules),
        )

    def test_declared_flag_still_fails_on_non_empty_diff(self):
        """The fold only equates empties with null — a real value still fails."""
        actual = {"d365_cases": [{"id": "c1", "custom_tags": ["urgent"]}]}
        expected = {"d365_cases": [{"id": "c1", "custom_tags": None}]}
        rules = {
            "d365_cases": {"custom_tags": ColumnCompareRule(treat_null_as_empty_collection=True)}
        }
        assert not _hashes_agree(
            apply_compare_columns_equivalences(actual, rules),
            apply_compare_columns_equivalences(expected, rules),
        )


class TestNormalizeTimezoneSuffix:
    """H2 — a datetime column may carry ``Z`` on one side and naive on the other."""

    def test_default_off_still_fails_on_mismatch(self):
        actual = {"orders": [{"id": "o1", "approx_delivery_date": "2026-09-15T12:00:00Z"}]}
        expected = {"orders": [{"id": "o1", "approx_delivery_date": "2026-09-15T12:00:00"}]}
        assert not _hashes_agree(actual, expected)

    def test_declared_flag_folds_trailing_Z(self):
        actual = {"orders": [{"id": "o1", "approx_delivery_date": "2026-09-15T12:00:00Z"}]}
        expected = {"orders": [{"id": "o1", "approx_delivery_date": "2026-09-15T12:00:00"}]}
        rules = {
            "orders": {"approx_delivery_date": ColumnCompareRule(normalize_timezone_suffix=True)}
        }
        assert _hashes_agree(
            apply_compare_columns_equivalences(actual, rules),
            apply_compare_columns_equivalences(expected, rules),
        )

    def test_declared_flag_folds_trailing_utc_offset(self):
        actual = {"orders": [{"id": "o1", "approx_delivery_date": "2026-09-15T12:00:00+00:00"}]}
        expected = {"orders": [{"id": "o1", "approx_delivery_date": "2026-09-15T12:00:00"}]}
        rules = {
            "orders": {"approx_delivery_date": ColumnCompareRule(normalize_timezone_suffix=True)}
        }
        assert _hashes_agree(
            apply_compare_columns_equivalences(actual, rules),
            apply_compare_columns_equivalences(expected, rules),
        )

    def test_declared_flag_still_fails_on_genuine_time_diff(self):
        """The fold only strips a trailing UTC marker — a different instant still fails."""
        actual = {"orders": [{"id": "o1", "approx_delivery_date": "2026-09-15T13:00:00Z"}]}
        expected = {"orders": [{"id": "o1", "approx_delivery_date": "2026-09-15T12:00:00"}]}
        rules = {
            "orders": {"approx_delivery_date": ColumnCompareRule(normalize_timezone_suffix=True)}
        }
        assert not _hashes_agree(
            apply_compare_columns_equivalences(actual, rules),
            apply_compare_columns_equivalences(expected, rules),
        )


class TestTreatEmptyStringAsNull:
    """H7 — a nullable-string column may be ``""`` in one state and ``null`` in the other."""

    def test_default_off_still_fails_on_mismatch(self):
        actual = {"cases": [{"id": "c1", "custom_corporate_account_id": ""}]}
        expected = {"cases": [{"id": "c1", "custom_corporate_account_id": None}]}
        assert not _hashes_agree(actual, expected)

    def test_declared_flag_folds_empty_and_null(self):
        actual = {"cases": [{"id": "c1", "custom_corporate_account_id": ""}]}
        expected = {"cases": [{"id": "c1", "custom_corporate_account_id": None}]}
        rules = {
            "cases": {
                "custom_corporate_account_id": ColumnCompareRule(treat_empty_string_as_null=True)
            }
        }
        assert _hashes_agree(
            apply_compare_columns_equivalences(actual, rules),
            apply_compare_columns_equivalences(expected, rules),
        )

    def test_declared_flag_still_fails_on_real_value_diff(self):
        actual = {"cases": [{"id": "c1", "custom_corporate_account_id": "ACC-1"}]}
        expected = {"cases": [{"id": "c1", "custom_corporate_account_id": None}]}
        rules = {
            "cases": {
                "custom_corporate_account_id": ColumnCompareRule(treat_empty_string_as_null=True)
            }
        }
        assert not _hashes_agree(
            apply_compare_columns_equivalences(actual, rules),
            apply_compare_columns_equivalences(expected, rules),
        )


class TestEquivalenceFoldsCompose:
    """A rule setting multiple equivalence flags folds every declared shape to one."""

    def test_null_empty_collection_and_empty_string_share_a_bucket(self):
        """When both flags are set, None ≡ [] ≡ {} ≡ "" all compare equal."""
        rule = ColumnCompareRule(
            treat_null_as_empty_collection=True,
            treat_empty_string_as_null=True,
        )
        rules = {"t": {"col": rule}}
        variants = [
            {"t": [{"col": None}]},
            {"t": [{"col": []}]},
            {"t": [{"col": {}}]},
            {"t": [{"col": ""}]},
        ]
        folded = [apply_compare_columns_equivalences(v, rules) for v in variants]
        base = folded[0]
        for other in folded[1:]:
            assert _hashes_agree(base, other)

    def test_subset_mode_still_works_alongside_equivalence_flags(self):
        """Legacy ``mode: subset`` rule remains intact when equivalence flags are also set
        on a sibling column — they are separate concerns wired through separate helpers."""
        actual = {"t": [{"id": "1", "params": {"body": "hi", "case_no": "C-1"}, "tag": None}]}
        expected = {"t": [{"id": "1", "params": {"body": "hi"}, "tag": []}]}
        rules = {
            "t": {
                "params": ColumnCompareRule(mode="subset", extras_allowed_for=["case_no"]),
                "tag": ColumnCompareRule(treat_null_as_empty_collection=True),
            }
        }
        filtered = apply_compare_columns_extras(actual, expected, rules)
        folded_actual = apply_compare_columns_equivalences(filtered, rules)
        folded_expected = apply_compare_columns_equivalences(expected, rules)
        assert _hashes_agree(folded_actual, folded_expected)


# ---------------------------------------------------------------------------
# apply_compare_columns_ordering: per-table row-permutation-insensitive hashing
# ---------------------------------------------------------------------------


class TestUnorderedRowsAtHash:
    """H4 — a table whose domain semantics are set-of-rows may hash equal on any
    permutation of the same rows when the pack declares ``order: unordered``.

    Sorting runs after equivalence folds so two rows already declared equal by
    scalar equivalence sort to the same position.
    """

    def test_default_ordered_still_fails_on_row_permutation(self):
        """Baseline: without the flag, a pure permutation disagrees at the hash."""
        actual = {"notifications": [{"id": "a"}, {"id": "b"}]}
        expected = {"notifications": [{"id": "b"}, {"id": "a"}]}
        assert not _hashes_agree(actual, expected)

    def test_unordered_agrees_on_permutation(self):
        actual = {"notifications": [{"id": "a"}, {"id": "b"}]}
        expected = {"notifications": [{"id": "b"}, {"id": "a"}]}
        rules = {"notifications": {"id": ColumnCompareRule(order="unordered")}}
        assert _hashes_agree(
            apply_compare_columns_ordering(actual, rules),
            apply_compare_columns_ordering(expected, rules),
        )

    def test_unordered_still_fails_on_missing_row(self):
        """Order-insensitivity is not content-insensitivity — a missing row still fails."""
        actual = {"notifications": [{"id": "a"}]}
        expected = {"notifications": [{"id": "b"}, {"id": "a"}]}
        rules = {"notifications": {"id": ColumnCompareRule(order="unordered")}}
        assert not _hashes_agree(
            apply_compare_columns_ordering(actual, rules),
            apply_compare_columns_ordering(expected, rules),
        )

    def test_unordered_still_fails_on_extra_row(self):
        actual = {"notifications": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
        expected = {"notifications": [{"id": "b"}, {"id": "a"}]}
        rules = {"notifications": {"id": ColumnCompareRule(order="unordered")}}
        assert not _hashes_agree(
            apply_compare_columns_ordering(actual, rules),
            apply_compare_columns_ordering(expected, rules),
        )

    def test_unordered_declaration_on_any_column_covers_the_table(self):
        """Ordering is a table-level property; declaring it on one column-rule affects the whole row list."""
        actual = {"notifications": [{"id": "a", "body": "hi"}, {"id": "b", "body": "yo"}]}
        expected = {"notifications": [{"id": "b", "body": "yo"}, {"id": "a", "body": "hi"}]}
        rules = {"notifications": {"body": ColumnCompareRule(order="unordered")}}
        assert _hashes_agree(
            apply_compare_columns_ordering(actual, rules),
            apply_compare_columns_ordering(expected, rules),
        )

    def test_ordering_composes_with_equivalences(self):
        """Equivalences fold values first; ordering then sees identical rows."""
        actual = {"tags": [{"id": "a", "custom_tags": None}, {"id": "b", "custom_tags": []}]}
        expected = {"tags": [{"id": "b", "custom_tags": None}, {"id": "a", "custom_tags": []}]}
        rules = {
            "tags": {
                "custom_tags": ColumnCompareRule(
                    treat_null_as_empty_collection=True,
                    order="unordered",
                )
            }
        }
        folded_actual = apply_compare_columns_equivalences(actual, rules)
        folded_expected = apply_compare_columns_equivalences(expected, rules)
        sorted_actual = apply_compare_columns_ordering(folded_actual, rules)
        sorted_expected = apply_compare_columns_ordering(folded_expected, rules)
        assert _hashes_agree(sorted_actual, sorted_expected)

    def test_ordering_untouched_when_no_rule_declares_unordered(self):
        """A table whose only rules are equivalence-only is a no-op for ordering."""
        state = {"notifications": [{"id": "a"}, {"id": "b"}]}
        rules = {"notifications": {"id": ColumnCompareRule(treat_empty_string_as_null=True)}}
        result = apply_compare_columns_ordering(state, rules)
        assert result["notifications"] == state["notifications"]


class TestAutoMaskClockColumns:
    """H8a/b/c — conventional write-time clock columns (``updated_at``,
    ``last_modified_date`` …) drop from every row on both sides when the pack
    opts into :data:`AUTO_MASKED_CLOCK_COLUMNS`.
    """

    def test_default_off_still_fails_on_clock_diff(self):
        """Baseline: without the flag, a differing clock column disagrees."""
        actual = {"cases": [{"id": "1", "updated_at": "2026-09-14T13:00:05Z"}]}
        expected = {"cases": [{"id": "1", "updated_at": "2026-09-14T13:00:00Z"}]}
        assert compute_stable_hash(actual) != compute_stable_hash(expected)

    def test_declared_flag_folds_differing_clocks_via_compute_stable_hash(self):
        actual = {"cases": [{"id": "1", "updated_at": "2026-09-14T13:00:05Z"}]}
        expected = {"cases": [{"id": "1", "updated_at": "2026-09-14T13:00:00Z"}]}
        assert compute_stable_hash(actual, auto_mask_clock_columns=True) == compute_stable_hash(
            expected, auto_mask_clock_columns=True
        )

    def test_declared_flag_folds_differing_clocks_via_state_digest(self):
        """Core substrate agrees with runner substrate on the mask."""
        actual = {"cases": [{"id": "1", "last_modified_date": "2026-09-14T13:00:05Z"}]}
        expected = {"cases": [{"id": "1", "last_modified_date": "2026-09-14T13:00:00Z"}]}
        assert state_digest(actual, auto_mask_clock_columns=True) == state_digest(
            expected, auto_mask_clock_columns=True
        )

    def test_declared_flag_still_fails_on_real_content_diff(self):
        """The mask drops clock columns; a real content diff still fails."""
        actual = {"cases": [{"id": "1", "status": "open", "updated_at": "2026-09-14T13:00:05Z"}]}
        expected = {
            "cases": [{"id": "1", "status": "closed", "updated_at": "2026-09-14T13:00:00Z"}]
        }
        assert compute_stable_hash(actual, auto_mask_clock_columns=True) != compute_stable_hash(
            expected, auto_mask_clock_columns=True
        )

    def test_composes_with_pack_unstable_fields(self):
        """A pack-declared ``unstable_fields`` mask still drops what it drops;
        the auto mask covers the clock columns the pack forgot. Both fold to
        the same state on both sides.
        """
        actual = {
            "cases": [
                {
                    "id": "1",
                    "updated_at": "2026-09-14T13:00:05Z",
                    "notes": "short answer A",
                }
            ]
        }
        expected = {
            "cases": [
                {
                    "id": "1",
                    "updated_at": "2026-09-14T13:00:00Z",
                    "notes": "short answer B",
                }
            ]
        }
        # Pack-declared mask alone: clock is ignored by our new flag,
        # but "notes" (which the pack itself decided to mask) still diverges
        # only under the pack mask, and NOT under the auto mask on its own.
        assert compute_stable_hash(
            actual,
            unstable_fields=["cases.notes"],
            auto_mask_clock_columns=True,
        ) == compute_stable_hash(
            expected,
            unstable_fields=["cases.notes"],
            auto_mask_clock_columns=True,
        )

    def test_covers_every_declared_clock_column(self):
        """Every name in :data:`AUTO_MASKED_CLOCK_COLUMNS` is dropped."""
        for column in AUTO_MASKED_CLOCK_COLUMNS:
            actual = {"t": [{"id": "1", column: "2026-09-14T13:00:05Z"}]}
            expected = {"t": [{"id": "1", column: "2026-09-14T13:00:00Z"}]}
            assert compute_stable_hash(actual, auto_mask_clock_columns=True) == compute_stable_hash(
                expected, auto_mask_clock_columns=True
            ), column

    def test_apply_auto_clock_mask_leaves_nonclock_columns_alone(self):
        """Direct helper contract: only known clock names are dropped."""
        state = {
            "t": [
                {"id": "1", "updated_at": "x", "note": "keep", "created_at": "keep-too"},
            ]
        }
        result = apply_auto_clock_mask(state)
        assert result["t"][0].keys() == {"id", "note", "created_at"}


# ---------------------------------------------------------------------------
# compute_stable_hash standalone behavior
#
# NOTE: the cross-implementation contract test that verified
# ``tolokaforge.core.hash.compute_stable_hash`` produces the same hash as
# ``mcp_core.utils.validation.calculate_database_hash`` lives in the
# adapter package's test suite, because it requires ``mcp_core`` to be
# importable.
# ---------------------------------------------------------------------------

import copy


class TestComputeStableHash:
    """Verify compute_stable_hash determinism, sensitivity, and edge cases."""

    def test_compute_stable_hash_deterministic(self):
        """Same input always produces the same hash."""
        state = {"users": [{"id": "1", "name": "Alice"}]}

        hash1 = compute_stable_hash(state)
        hash2 = compute_stable_hash(state)

        assert hash1 == hash2

    def test_compute_stable_hash_different_inputs(self):
        """Different inputs produce different hashes."""
        state_a = {"users": [{"id": "1", "name": "Alice"}]}
        state_b = {"users": [{"id": "1", "name": "Bob"}]}

        assert compute_stable_hash(state_a) != compute_stable_hash(state_b)

    def test_compute_stable_hash_empty_dict(self):
        """Empty dict produces a valid 64-char hex hash."""
        result = compute_stable_hash({})

        assert isinstance(result, str)
        assert len(result) == 64
        # Must be valid hexadecimal
        int(result, 16)

    def test_compute_stable_hash_sorted_keys(self):
        """Dict key order doesn't affect hash."""
        state_ordered = {"a": 1, "b": 2, "c": 3}
        state_reversed = {"c": 3, "b": 2, "a": 1}

        assert compute_stable_hash(state_ordered) == compute_stable_hash(state_reversed)

    def test_filter_unstable_fields_preserves_original(self):
        """Original dict is not mutated by filter_unstable_fields."""
        state = {
            "tickets": [
                {"id": "1", "subject": "original", "status": "open"},
            ],
        }
        original = copy.deepcopy(state)

        filter_unstable_fields(state, ["tickets.subject"])

        assert state == original, "filter_unstable_fields must not mutate the original dict"


class TestComputeStableHashNumericCanonicalization:
    """Two-tier numeric canonicalization in state hashing.

    Tier 1 (default): numerically-equal NUMERIC-TYPE values hash identically
    (72 == 72.0 == Decimal("72.00")) — the type declares number-ness, generic
    and safe. Tier 2 (opt-in ``numeric_string_fields``): numeric-looking
    STRINGS also fold ("130.00" == "130.0") but ONLY under a record key listed
    in that per-field set, because exact string representation can carry meaning
    (versions, codes).
    """

    # ---- tier 1: numeric types, default behavior ----

    def test_numeric_types_fold_by_default(self):
        from decimal import Decimal

        a = {"cases": [{"id": "C1", "qty": 72}]}
        b = {"cases": [{"id": "C1", "qty": 72.0}]}
        c = {"cases": [{"id": "C1", "qty": Decimal("72.00")}]}
        assert compute_stable_hash(a) == compute_stable_hash(b) == compute_stable_hash(c)

    def test_numeric_strings_do_NOT_fold_by_default(self):
        a = {"cases": [{"id": "C1", "refund": "130.00"}]}
        b = {"cases": [{"id": "C1", "refund": "130.0"}]}
        assert compute_stable_hash(a) != compute_stable_hash(b)

    def test_bool_not_collapsed_to_int(self):
        a = {"flags": [{"active": True}]}
        b = {"flags": [{"active": 1}]}
        assert compute_stable_hash(a) != compute_stable_hash(b)

    def test_opt_out_preserves_legacy_byte_exact_behavior(self):
        from decimal import Decimal

        a = {"cases": [{"id": "C1", "qty": 72}]}
        b = {"cases": [{"id": "C1", "qty": Decimal("72.00")}]}
        # Legacy mcp_core-exact behavior: str(72) != str(Decimal("72.00")).
        assert compute_stable_hash(a, canonicalize_numbers=False) != compute_stable_hash(
            b, canonicalize_numbers=False
        )

    # ---- tier 2: numeric strings, only under a listed field ----

    def test_decimal_string_formats_fold_for_listed_field(self):
        a = {"cases": [{"id": "C1", "refund": "130.00"}]}
        b = {"cases": [{"id": "C1", "refund": "130.0"}]}
        assert compute_stable_hash(a, numeric_string_fields=["refund"]) == compute_stable_hash(
            b, numeric_string_fields=["refund"]
        )

    def test_int_vs_decimal_string_folds_for_listed_field(self):
        a = {"cases": [{"id": "C1", "qty": 72}]}
        b = {"cases": [{"id": "C1", "qty": "72.00"}]}
        assert compute_stable_hash(a, numeric_string_fields=["qty"]) == compute_stable_hash(
            b, numeric_string_fields=["qty"]
        )

    def test_unlisted_field_does_NOT_fold_even_with_a_sibling_listed(self):
        # The whole point of per-field: a version string in the same record as a
        # listed money field must NOT fold just because the money field is listed.
        a = {"cases": [{"refund": "130.00", "version": "1.10"}]}
        b = {"cases": [{"refund": "130.0", "version": "1.1"}]}
        # "version" is not listed → its "1.10" vs "1.1" difference is preserved,
        # so the two states stay distinct even though "refund" folds.
        assert compute_stable_hash(a, numeric_string_fields=["refund"]) != compute_stable_hash(
            b, numeric_string_fields=["refund"]
        )
        # Listing "version" too collapses both, confirming the difference was the
        # version field alone.
        assert compute_stable_hash(
            a, numeric_string_fields=["refund", "version"]
        ) == compute_stable_hash(b, numeric_string_fields=["refund", "version"])

    def test_genuine_numeric_difference_differs_even_when_listed(self):
        a = {"cases": [{"id": "C1", "refund": "790.00"}]}
        b = {"cases": [{"id": "C1", "refund": "0.0"}]}
        assert compute_stable_hash(a, numeric_string_fields=["refund"]) != compute_stable_hash(
            b, numeric_string_fields=["refund"]
        )

    def test_leading_zero_identifier_not_collapsed_even_when_listed(self):
        a = {"cases": [{"code": "00123"}]}
        b = {"cases": [{"code": "123"}]}
        assert compute_stable_hash(a, numeric_string_fields=["code"]) != compute_stable_hash(
            b, numeric_string_fields=["code"]
        )

    def test_negative_zero_collapses_to_zero_when_listed(self):
        assert canonical_number("-0.00", normalize_strings=True) == canonical_number(0)
        assert compute_stable_hash(
            {"t": [{"amt": "-0.00"}]}, numeric_string_fields=["amt"]
        ) == compute_stable_hash({"t": [{"amt": "0.0"}]}, numeric_string_fields=["amt"])

    # ---- guards independent of the field set ----

    def test_tagged_string_does_not_collide_with_number(self):
        # A crafted string byte-equal to a numeric token must not equal the number.
        crafted = "\x00tf-num:130"
        assert canonical_number(crafted, normalize_strings=True) != canonical_number(130)
        assert compute_stable_hash(
            {"t": [{"amt": 130}]}, numeric_string_fields=["amt"]
        ) != compute_stable_hash({"t": [{"amt": crafted}]}, numeric_string_fields=["amt"])


def _load_standalone_fallback_hash():
    """Import the json_db_service standalone ``compute_stable_hash`` fallback.

    The service prefers ``tolokaforge.core.hash`` and only defines the local
    fallback when that import fails (standalone/testing). Force the ImportError
    branch by poisoning ``sys.modules`` so we exercise the vendored copy, then
    restore the real module so no other test is affected.
    """
    import builtins
    import importlib
    import sys

    real_import = builtins.__import__

    def blocking_import(name, *args, **kwargs):
        if name == "tolokaforge.core.hash":
            raise ImportError("forced for parity test")
        return real_import(name, *args, **kwargs)

    saved = {m: sys.modules[m] for m in list(sys.modules) if "json_db_service" in m}
    for m in saved:
        del sys.modules[m]
    builtins.__import__ = blocking_import
    try:
        app = importlib.import_module("tolokaforge.env.json_db_service.app")
        fn = app.compute_stable_hash
        # Guard: make sure we actually got the vendored fallback (defined in
        # app.py), not the real core function — otherwise this parity test would
        # silently compare the real implementation against itself.
        assert (
            fn.__module__ == "tolokaforge.env.json_db_service.app"
        ), "fallback poisoning failed; got the real core.hash function"
        return fn
    finally:
        builtins.__import__ = real_import
        for m in list(sys.modules):
            if "json_db_service" in m:
                del sys.modules[m]
        sys.modules.update(saved)


class TestStandaloneFallbackParity:
    """The json_db_service standalone fallback must stay byte-identical to the
    real core ``compute_stable_hash`` (the only current sync guard is a comment).
    """

    def test_fallback_matches_core_across_cases(self):
        fallback = _load_standalone_fallback_hash()

        cases = [
            # (state, kwargs)
            ({"cases": [{"id": "C1", "qty": 72}]}, {}),
            ({"cases": [{"id": "C1", "qty": 72.0}]}, {}),
            ({"t": [{"amt": "130.00", "ver": "1.10"}]}, {"numeric_string_fields": ["amt"]}),
            ({"t": [{"amt": "130.0", "ver": "1.1"}]}, {"numeric_string_fields": ["amt"]}),
            (
                {"t": [{"amt": "130.00", "ver": "1.10"}]},
                {"numeric_string_fields": ["amt", "ver"]},
            ),
            ({"cases": [{"code": "00123"}]}, {"numeric_string_fields": ["code"]}),
            ({"flags": [{"active": True}]}, {"numeric_string_fields": ["active"]}),
            ({"t": [{"amt": "-0.00"}]}, {"numeric_string_fields": ["amt"]}),
            ({"t": [{"amt": "\x00tf-num:130"}]}, {"numeric_string_fields": ["amt"]}),
            ({"cases": [{"id": "C1", "qty": 72}]}, {"canonicalize_numbers": False}),
        ]
        for state, kwargs in cases:
            assert fallback(state, **kwargs) == compute_stable_hash(state, **kwargs), (
                state,
                kwargs,
            )
