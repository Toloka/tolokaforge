"""Canonical hash computation for stable state comparison.

This module provides a single, standardized hash function. With
``canonicalize_numbers=False`` it matches
mcp_core.utils.validation.calculate_database_hash(); the default (True)
additionally folds numerically-equal NUMERIC-TYPE values (72 == 72.0) together,
so it intentionally diverges from that byte-for-byte output. Numeric-looking
STRINGS ("130.00" == "130.0") fold only with the opt-in
``normalize_numeric_strings`` flag — see :func:`canonical_number`.

All hash computations across the codebase should use compute_stable_hash()
to ensure consistent results.
"""

import hashlib
import json
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

logger = logging.getLogger(__name__)

# Cap the work spent deciding whether a string is a number: no real amount,
# quantity, or id is this long, and it bounds pathological inputs.
_MAX_NUMERIC_LEN = 64
_NUMERIC_TAG = "\x00tf-num:"
# Escape prefix for a genuine string that itself begins with the reserved NUL,
# so a crafted value like "\x00tf-num:130" can never collide with a numeric token.
_ESCAPE_TAG = "\x00tf-esc:"


def _numeric_token(d: Decimal) -> str:
    """Canonical token for a Decimal: trailing zeros stripped, -0 folded to 0."""
    d = d.normalize()
    if d.is_zero():  # collapse "-0" and "0"
        d = abs(d)
    return _NUMERIC_TAG + format(d, "f")


def _looks_like_plain_decimal(s: str) -> bool:
    """Whether ``s`` is a plain decimal / integer literal we should canonicalize.

    Linear-time and regex-free (so it cannot backtrack). Accepts ``"130"``,
    ``"130.00"``, ``"-5.50"``, ``"0"``, ``"0.0"``, ``".5"``; rejects leading-zero
    integer parts (``"00123"``, ``"007"`` — they smell like string identifiers),
    scientific notation (``"1e3"``), and anything non-ASCII or non-numeric.
    """
    body = s[1:] if s[:1] in ("+", "-") else s
    int_part, dot, frac_part = body.partition(".")
    if dot:  # exactly one '.'; the fractional side must be >= 1 ASCII digit
        if not (frac_part.isascii() and frac_part.isdigit()):
            return False
        if int_part and not (int_part.isascii() and int_part.isdigit()):
            return False
    elif not (int_part.isascii() and int_part.isdigit()):
        return False
    # Reject leading-zero integer parts; allow a lone "0" and "0.x".
    return not (len(int_part) > 1 and int_part[0] == "0")


