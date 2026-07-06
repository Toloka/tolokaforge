"""Unit tests for fuzzy/stable state comparison — tolokaforge-internal surface only.

These tests cover the public-facing tolokaforge code paths:

- ``HashComparator`` (pure-dict comparison + nested path extraction)
- ``FuzzyStateComparator`` (wrapper API and constructor config)
- ``get_stable_state`` / ``calculate_stable_hash`` ImportError guards when
  ``mcp_core`` is not installed
- Module-level sys.path injection does not crash when ``contrib/`` is absent

The companion tests that exercise ``mcp_core`` library behaviour directly
(``get_stable_database_state``, ``calculate_database_hash``,
``UnstableField.extract_names``) live in the adapter package's test
suite, because they require ``mcp_core`` to be importable.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

# Import-time side effect: this module adds ``contrib/<...>/mcp_core/src``
# to ``sys.path`` if available. We import it eagerly so that
# ``TestSysPathIsolation`` can verify the module loads cleanly.
import tolokaforge.core.grading.fuzzy_compare  # noqa: F401
from tolokaforge.core.grading.fuzzy_compare import (
    FuzzyStateComparator,
    HashComparator,
    calculate_stable_hash,
    get_stable_state,
)

# ---------------------------------------------------------------------------
# get_stable_state / calculate_stable_hash: ImportError guards
# ---------------------------------------------------------------------------


class TestGetStableStateErrorHandling:
    """Verify the public helpers raise a helpful ImportError when mcp_core is absent."""

    def test_get_stable_state_raises_on_missing_mcp_core(self):
        """``get_stable_state()`` must raise ImportError with a helpful message."""
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
        """``calculate_stable_hash()`` must raise ImportError with a helpful message."""
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
# HashComparator
# ---------------------------------------------------------------------------


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
# FuzzyStateComparator
# ---------------------------------------------------------------------------


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
# sys.path isolation — module loads even without contrib
# ---------------------------------------------------------------------------


class TestSysPathIsolation:
    """Verify fuzzy_compare module loads without error when contrib is absent.

    NOTE: this test exists because the module currently performs a
    ``sys.path.insert`` of ``contrib/<...>/mcp_core/src`` at import time
    so that ``mcp_core`` becomes importable when present. A follow-up
    cleanup removes that ``sys.path`` hack entirely; when that happens,
    this test becomes obsolete and should be deleted.
    """

    def test_module_level_sys_path_does_not_error_when_contrib_missing(self):
        """The module is already imported at test time — confirm it loaded."""
        import tolokaforge.core.grading.fuzzy_compare as mod

        assert hasattr(mod, "HashComparator")
        assert hasattr(mod, "FuzzyStateComparator")
        assert hasattr(mod, "get_stable_state")
        assert hasattr(mod, "calculate_stable_hash")
