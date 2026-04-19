"""Unit tests for tolokaforge.core.hash module.

Tests verify:
- filter_unstable_fields handles nested table.field patterns
- compute_stable_hash matches mcp_core's calculate_database_hash for same stable state
"""

import pytest

pytestmark = pytest.mark.unit

from tolokaforge.core.hash import compute_stable_hash, filter_unstable_fields

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
# Test 8: compute_stable_hash matches mcp_core's calculate_database_hash
# ---------------------------------------------------------------------------


class TestComputeStableHashCrossImplementation:
    """Verify that compute_stable_hash produces the same result as mcp_core."""

    @pytest.fixture(autouse=True)
    def _require_mcp_core(self):
        # Trigger mcp_core sys.path setup via tolokaforge module
        import tolokaforge.core.grading.fuzzy_compare  # noqa: F401

        pytest.importorskip("mcp_core", reason="mcp_core runtime not available")

    def test_hash_matches_mcp_core(self):
        """compute_stable_hash(stable_state) == calculate_database_hash(db).

        Flow:
        1. Build InMemoryDatabase with synthetic models
        2. get_stable_database_state(db) → filtered state dict
        3. calculate_database_hash(db) → mcp_core hash
        4. compute_stable_hash(filtered_state) → tolokaforge hash
        5. Assert equal
        """
        from mcp_core.utils.validation import (
            calculate_database_hash,
            get_stable_database_state,
        )

        from tests.data.tlk_mcp_core.db_helpers import make_test_db

        db = make_test_db(
            tickets=[
                {
                    "id": "1",
                    "subject": "Cross-hash test",
                    "description": "Verifying hash consistency",
                    "status": "open",
                    "priority": "high",
                }
            ],
            users=[
                {
                    "id": "2",
                    "name": "Alice",
                    "email": "alice@test.com",
                    "created_at": "2025-06-01",
                    "updated_at": "2025-06-01",
                }
            ],
        )

        mcp_hash = calculate_database_hash(db)
        stable_state = get_stable_database_state(db)
        tf_hash = compute_stable_hash(stable_state)

        assert mcp_hash == tf_hash, (
            f"Hash mismatch: mcp_core={mcp_hash[:16]}... vs tolokaforge={tf_hash[:16]}..."
        )

    def test_hash_matches_after_mutation(self):
        """Cross-implementation hash still matches after DB mutation."""
        from mcp_core.utils.validation import (
            calculate_database_hash,
            get_stable_database_state,
        )

        from tests.data.tlk_mcp_core.db_helpers import make_test_db
        from tests.data.tlk_mcp_core.models import SyntheticTicket

        db = make_test_db(tickets=[{"id": "1", "status": "open", "priority": "normal"}])

        # Mutate stable field
        ticket = db.get_by_id(SyntheticTicket, "1")
        ticket.priority = "urgent"
        db.update(ticket)

        mcp_hash = calculate_database_hash(db)
        stable_state = get_stable_database_state(db)
        tf_hash = compute_stable_hash(stable_state)

        assert mcp_hash == tf_hash


# ---------------------------------------------------------------------------
# Tests: compute_stable_hash standalone behavior
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
