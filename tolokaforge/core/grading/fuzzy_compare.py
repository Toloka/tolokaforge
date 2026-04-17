"""
Fuzzy state comparison for grading.

This module provides interfaces for comparing environment states
using stable field filtering. The key feature is filtering out
"unstable" fields (auto-generated IDs, timestamps, etc.) before
comparison, which is critical for tool-use style grading.

Stable state comparison uses mcp_core's UnstableField annotations
to identify fields that should be excluded from comparison.
Fields marked with UnstableField are typically:
- Auto-generated IDs (e.g., ticket IDs, request IDs)
- Timestamps (created_at, updated_at)
- LLM-generated content (subject, description)

The FuzzyStateComparator uses mcp_core's get_stable_database_state()
to filter these unstable fields, then hashes the remaining stable state.
"""

import hashlib
import json
import logging
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Add contrib paths for mcp_core imports
_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_MCP_CORE_PATH = _REPO_ROOT / "contrib" / "project-m-copilot-mock-tools" / "mcp_core" / "src"
if _MCP_CORE_PATH.exists() and str(_MCP_CORE_PATH) not in sys.path:
    sys.path.insert(0, str(_MCP_CORE_PATH))


@dataclass
class FieldDifference:
    """Represents a difference in a specific field."""

    field_path: str
    """Dot-notation path to the field (e.g., 'orders.0.status')."""

    expected_value: Any
    """The expected value from golden state."""

    actual_value: Any
    """The actual value from current state."""

    severity: str = "error"
    """Severity level: 'error', 'warning', or 'info'."""


@dataclass
class ComparisonResult:
    """Result of comparing two states."""

    is_match: bool
    """True if states match according to the comparison strategy."""

    score: float
    """Match score from 0.0 (no match) to 1.0 (perfect match)."""

    differences: list[FieldDifference] = field(default_factory=list)
    """List of field differences found."""

    compared_fields: set[str] = field(default_factory=set)
    """Set of field paths that were compared."""

    ignored_fields: set[str] = field(default_factory=set)
    """Set of field paths that were ignored."""

    details: dict[str, Any] = field(default_factory=dict)
    """Additional comparison details."""


class StateComparator(ABC):
    """
    Abstract interface for comparing environment states.

    Different implementations support different comparison strategies:
    - HashComparator: Full hash comparison (fastest, strictest)
    - FuzzyComparator: Field-level comparison with configurable matching
    """

    @abstractmethod
    def compare(
        self,
        expected_state: dict[str, Any],
        actual_state: dict[str, Any],
        relevant_fields: list[str] | None = None,
    ) -> ComparisonResult:
        """
        Compare two states.

        Args:
            expected_state: The expected/golden state.
            actual_state: The actual state to compare.
            relevant_fields: Optional list of field paths to compare.
                           If None, compare all fields.

        Returns:
            ComparisonResult with match status and details.
        """
        ...

    @abstractmethod
    def compute_hash(
        self,
        state: dict[str, Any],
        relevant_fields: list[str] | None = None,
    ) -> str:
        """
        Compute a hash of the state.

        Args:
            state: The state to hash.
            relevant_fields: Optional list of fields to include in hash.
                           If None, hash entire state.

        Returns:
            A hash string representing the state.
        """
        ...


class HashComparator(StateComparator):
    """
    State comparator using full hash comparison.

    This is the default comparison strategy - it computes a SHA-256 hash
    of the entire state and compares the hashes.
    """

    def compare(
        self,
        expected_state: dict[str, Any],
        actual_state: dict[str, Any],
        relevant_fields: list[str] | None = None,
    ) -> ComparisonResult:
        """
        Compare states by their hash values.

        Args:
            expected_state: The expected state.
            actual_state: The actual state.
            relevant_fields: Ignored in hash comparison.

        Returns:
            ComparisonResult with binary match (1.0 or 0.0 score).
        """
        expected_hash = self.compute_hash(expected_state, relevant_fields)
        actual_hash = self.compute_hash(actual_state, relevant_fields)

        is_match = expected_hash == actual_hash
        return ComparisonResult(
            is_match=is_match,
            score=1.0 if is_match else 0.0,
            differences=[],
            details={
                "expected_hash": expected_hash,
                "actual_hash": actual_hash,
                "comparison_type": "hash",
            },
        )

    def compute_hash(
        self,
        state: dict[str, Any],
        relevant_fields: list[str] | None = None,
    ) -> str:
        """
        Compute SHA-256 hash of the state.

        Args:
            state: The state to hash.
            relevant_fields: If provided, only hash these fields.

        Returns:
            Hex-encoded SHA-256 hash.
        """
        if relevant_fields:
            state_to_hash = self._extract_fields(state, relevant_fields)
        else:
            state_to_hash = state

        state_json = json.dumps(state_to_hash, sort_keys=True, default=str)
        return hashlib.sha256(state_json.encode()).hexdigest()

    def _extract_fields(self, state: dict[str, Any], field_paths: list[str]) -> dict[str, Any]:
        """Extract specific fields from state by dot-notation paths."""
        result = {}
        for path in field_paths:
            value = self._get_nested_value(state, path)
            if value is not None:
                result[path] = value
        return result

    def _get_nested_value(self, obj: Any, path: str) -> Any:
        """Get a nested value by dot-notation path."""
        parts = path.split(".")
        current = obj
        for part in parts:
            if current is None:
                return None
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list):
                try:
                    idx = int(part)
                    current = current[idx] if 0 <= idx < len(current) else None
                except ValueError:
                    return None
            else:
                return None
        return current


