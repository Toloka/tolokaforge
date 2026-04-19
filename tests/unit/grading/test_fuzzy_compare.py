"""Unit tests for fuzzy/stable state comparison using synthetic mcp_core models.

Tests verify that:
- get_stable_database_state correctly filters UnstableField-annotated fields
- calculate_database_hash is deterministic and only changes with stable data
- UnstableField.extract_names works for annotated vs un-annotated models
"""

import pytest

pytestmark = pytest.mark.unit

# Trigger mcp_core sys.path setup via tolokaforge module (which adds contrib to path)
import tolokaforge.core.grading.fuzzy_compare  # noqa: F401

mcp_core = pytest.importorskip("mcp_core", reason="mcp_core runtime not available")

from mcp_core.db.unstable_field import UnstableField
from mcp_core.utils.validation import calculate_database_hash, get_stable_database_state

from tests.data.tlk_mcp_core.db_helpers import make_test_db
from tests.data.tlk_mcp_core.models import SyntheticOrder, SyntheticTicket, SyntheticUser

# ---------------------------------------------------------------------------
# Test 4: get_stable_database_state filters UnstableField-annotated fields
# ---------------------------------------------------------------------------


class TestGetStableStateFiltering:
    """Verify that get_stable_database_state removes UnstableField-annotated fields."""

    def test_ticket_unstable_fields_removed(self):
        """SyntheticTicket.subject and .description must be absent in stable state."""
        db = make_test_db(
            tickets=[
                {
                    "id": "1",
                    "subject": "Should be stripped",
                    "description": "Also stripped",
                    "status": "open",
                    "priority": "high",
                    "requester_id": "100",
                }
            ]
        )

        stable = get_stable_database_state(db)
        ticket = stable["test_tickets"][0]

        # Unstable fields must be absent
        assert "subject" not in ticket
        assert "description" not in ticket

        # Stable fields must be present
        assert ticket["status"] == "open"
        assert ticket["priority"] == "high"
        assert ticket["requester_id"] == "100"

    def test_user_timestamp_fields_removed(self):
        """SyntheticUser.created_at and .updated_at must be absent in stable state."""
        db = make_test_db(
            users=[
                {
                    "id": "2",
                    "name": "Bob",
                    "email": "bob@test.com",
                    "created_at": "2025-01-01T00:00:00Z",
                    "updated_at": "2025-01-01T00:00:00Z",
                }
            ]
        )

        stable = get_stable_database_state(db)
        user = stable["test_users"][0]

        # Timestamp unstable fields must be absent
        assert "created_at" not in user
        assert "updated_at" not in user

        # Stable fields must be present
        assert user["name"] == "Bob"
        assert user["email"] == "bob@test.com"

    def test_order_all_fields_kept(self):
        """SyntheticOrder has no unstable fields — all fields must survive."""
        db = make_test_db(orders=[{"id": "3", "status": "pending", "total": 42.0}])

        stable = get_stable_database_state(db)
        order = stable["test_orders"][0]

        assert order == {"id": "3", "status": "pending", "total": 42.0}


# ---------------------------------------------------------------------------
# Test 5: calculate_database_hash determinism and sensitivity
# ---------------------------------------------------------------------------


class TestCalculateDatabaseHash:
    """Verify hash determinism and sensitivity to stable vs unstable changes."""

    def test_same_state_produces_same_hash(self):
        """Two databases with identical data must produce the same hash."""
        kwargs = {
            "tickets": [
                {
                    "id": "1",
                    "subject": "Same subject",
                    "description": "Same desc",
                    "status": "open",
                    "priority": "high",
                }
            ]
        }
        db1 = make_test_db(**kwargs)
        db2 = make_test_db(**kwargs)

        assert calculate_database_hash(db1) == calculate_database_hash(db2)

    def test_stable_field_change_alters_hash(self):
        """Changing a stable field (status) must change the hash."""
        db = make_test_db(tickets=[{"id": "1", "status": "open", "priority": "normal"}])
        hash_before = calculate_database_hash(db)

        # Mutate a stable field
        ticket = db.get_by_id(SyntheticTicket, "1")
        ticket.status = "closed"
        db.update(ticket)
        hash_after = calculate_database_hash(db)

        assert hash_before != hash_after

    def test_unstable_field_change_preserves_hash(self):
        """Changing an unstable field (subject) must NOT change the hash."""
        db = make_test_db(
            tickets=[
                {
                    "id": "1",
                    "subject": "Original",
                    "description": "Original desc",
                    "status": "open",
                }
            ]
        )
        hash_before = calculate_database_hash(db)

        # Mutate only unstable fields
        ticket = db.get_by_id(SyntheticTicket, "1")
        ticket.subject = "Completely different subject"
        ticket.description = "Completely different description"
        db.update(ticket)
        hash_after = calculate_database_hash(db)

        assert hash_before == hash_after, (
            "Hash changed when only unstable fields (subject, description) were modified"
        )


# ---------------------------------------------------------------------------
# Test 6: UnstableField.extract_names works for synthetic models
# ---------------------------------------------------------------------------


class TestUnstableFieldExtractNames:
    """Verify UnstableField.extract_names for synthetic domain models."""

    def test_ticket_unstable_fields(self):
        """SyntheticTicket must have subject and description as unstable."""
        names = UnstableField.extract_names(SyntheticTicket)
        assert "subject" in names
        assert "description" in names

    def test_user_unstable_fields(self):
        """SyntheticUser must have created_at and updated_at as unstable."""
        names = UnstableField.extract_names(SyntheticUser)
        assert "created_at" in names
        assert "updated_at" in names

    def test_order_has_no_unstable_fields(self):
        """SyntheticOrder should have no unstable fields."""
        names = UnstableField.extract_names(SyntheticOrder)
        assert names == [], f"Expected no unstable fields, got {names}"


