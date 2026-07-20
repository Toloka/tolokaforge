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
    """Numerically-equal state values must hash identically (grading false-fail fix).

    Regression: the DB round-trips ``Decimal`` columns through strings, so the
    same amount surfaces as ``"130.00"`` on one side and ``"130.0"`` on the
    other; the old JSON hash treated that pure formatting difference as a state
    change and false-failed correct trials (OTS ``custom_refund_amount`` etc.).
    """

    def test_decimal_trailing_zeros_hash_equal(self):
        a = {"cases": [{"id": "C1", "refund": "130.00"}]}
        b = {"cases": [{"id": "C1", "refund": "130.0"}]}
        assert compute_stable_hash(a) == compute_stable_hash(b)

    def test_int_vs_decimal_string_hash_equal(self):
        a = {"cases": [{"id": "C1", "qty": 72}]}
        b = {"cases": [{"id": "C1", "qty": "72.00"}]}
        assert compute_stable_hash(a) == compute_stable_hash(b)

    def test_genuine_numeric_difference_hashes_differently(self):
        a = {"cases": [{"id": "C1", "refund": "790.00"}]}
        b = {"cases": [{"id": "C1", "refund": "0.0"}]}
        assert compute_stable_hash(a) != compute_stable_hash(b)

    def test_leading_zero_identifier_not_collapsed(self):
        a = {"cases": [{"code": "00123"}]}
        b = {"cases": [{"code": "123"}]}
        assert compute_stable_hash(a) != compute_stable_hash(b)

    def test_bool_not_collapsed_to_int(self):
        a = {"flags": [{"active": True}]}
        b = {"flags": [{"active": 1}]}
        assert compute_stable_hash(a) != compute_stable_hash(b)

    def test_opt_out_preserves_legacy_byte_exact_behavior(self):
        a = {"cases": [{"id": "C1", "refund": "130.00"}]}
        b = {"cases": [{"id": "C1", "refund": "130.0"}]}
        # Legacy mcp_core-exact behavior: without canonicalization the strings differ.
        assert compute_stable_hash(a, canonicalize_numbers=False) != compute_stable_hash(
            b, canonicalize_numbers=False
        )

    def test_tagged_string_does_not_collide_with_number(self):
        # A crafted string byte-equal to a numeric token must not equal the number.
        crafted = "\x00tf-num:130"
        assert canonical_number(crafted) != canonical_number(130)
        assert compute_stable_hash({"t": [{"amt": 130}]}) != compute_stable_hash(
            {"t": [{"amt": crafted}]}
        )

    def test_negative_zero_collapses_to_zero(self):
        assert canonical_number("-0.00") == canonical_number(0)
        assert compute_stable_hash({"t": [{"amt": "-0.00"}]}) == compute_stable_hash(
            {"t": [{"amt": "0.0"}]}
        )