class FuzzyStateComparator(StateComparator):
    """
    State comparator using stable field filtering.

    This comparator filters out "unstable" fields before comparison,
    using mcp_core's get_stable_database_state() function. Unstable
    fields are identified by UnstableField annotations in Pydantic models.

    Typical unstable fields include:
    - Auto-generated IDs (ticket IDs, request IDs)
    - Timestamps (created_at, updated_at)
    - LLM-generated content (subject, description)

    This enables accurate grading even when agent generates different
    IDs or timestamps compared to the golden path.
    """

    def __init__(
        self,
        ignore_fields: list[str] | None = None,
        normalize_strings: bool = True,
        ignore_timestamps: bool = True,
        ignore_ids: bool = False,
    ):
        """
        Initialize the fuzzy comparator.

        Args:
            ignore_fields: List of field paths to ignore during comparison.
            normalize_strings: If True, normalize string values before comparison.
            ignore_timestamps: If True, ignore timestamp fields.
            ignore_ids: If True, ignore ID fields (useful for generated IDs).
        """
        self.ignore_fields = set(ignore_fields or [])
        self.normalize_strings = normalize_strings
        self.ignore_timestamps = ignore_timestamps
        self.ignore_ids = ignore_ids

        self._hash_comparator = HashComparator()

    def compare(
        self,
        expected_state: dict[str, Any],
        actual_state: dict[str, Any],
        relevant_fields: list[str] | None = None,
    ) -> ComparisonResult:
        """
        Compare states using stable field filtering.

        Both states should already be filtered by get_stable_database_state()
        or passed through get_stable_state() before calling this method.

        Args:
            expected_state: The expected stable state.
            actual_state: The actual stable state.
            relevant_fields: List of field paths to compare.

        Returns:
            ComparisonResult with match status.
        """
        result = self._hash_comparator.compare(expected_state, actual_state, relevant_fields)
        result.details["comparison_type"] = "stable_state"
        return result

    def compute_hash(
        self,
        state: dict[str, Any],
        relevant_fields: list[str] | None = None,
    ) -> str:
        """
        Compute hash of state.

        Note: The state should already be filtered by get_stable_database_state()
        before calling this method.

        Args:
            state: The stable state to hash.
            relevant_fields: Fields to include in hash.

        Returns:
            Hash of the state.
        """
        return self._hash_comparator.compute_hash(state, relevant_fields)


def get_stable_state(db: Any) -> dict[str, Any]:
    """
    Get stable state from an InMemoryDatabase instance.

    Uses mcp_core's get_stable_database_state() to filter out
    fields marked with UnstableField annotation.

    Args:
        db: InMemoryDatabase instance from mcp_core.

    Returns:
        Stable state dictionary (with unstable fields filtered out).

    Raises:
        ImportError: If mcp_core is not available.
    """
    try:
        from mcp_core.utils.validation import get_stable_database_state
    except ImportError as e:
        raise ImportError(
            "mcp_core is required for stable state comparison. "
            "Ensure contrib/mcp_core is available and mcp_core is installed."
        ) from e

    return get_stable_database_state(db)


def calculate_stable_hash(db: Any) -> str:
    """
    Calculate stable hash of an InMemoryDatabase.

    Uses mcp_core's calculate_database_hash() with exclude_unstable_fields=True.

    Args:
        db: InMemoryDatabase instance from mcp_core.

    Returns:
        SHA-256 hash of stable state.

    Raises:
        ImportError: If mcp_core is not available.
    """
    try:
        from mcp_core.utils.validation import calculate_database_hash
    except ImportError as e:
        raise ImportError(
            "mcp_core is required for stable hash calculation. "
            "Ensure contrib/mcp_core is available and mcp_core is installed."
        ) from e

    return calculate_database_hash(db, exclude_unstable_fields=True)


def create_comparator(
    comparison_type: str = "hash",
    **kwargs: Any,
) -> StateComparator:
    """
    Factory function to create a state comparator.

    Args:
        comparison_type: Type of comparator ("hash" or "fuzzy").
        **kwargs: Additional arguments passed to the comparator.

    Returns:
        A StateComparator instance.
    """
    if comparison_type == "hash":
        return HashComparator()
    elif comparison_type == "fuzzy":
        return FuzzyStateComparator(**kwargs)
    else:
        raise ValueError(f"Unknown comparison type: {comparison_type}")