# ---------------------------------------------------------------------------
# Tests: get_stable_state / calculate_stable_hash error handling
# ---------------------------------------------------------------------------

from unittest.mock import patch


class TestGetStableStateErrorHandling:
    """Verify get_stable_state raises ImportError when mcp_core is absent."""

    def test_get_stable_state_raises_on_missing_mcp_core(self):
        """get_stable_state() must raise ImportError with helpful message."""
        from tolokaforge.core.grading.fuzzy_compare import get_stable_state

        with patch.dict(
            "sys.modules",
            {
                "mcp_core": None,
                "mcp_core.utils": None,
                "mcp_core.utils.validation": None,
            },
        ):
            with pytest.raises(ImportError, match="mcp_core is required"):
                get_stable_state(object())

    def test_calculate_stable_hash_raises_on_missing_mcp_core(self):
        """calculate_stable_hash() must raise ImportError with helpful message."""
        from tolokaforge.core.grading.fuzzy_compare import calculate_stable_hash

        with patch.dict(
            "sys.modules",
            {
                "mcp_core": None,
                "mcp_core.utils": None,
                "mcp_core.utils.validation": None,
            },
        ):
            with pytest.raises(ImportError, match="mcp_core is required"):
                calculate_stable_hash(object())


# ---------------------------------------------------------------------------
# Tests: HashComparator
# ---------------------------------------------------------------------------

from tolokaforge.core.grading.fuzzy_compare import HashComparator


class TestHashComparator:
    """Unit tests for HashComparator pure-logic methods."""

    def test_hash_comparator_identical_states(self):
        """Same state → is_match=True, score=1.0."""
        comparator = HashComparator()
        state = {"orders": [{"id": "1", "status": "shipped"}]}

        result = comparator.compare(state, state)

        assert result.is_match is True
        assert result.score == 1.0

    def test_hash_comparator_different_states(self):
        """Different states → is_match=False, score=0.0."""
        comparator = HashComparator()
        expected = {"orders": [{"id": "1", "status": "shipped"}]}
        actual = {"orders": [{"id": "1", "status": "cancelled"}]}

        result = comparator.compare(expected, actual)

        assert result.is_match is False
        assert result.score == 0.0

    def test_hash_comparator_with_relevant_fields(self):
        """Only matching relevant_fields are compared."""
        comparator = HashComparator()
        # States differ in 'orders' but share 'users'
        expected = {
            "orders": [{"id": "1", "status": "shipped"}],
            "users": [{"id": "10", "name": "Alice"}],
        }
        actual = {
            "orders": [{"id": "1", "status": "cancelled"}],
            "users": [{"id": "10", "name": "Alice"}],
        }

        # Comparing only users → should match
        result = comparator.compare(expected, actual, relevant_fields=["users"])
        assert result.is_match is True
        assert result.score == 1.0

        # Comparing orders → should NOT match
        result = comparator.compare(expected, actual, relevant_fields=["orders"])
        assert result.is_match is False

    def test_hash_comparator_nested_extraction(self):
        """_get_nested_value traverses nested dicts and lists."""
        comparator = HashComparator()
        state = {
            "orders": [
                {"id": "1", "items": [{"sku": "A"}, {"sku": "B"}]},
            ],
        }

        assert comparator._get_nested_value(state, "orders.0.id") == "1"
        assert comparator._get_nested_value(state, "orders.0.items.1.sku") == "B"

    def test_hash_comparator_nested_value_missing(self):
        """_get_nested_value returns None for missing paths."""
        comparator = HashComparator()
        state = {"orders": [{"id": "1"}]}

        assert comparator._get_nested_value(state, "orders.0.missing_field") is None
        assert comparator._get_nested_value(state, "nonexistent.path") is None
        assert comparator._get_nested_value(state, "orders.99") is None


# ---------------------------------------------------------------------------
# Tests: FuzzyStateComparator
# ---------------------------------------------------------------------------

from tolokaforge.core.grading.fuzzy_compare import FuzzyStateComparator


class TestFuzzyStateComparator:
    """Unit tests for FuzzyStateComparator."""

    def test_fuzzy_comparator_delegates_to_hash(self):
        """FuzzyStateComparator wraps HashComparator and adds comparison_type."""
        comparator = FuzzyStateComparator()
        state = {"orders": [{"id": "1", "status": "done"}]}

        result = comparator.compare(state, state)

        assert result.is_match is True
        assert result.score == 1.0
        assert result.details.get("comparison_type") == "stable_state"

    def test_fuzzy_comparator_constructor_params(self):
        """Verify constructor stores ignore_fields, normalize_strings, etc."""
        comparator = FuzzyStateComparator(
            ignore_fields=["orders.timestamp"],
            normalize_strings=False,
            ignore_timestamps=False,
            ignore_ids=True,
        )

        assert "orders.timestamp" in comparator.ignore_fields
        assert comparator.normalize_strings is False
        assert comparator.ignore_timestamps is False
        assert comparator.ignore_ids is True


# ---------------------------------------------------------------------------
# Test: sys.path isolation — module loads even without contrib
# ---------------------------------------------------------------------------


class TestSysPathIsolation:
    """Verify fuzzy_compare module loads without error when contrib is absent."""

    def test_module_level_sys_path_does_not_error_when_contrib_missing(self):
        """The module is already imported at test time — confirm it loaded."""
        import tolokaforge.core.grading.fuzzy_compare as mod

        assert hasattr(mod, "HashComparator")
        assert hasattr(mod, "FuzzyStateComparator")
        assert hasattr(mod, "get_stable_state")
        assert hasattr(mod, "calculate_stable_hash")