def canonical_number(value: Any, *, normalize_strings: bool = False) -> Any:
    """Collapse numerically-equal representations of a value to one token.

    State grading compares field values for equality — via this module's
    :func:`compute_stable_hash` and via
    :func:`tolokaforge.core.grading.state_checks.to_hashable`. The same amount
    can surface as ``72`` on one side and ``72.0`` (or ``Decimal("72.00")``) on
    the other; a naive string/JSON comparison then treats a pure representation
    difference as a state change — a grading false-fail.

    Two tiers of folding:

    * **Numeric types** (``int`` / ``float`` / ``Decimal``) — always folded to
      one token. The type itself declares the value is a number, so this is a
      safe, generic improvement (``72 == 72.0 == Decimal("72.00")``).
    * **Numeric-looking strings** (``"130.00"`` vs ``"130.0"``) — folded ONLY
      when ``normalize_strings=True``. This is deliberately opt-in and
      DANGEROUS as a default: a string that merely looks numeric may carry
      meaning in its exact representation (version numbers like ``"1.10"`` vs
      ``"1.1"``, codes, zero-padded ids), and folding those would false-PASS a
      genuinely wrong state. Enable it per task via the grading config
      (``state_checks.numeric_string_normalization``) for domains whose string
      fields are genuinely numeric (e.g. DB-round-tripped Decimal columns).

    Correctness guards in both tiers:

    * ``bool`` is left untouched (``True == 1`` in Python, undesirable here);
    * identifier-like strings with leading zeros (``"00123"``) are left untouched
      so two distinct ids are never numerically equated;
    * genuinely different numbers (``"790.00"`` vs ``"0.0"``) stay different;
    * a genuine string that itself begins with the reserved NUL prefix is escaped
      so it cannot masquerade as a numeric token;
    * non-numeric values pass through unchanged.

    Leniency note (string tier): surrounding whitespace and a leading ``+`` are
    ignored, and a numeric string collapses with its bare-number twin
    (``"123"`` == ``123`` == ``"123.0"``); numeric-string ids are protected only
    by the leading-zero rule.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        try:
            return _numeric_token(Decimal(str(value)))
        except (InvalidOperation, ValueError):
            return value
    if isinstance(value, str):
        if normalize_strings:
            s = value.strip()
            if 0 < len(s) <= _MAX_NUMERIC_LEN and _looks_like_plain_decimal(s):
                try:
                    return _numeric_token(Decimal(s))
                except InvalidOperation:
                    pass
        # A genuine string beginning with the reserved NUL would otherwise be
        # byte-identical to a numeric token; escape it so the two can't collide.
        if value.startswith("\x00"):
            return _ESCAPE_TAG + value
    return value


def _canonicalize_numbers(data: Any, *, normalize_strings: bool = False) -> Any:
    """Recursively apply :func:`canonical_number` to every scalar in a state."""
    if isinstance(data, dict):
        return {
            key: _canonicalize_numbers(value, normalize_strings=normalize_strings)
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [_canonicalize_numbers(item, normalize_strings=normalize_strings) for item in data]
    if isinstance(data, tuple):
        return tuple(
            _canonicalize_numbers(item, normalize_strings=normalize_strings) for item in data
        )
    return canonical_number(data, normalize_strings=normalize_strings)


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
    *,
    canonicalize_numbers: bool = True,
    normalize_numeric_strings: bool = False,
) -> str:
    """
    Compute a stable SHA-256 hash of the state dictionary.

    With ``canonicalize_numbers=False`` this produces the same output as
    mcp_core.utils.validation.calculate_database_hash() for identical state
    dictionaries. The default (True) additionally folds numerically-equal
    representations, intentionally diverging from that byte-for-byte output.

    Algorithm:
    1. Filter out unstable fields (if specified)
    2. Convert datetime objects to ISO format strings
    3. Serialize to JSON with sort_keys=True, separators=(",", ":"), default=str
    4. Compute SHA-256 hexdigest with UTF-8 encoding

    Args:
        state: State dictionary to hash
        unstable_fields: Optional list of field names to exclude from hash
        canonicalize_numbers: When True (default), collapse numerically-equal
            NUMERIC-TYPE values (72 == 72.0 == Decimal("72.00")) before hashing.
            Generic and safe — the type declares the value is a number. Pass
            False to reproduce the legacy byte-for-byte
            mcp_core.calculate_database_hash() output.
        normalize_numeric_strings: When True, ALSO collapse numeric-looking
            STRINGS ("130.00" == "130.0" == "130"). Default False — this is an
            opt-in, per-task feature (grading config
            ``state_checks.numeric_string_normalization``), NOT a generic
            improvement: exact string representation can be meaningful
            (versions "1.10" vs "1.1", codes), and folding those would
            false-pass a wrong state. Enable only for domains whose string
            fields are DB-round-tripped decimals. Ignored when
            canonicalize_numbers is False.

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

    # Collapse numerically-equal representations so a pure representation
    # difference is not graded as a state change: numeric TYPES always
    # (72 == 72.0), numeric-looking STRINGS only when the per-task flag opts in
    # ("130.00" == "130.0" — see the normalize_numeric_strings docstring).
    if canonicalize_numbers:
        serializable_state = _canonicalize_numbers(
            serializable_state, normalize_strings=normalize_numeric_strings
        )

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
