"""Canonical hash computation for stable state comparison.

This module provides a single, standardized hash function that matches
the implementation in mcp_core.utils.validation.calculate_database_hash().

All hash computations across the codebase should use compute_stable_hash()
to ensure consistent results.
"""

import hashlib
import json
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


def _convert_datetime_to_str(data: Any) -> Any:
    """
    Recursively convert datetime objects to ISO format strings for JSON serialization.

    Args:
        data: Data structure that may contain datetime objects

    Returns:
        Data structure with datetime objects converted to strings
    """
    if isinstance(data, datetime):
        return data.isoformat()
    elif isinstance(data, dict):
        return {key: _convert_datetime_to_str(value) for key, value in data.items()}
    elif isinstance(data, list):
        return [_convert_datetime_to_str(item) for item in data]
    elif isinstance(data, set):
        return sorted([_convert_datetime_to_str(item) for item in data])
    else:
        return data


def filter_unstable_fields(
    state: dict[str, Any],
    unstable_fields: list[str] | None = None,
) -> dict[str, Any]:
    """
    Filter out unstable fields from state dictionary.

    Unstable fields are auto-generated values like IDs, timestamps, etc.
    that should not be included in hash comparison.

    Args:
        state: State dictionary (can be nested)
        unstable_fields: List of field names to exclude (supports dot notation for nested fields)

    Returns:
        State dictionary with unstable fields removed
    """
    if not unstable_fields:
        return state

    # Build a set of top-level fields and nested field patterns
    top_level_fields: set[str] = set()
    nested_patterns: dict[str, list[str]] = {}  # table -> [fields]

    for field in unstable_fields:
        if "." in field:
            parts = field.split(".", 1)
            table = parts[0]
            nested_field = parts[1]
            if table not in nested_patterns:
                nested_patterns[table] = []
            nested_patterns[table].append(nested_field)
        else:
            top_level_fields.add(field)

    def filter_dict(d: dict[str, Any], parent_key: str = "") -> dict[str, Any]:
        result = {}
        for key, value in d.items():
            # Skip top-level unstable fields
            if key in top_level_fields:
                continue

            # Handle nested structures
            if isinstance(value, dict):
                # Check if this key has nested unstable fields
                if key in nested_patterns:
                    # Filter nested fields
                    filtered_value = {
                        k: v for k, v in value.items() if k not in nested_patterns[key]
                    }
                    result[key] = filter_dict(filtered_value, key)
                else:
                    result[key] = filter_dict(value, key)
            elif isinstance(value, list):
                # Handle list of dicts (common for database tables)
                if value and isinstance(value[0], dict):
                    if key in nested_patterns:
                        # Filter fields from each record
                        result[key] = [
                            {k: v for k, v in item.items() if k not in nested_patterns[key]}
                            for item in value
                        ]
                    else:
                        result[key] = value
                else:
                    result[key] = value
            else:
                result[key] = value

        return result

    return filter_dict(state)


def compute_stable_hash(
    state: dict[str, Any],
    unstable_fields: list[str] | None = None,
) -> str:
    """
    Compute a stable SHA-256 hash of the state dictionary.

    This function produces the same output as mcp_core.utils.validation.calculate_database_hash()
    for identical state dictionaries.

    Algorithm:
    1. Filter out unstable fields (if specified)
    2. Convert datetime objects to ISO format strings
    3. Serialize to JSON with sort_keys=True, separators=(",", ":"), default=str
    4. Compute SHA-256 hexdigest with UTF-8 encoding

    Args:
        state: State dictionary to hash
        unstable_fields: Optional list of field names to exclude from hash

    Returns:
        Hexadecimal string of the SHA-256 hash
    """
    logger.debug(
        "Computing stable hash",
        extra={
            "num_tables": len(state) if isinstance(state, dict) else 0,
            "unstable_fields_count": len(unstable_fields) if unstable_fields else 0,
        },
    )

    # Filter unstable fields if specified
    if unstable_fields:
        logger.debug("Filtering unstable fields: %s", unstable_fields)
        state = filter_unstable_fields(state, unstable_fields)

    # Convert datetime objects to strings
    serializable_state = _convert_datetime_to_str(state)

    # Serialize with canonical format matching mcp_core
    json_str = json.dumps(serializable_state, sort_keys=True, separators=(",", ":"), default=str)

    # Compute hash
    hash_result = hashlib.sha256(json_str.encode("utf-8")).hexdigest()

    logger.debug(
        "Hash computed",
        extra={
            "hash": hash_result[:16] + "...",  # Log first 16 chars for debugging
            "json_length": len(json_str),
        },
    )

    return hash_result


def compute_etag(data: dict[str, Any]) -> str:
    """
    Compute ETag for HTTP caching using the canonical hash algorithm.

    This is an alias for compute_stable_hash() for use in HTTP services.

    Args:
        data: Data dictionary to hash

    Returns:
        Hexadecimal string of the SHA-256 hash
    """
    return compute_stable_hash(data)
