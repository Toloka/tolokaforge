"""Unit tests for tolokaforge.core.hash module.

Tests verify:
- filter_unstable_fields handles nested table.field patterns
- compute_stable_hash matches mcp_core's calculate_database_hash for same stable state
"""

import pytest

pytestmark = pytest.mark.unit

from tolokaforge.core.hash import canonical_number, compute_stable_hash, filter_unstable_fields

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
