"""Canonical hash computation for stable state comparison.

This module provides a single, standardized hash function. With
``canonicalize_numbers=False`` it matches
mcp_core.utils.validation.calculate_database_hash(); the default (True)
additionally folds numerically-equal NUMERIC-TYPE values (72 == 72.0) together,
so it intentionally diverges from that byte-for-byte output. Numeric-looking
STRINGS ("130.00" == "130.0") fold only for the per-field opt-in set
``numeric_string_fields`` — see :func:`compute_stable_hash`.

:func:`compute_stable_hash` is the RUNNER substrate's digest: db-service state
hashes, ETags (:func:`compute_etag`), snapshot hashes, and the
``ResetTrialResponse.state_hash`` / ``GetStateResponse.stable_hash`` wire
fields. Core grading's digest is a different algebra —
``state_digest`` (``consistent_hash(to_hashable(...))``) in
``tolokaforge/core/grading/state_checks.py``. The two agree on which states are
equal (both fold through :func:`canonical_number`) and disagree on every label,
so a hash comparison hashes both sides with one function, on one substrate,
and a digest never crosses substrates. The two algebras stay separate
deliberately: this module's digests are persisted and core's reproduce the
digests recorded bundles carry, so changing either function invalidates
digests that already exist — while nothing needs a digest to travel between
substrates. Locked by
``tests/canonical/test_expected_state_hash_is_not_portable.py``.
"""

import hashlib
import json
import logging
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)


class ColumnCompareRule(BaseModel):
    """Per-(table, column) rule for the state-hash comparator.

    Two independent families of loosening the pack can opt into per column:

    **Structural** (``mode: subset``): the model may include keys in ``column``
    beyond what the golden's version of ``column`` carries — for exactly the keys
    named in ``extras_allowed_for`` — without failing the hash. Extras not named
    there still fail; keys the golden declares are still compared
    value-for-value. Mirrors the trace comparator's ``compare_args`` shape on
    ``RequiredAction``.

    **Collection order** (``order: unordered``): the enclosing table's row
    list is treated as a set — the comparator sorts rows on both sides by
    canonical JSON before hashing so a pure row-permutation stops failing
    the hash. Set on any one column-rule of the table; the ordering effect
    is table-wide, since row order is the property of the row list, not of
    a single column. Default ``ordered`` keeps positional-list semantics —
    the same shape every unmigrated column has today.

    **Scalar equivalences** (``treat_null_as_empty_collection``,
    ``normalize_timezone_suffix``, ``treat_empty_string_as_null``): the
    column's scalar value is canonicalized to a single form before hashing on
    both sides. Each flag opts in one equivalence relation:

    - ``treat_null_as_empty_collection`` — ``None`` ≡ ``[]`` ≡ ``{}``. For
      collection columns the domain treats "no items" and "field absent" as
      the same state.
    - ``normalize_timezone_suffix`` — strings ending in ``Z``, ``+00:00``, or
      ``+0000`` compare equal to the same string without that trailing UTC
      marker. For datetime columns where the prompt does not require an
      explicit ``Z`` and the storage layer round-trips it inconsistently.
    - ``treat_empty_string_as_null`` — ``None`` ≡ ``""``. For nullable-string
      columns whose prompt says "leave empty" and DB storage folds one form
      into the other.

    When more than one flag is set on the same column the folds compose to a
    single "nullish" bucket, so ``None``, ``[]``, ``{}`` and ``""`` all
    compare equal.

    Room for future modes (case-insensitive, numeric-tolerant) is why this is
    a typed struct rather than a bare ``list``.

    Declaring ``extras_allowed_for`` requires ``mode: subset``; a rule with a
    non-empty ``extras_allowed_for`` and no ``mode`` is refused at load time
    rather than silently ignored — the extras filter only runs under subset
    mode, and a filter that authors expected to fire is worse than a load-time
    error naming the missing declaration.

    Declared as a per-table, per-column map in ``state_checks.compare_columns``:

    .. code-block:: yaml

        state_checks:
          compare_columns:
            send_notification_notifications:
              notification_id:
                order: unordered
              params:
                mode: subset
                extras_allowed_for: [param_case_number]
            d365_cases:
              custom_tags:
                treat_null_as_empty_collection: true
            sap_api_orders:
              approx_delivery_date:
                normalize_timezone_suffix: true
            d365_api_cases:
              custom_corporate_account_id:
                treat_empty_string_as_null: true
    """

    model_config = {"extra": "forbid"}

    mode: Literal["subset"] | None = None
    extras_allowed_for: list[str] = Field(default_factory=list)
    order: Literal["ordered", "unordered"] = "ordered"
    treat_null_as_empty_collection: bool = False
    normalize_timezone_suffix: bool = False
    treat_empty_string_as_null: bool = False

    @model_validator(mode="after")
    def _extras_require_subset_mode(self) -> "ColumnCompareRule":
        if self.extras_allowed_for and self.mode is None:
            raise ValueError(
                "extras_allowed_for is only consulted under mode: subset — declare "
                "mode: subset alongside, or remove extras_allowed_for."
            )
        return self


def apply_compare_columns_extras(
    actual: dict[str, Any],
    expected: dict[str, Any],
    compare_columns: dict[str, dict[str, ColumnCompareRule]] | None,
) -> dict[str, Any]:
    """Return ``actual`` with permitted-extra keys removed from the declared column dicts.

    For each ``(table, column)`` in ``compare_columns`` with ``mode: subset``, walks
    ``actual`` and ``expected`` in lock-step and, for every record where the actual
    column value is a dict, drops any key in ``extras_allowed_for`` that the model
    added but the golden did not carry. Keys the golden did declare are left in place
    (so a value mismatch on a declared key still fails the hash). Keys outside the
    allowlist are left in place (extras not permitted by the pack still fail).

    Row pairing is strictly positional (``actual[i]`` against ``expected[i]``): the
    state hash itself is order-sensitive on list columns, so a row-order mismatch
    fails the hash regardless of what this filter does. The one visible consequence
    of positional pairing is that the ``StateDiff`` reported to the author after a
    mismatch reflects the pairing that was hashed, not a semantic id-match.

    Table cases with no filter to apply:

    - The rule only makes sense for dict-valued columns like tool-call ``params``.
      A non-dict actual column value is left untouched.
    - When the golden's row does not carry the column at all (missing or non-dict),
      the containing hash mismatch already covers the schema difference. The rule
      leaves the actual column unchanged: a golden with no ``params`` at all and an
      actual with ``params: {…}`` is a schema difference the state hash fails on.
    - Tables absent from ``actual`` or ``expected``, or with mismatched container
      shapes (list vs dict), are skipped without error for the same reason.

    Symmetric-drop escape hatches (:func:`filter_unstable_fields`) remain the right
    tool for a column the pack wants to ignore entirely; this one is for keys the
    prompt permits the model to add.

    The returned dict is a shallow copy of ``actual`` — tables absent from
    ``compare_columns`` share list / dict references with the input rather than
    being deep-copied. Callers that hash the result (``compute_stable_hash``)
    and discard it — the only supported use — are unaffected; callers that
    mutate the returned dict must not touch untouched tables in place.
    """
    if not compare_columns:
        return actual

    def _filter_row_pair(
        actual_row: Any,
        expected_row: Any,
        rules_with_allowed: list[tuple[str, frozenset[str]]],
    ) -> Any:
        if not isinstance(actual_row, dict) or not isinstance(expected_row, dict):
            return actual_row
        filtered = dict(actual_row)
        for column, allowed in rules_with_allowed:
            actual_col = filtered.get(column)
            expected_col = expected_row.get(column)
            if not isinstance(actual_col, dict) or not isinstance(expected_col, dict):
                continue
            filtered[column] = {
                k: v for k, v in actual_col.items() if not (k in allowed and k not in expected_col)
            }
        return filtered

    result = dict(actual)
    for table, column_rules in compare_columns.items():
        if not column_rules:
            continue
        # Materialize allowlist once per rule as a frozenset — O(1) membership
        # inside the inner comprehension, regardless of extras_allowed_for length.
        rules_with_allowed: list[tuple[str, frozenset[str]]] = [
            (column, frozenset(rule.extras_allowed_for))
            for column, rule in column_rules.items()
            if rule.mode == "subset"
        ]
        if not rules_with_allowed:
            continue
        actual_table = result.get(table)
        expected_table = expected.get(table)
        if actual_table is None or expected_table is None:
            continue
        if isinstance(actual_table, list) and isinstance(expected_table, list):
            paired: list[Any] = []
            for i, actual_row in enumerate(actual_table):
                expected_row = expected_table[i] if i < len(expected_table) else {}
                paired.append(_filter_row_pair(actual_row, expected_row, rules_with_allowed))
            result[table] = paired
        elif isinstance(actual_table, dict) and isinstance(expected_table, dict):
            result[table] = _filter_row_pair(actual_table, expected_table, rules_with_allowed)
    return result


# Cap the work spent deciding whether a string is a number: no real amount,
# quantity, or id is this long, and it bounds pathological inputs.
_MAX_NUMERIC_LEN = 64
_NUMERIC_TAG = "\x00tf-num:"
# Escape prefix for a genuine string that itself begins with the reserved NUL,
# so a crafted value like "\x00tf-num:130" can never collide with a numeric token.
_ESCAPE_TAG = "\x00tf-esc:"

# Shared canonical token for column-level nullish equivalences. Distinct from
# every legitimate value under a folded column: ``None`` cannot collide with a
# string of any content, and no state ever stores a value with this leading NUL.
_NULLISH_TOKEN = "\x00tf-nullish"

# Trailing UTC markers a timezone-normalized string treats as interchangeable
# with the suffix-free form. All three collapse the same equivalence when the
# DB layer round-trips one form as another.
_TIMEZONE_UTC_SUFFIXES: tuple[str, ...] = ("Z", "+00:00", "+0000")


def _is_empty_collection(value: Any) -> bool:
    """A value the ``treat_null_as_empty_collection`` fold treats as ``None``."""
    if value is None:
        return True
    if isinstance(value, list) and not value:
        return True
    if isinstance(value, dict) and not value:
        return True
    return False


def _fold_column_value(value: Any, rule: "ColumnCompareRule") -> Any:
    """Canonicalize one column's scalar value under the rule's equivalence flags.

    Symmetric: applied to both trial and golden before hashing, so any two
    values the rule declares equivalent collapse to a single token
    representation on both sides. The subset-mode ``extras_allowed_for``
    filter is applied separately by :func:`apply_compare_columns_extras`
    and does not run here.
    """
    if rule.treat_null_as_empty_collection and _is_empty_collection(value):
        return _NULLISH_TOKEN
    if rule.treat_empty_string_as_null and (
        value is None or (isinstance(value, str) and not value)
    ):
        return _NULLISH_TOKEN
    if rule.normalize_timezone_suffix and isinstance(value, str):
        for suffix in _TIMEZONE_UTC_SUFFIXES:
            if value.endswith(suffix):
                return value[: -len(suffix)]
    return value


#: Sentinel rule applied by :func:`apply_global_nullable_normalize` when a
#: pack sets ``state_checks.auto_normalize_nullables: true``. Sets only the
#: two null-vs-empty flags — timezone-suffix stripping is deliberately NOT
#: in the global pass, since a non-datetime string ending in ``Z`` /
#: ``+0000`` (e.g. a product code) would false-collapse. Packs that want
#: timezone-suffix normalization on a specific datetime column declare it
#: per-column via :class:`ColumnCompareRule.normalize_timezone_suffix`.
_GLOBAL_NULLABLE_RULE: "ColumnCompareRule" = ColumnCompareRule(
    treat_null_as_empty_collection=True,
    treat_empty_string_as_null=True,
)


def apply_global_nullable_normalize(
    state: dict[str, Any],
    enabled: bool,
) -> dict[str, Any]:
    """Return ``state`` with every scalar column folded under the two
    null-vs-empty equivalences (``None ≡ [] ≡ {} ≡ ""``) when ``enabled``
    is True.

    Task-level bool grading config
    ``state_checks.auto_normalize_nullables``. Composes with per-column
    :class:`ColumnCompareRule` declarations — this pass runs first, so a
    per-column rule that sets one of the same flags is idempotent, and a
    per-column rule that sets a distinct flag (e.g. ``mode: subset``,
    ``order: unordered``, ``normalize_timezone_suffix``) still applies
    afterwards.

    Timezone-suffix normalization is NOT part of the global pass — a
    trailing ``Z`` / ``+00:00`` / ``+0000`` can appear on non-datetime
    strings, so packs opt in per-column via
    :attr:`ColumnCompareRule.normalize_timezone_suffix` instead.

    Symmetric by design: every caller applies it to both trial and golden
    before hashing.

    Table entries that are not row lists (single dicts, or non-collection
    scalars at the top level) are folded key-by-key; anything else passes
    through unchanged. A non-dict ``state`` passes through unchanged
    (matching :func:`apply_auto_clock_mask`'s guard).
    """
    if not enabled or not isinstance(state, dict):
        return state

    def _fold_row(row: Any) -> Any:
        if not isinstance(row, dict):
            return row
        return {key: _fold_column_value(value, _GLOBAL_NULLABLE_RULE) for key, value in row.items()}

    result = dict(state)
    for table, table_data in state.items():
        if isinstance(table_data, list):
            result[table] = [_fold_row(row) for row in table_data]
        elif isinstance(table_data, dict):
            result[table] = _fold_row(table_data)
    return result


def apply_compare_columns_equivalences(
    state: dict[str, Any],
    compare_columns: dict[str, dict[str, "ColumnCompareRule"]] | None,
) -> dict[str, Any]:
    """Return ``state`` with column-level equivalence folds applied.

    For each ``(table, column)`` in ``compare_columns`` whose rule sets any of
    ``treat_null_as_empty_collection``, ``normalize_timezone_suffix`` or
    ``treat_empty_string_as_null``, canonicalizes the column's value in every
    row of the table via :func:`_fold_column_value`. The fold is symmetric:
    every caller runs it on both trial and golden states before hashing, so
    two values the pack declared equivalent collapse to the same token on
    both sides.

    The returned dict is a shallow copy — tables absent from
    ``compare_columns`` share list / dict references with the input rather
    than being deep-copied. Callers that hash the result (``compute_stable_hash``)
    and discard it — the only supported use — are unaffected.

    A rule with only structural mode (``mode: subset`` and no equivalence
    flags set) is a no-op here — its behaviour lives in
    :func:`apply_compare_columns_extras`.
    """
    if not compare_columns:
        return state

    def _rule_has_equivalence(rule: "ColumnCompareRule") -> bool:
        return (
            rule.treat_null_as_empty_collection
            or rule.normalize_timezone_suffix
            or rule.treat_empty_string_as_null
        )

    def _fold_row(row: Any, rules: list[tuple[str, "ColumnCompareRule"]]) -> Any:
        if not isinstance(row, dict):
            return row
        folded = dict(row)
        for column, rule in rules:
            if column in folded:
                folded[column] = _fold_column_value(folded[column], rule)
        return folded

    result = dict(state)
    for table, column_rules in compare_columns.items():
        rules_with_equivalence: list[tuple[str, ColumnCompareRule]] = [
            (column, rule) for column, rule in column_rules.items() if _rule_has_equivalence(rule)
        ]
        if not rules_with_equivalence:
            continue
        table_data = result.get(table)
        if isinstance(table_data, list):
            result[table] = [_fold_row(row, rules_with_equivalence) for row in table_data]
        elif isinstance(table_data, dict):
            result[table] = _fold_row(table_data, rules_with_equivalence)
    return result


def apply_compare_columns_ordering(
    state: dict[str, Any],
    compare_columns: dict[str, dict[str, "ColumnCompareRule"]] | None,
    *,
    numeric_string_fields: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Return ``state`` with each row list sorted for tables the pack declares unordered.

    Row order matters to :func:`compute_stable_hash` by default — a
    permutation on the same rows hashes differently. For tables whose
    domain semantics are set-of-rows, the pack opts in via ``order:
    unordered`` on any one column-rule of the table; this pass then sorts
    the table's row list on both sides so a pure permutation stops
    failing the hash.

    Sort key is the canonical JSON serialization of a numeric-canonicalized
    view of the row. ``numeric_string_fields`` (the same per-field opt-in
    :func:`compute_stable_hash` consumes) folds "1" ≡ "1.0" ≡ 1 before the
    sort so IDs the pack declared numeric sort together on both sides. Order
    is stable, hash-safe, and does not depend on which column carries the
    ``order`` declaration. A row that cannot be canonically serialized is
    itself unhashable downstream — surfaced as :class:`TypeError` here so
    the failure names the row rather than deferring to a later hash step.

    The returned dict is a shallow copy — tables absent from
    ``compare_columns`` share list references with the input rather than
    being deep-copied. Callers that hash the result
    (:func:`compute_stable_hash`) and discard it — the only supported
    use — are unaffected.

    Prefer :func:`apply_compare_columns_pipeline` over calling this
    directly — the pipeline runs equivalence folds, ordering, and extras
    in the order the state-hash comparator requires.
    """
    if not compare_columns:
        return state

    def _row_sort_key(row: Any) -> str:
        canonical = _canonicalize_numbers(row, numeric_string_fields)
        try:
            return json.dumps(canonical, sort_keys=True, default=str)
        except TypeError as exc:
            raise TypeError(
                f"cannot canonicalize row for order: unordered — row is not JSON-serializable: {row!r}"
            ) from exc

    result = dict(state)
    for table, column_rules in compare_columns.items():
        if not any(rule.order == "unordered" for rule in column_rules.values()):
            continue
        table_data = result.get(table)
        if isinstance(table_data, list):
            result[table] = sorted(table_data, key=_row_sort_key)
    return result


def apply_compare_columns_pipeline(
    actual: dict[str, Any],
    expected: dict[str, Any],
    compare_columns: dict[str, dict[str, "ColumnCompareRule"]] | None,
    *,
    numeric_string_fields: frozenset[str] | None = None,
    auto_normalize_nullables: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply the state-hash comparator's per-column pipeline to both sides.

    Order matters and this function owns it:

    0. Global nullable normalization
       (:func:`apply_global_nullable_normalize`) runs first on both sides
       when the task sets ``state_checks.auto_normalize_nullables: true`` —
       every scalar collapses under ``None ≡ [] ≡ {} ≡ ""`` before
       per-column rules see the values. Timezone-suffix stripping stays
       per-column-opt-in (see
       :attr:`ColumnCompareRule.normalize_timezone_suffix`).
    1. Equivalence folds (:func:`apply_compare_columns_equivalences`) run
       next on both sides — two values a rule declared equivalent collapse
       to one token before anything else sees them. Idempotent with the
       global pass; per-column rules setting distinct flags still apply.
    2. Ordering (:func:`apply_compare_columns_ordering`) then sorts the
       row list of any table a rule declared ``unordered`` on both sides.
       Extras filtering pairs rows positionally, so the sort must happen
       before extras run or the wrong pair of rows drives the drop.
    3. Extras (:func:`apply_compare_columns_extras`) drops keys the pack
       declared permitted-extra from ``actual`` where ``expected`` does
       not carry them, on the sorted pairing.

    Two-sided by design: the actual and expected states must go through
    the same pipeline for their hashes to agree, and calling site
    ordering has been a load-bearing invariant. Callers pass both raw
    states and receive both processed states in one step.

    ``numeric_string_fields``, when provided, is threaded into the
    ordering step so an ID column the pack declared numeric folds
    ``"1"`` and ``"1.0"`` into the same sort position on both sides.
    """
    if not compare_columns and not auto_normalize_nullables:
        return actual, expected
    actual_normalized = apply_global_nullable_normalize(actual, auto_normalize_nullables)
    expected_normalized = apply_global_nullable_normalize(expected, auto_normalize_nullables)
    actual_folded = apply_compare_columns_equivalences(actual_normalized, compare_columns)
    expected_folded = apply_compare_columns_equivalences(expected_normalized, compare_columns)
    actual_sorted = apply_compare_columns_ordering(
        actual_folded, compare_columns, numeric_string_fields=numeric_string_fields
    )
    expected_sorted = apply_compare_columns_ordering(
        expected_folded, compare_columns, numeric_string_fields=numeric_string_fields
    )
    actual_final = apply_compare_columns_extras(actual_sorted, expected_sorted, compare_columns)
    return actual_final, expected_sorted


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
      genuinely wrong state. Enabled per FIELD via the grading config
      (``state_checks.numeric_string_fields``) for the specific money / quantity
      fields that are genuinely numeric (e.g. DB-round-tripped Decimal columns),
      not as a blanket per-task switch.

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


def _canonicalize_numbers(
    data: Any,
    string_fields: frozenset[str] | None = None,
    *,
    _normalize_strings: bool = False,
) -> Any:
    """Recursively fold numerically-equal values in a state.

    Numeric TYPES (int/float/Decimal) always fold. Numeric-looking STRINGS fold
    only for a value sitting under a record key named in ``string_fields`` — the
    per-field opt-in. ``_normalize_strings`` carries that per-key decision down
    through the value's enclosing list(s); a nested dict re-decides per its own
    keys. So a version/code string field is never folded merely because a
    sibling money field in the same record is listed.
    """
    if isinstance(data, dict):
        return {
            key: _canonicalize_numbers(
                value,
                string_fields,
                _normalize_strings=(string_fields is not None and key in string_fields),
            )
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [
            _canonicalize_numbers(item, string_fields, _normalize_strings=_normalize_strings)
            for item in data
        ]
    if isinstance(data, tuple):
        return tuple(
            _canonicalize_numbers(item, string_fields, _normalize_strings=_normalize_strings)
            for item in data
        )
    return canonical_number(data, normalize_strings=_normalize_strings)


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


#: Column names whose values are auto-generated write-time clocks — dropped
#: everywhere they appear as record keys when
#: ``state_checks.auto_mask_clock_columns`` is enabled. The six names cover
#: the Salesforce / D365 / Zendesk conventions typical of the CRM-shaped
#: packs the flag targets. All matched case-sensitively at the exact
#: record-key level; a nested field named ``updated_at`` inside a JSON
#: payload is not touched (record keys sit at the table row's top level,
#: one nesting layer inside the state dict). Extending the set is a
#: wire-lock change.
AUTO_MASKED_CLOCK_COLUMNS: frozenset[str] = frozenset(
    {
        "updated_at",
        "updated_on",
        "last_modified",
        "last_modified_date",
        "modified_at",
        "modified_on",
    }
)


def apply_auto_clock_mask(state: dict[str, Any]) -> dict[str, Any]:
    """Return ``state`` with every :data:`AUTO_MASKED_CLOCK_COLUMNS` key
    dropped from every table row.

    Only ``list``-valued top-level entries are treated as tables — the
    row-list shape :func:`apply_compare_columns_extras` and
    :func:`apply_compare_columns_ordering` also assume. Dict-valued
    top-level entries (e.g. a ``metadata`` block carrying a schema
    ``updated_at`` stamp) are left untouched: they are not table rows and
    their clock keys are not the write-time clocks the flag targets.

    Symmetric — every caller runs it on both trial and golden sides before
    hashing, so a clock column present on one side but not the other, or
    holding two different timestamps for the same content, folds to the
    same absent-column state on both. Composes with ``unstable_fields``:
    a pack-declared mask still drops what it drops, this one drops the
    clock columns the pack forgot.

    Returned dict is a shallow copy; row dicts that carried none of the
    masked columns share references with the input.
    """
    if not isinstance(state, dict):
        return state
    result: dict[str, Any] = dict(state)
    for table, table_data in state.items():
        if isinstance(table_data, list):
            result[table] = [_drop_clock_columns_from_row(row) for row in table_data]
    return result


def _drop_clock_columns_from_row(row: Any) -> Any:
    if not isinstance(row, dict):
        return row
    if not AUTO_MASKED_CLOCK_COLUMNS.intersection(row.keys()):
        return row
    return {key: value for key, value in row.items() if key not in AUTO_MASKED_CLOCK_COLUMNS}


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
    numeric_string_fields: Iterable[str] | None = None,
    auto_mask_clock_columns: bool = False,
    auto_normalize_nullables: bool = False,
) -> str:
    """
    Compute a stable SHA-256 hash of the state dictionary.

    With ``canonicalize_numbers=False`` this produces the same output as
    mcp_core.utils.validation.calculate_database_hash() for identical state
    dictionaries. The default (True) additionally folds numerically-equal
    representations, intentionally diverging from that byte-for-byte output.

    This is the runner substrate's digest, and it is frozen: its output is
    persisted — db-service ETags (:func:`compute_etag`), snapshot hashes, and
    the ``ResetTrialResponse.state_hash`` / ``GetStateResponse.stable_hash``
    wire fields — so a serialisation change invalidates every digest already
    stored. Core grading's ``state_digest``
    (``tolokaforge/core/grading/state_checks.py``) is a different algebra over
    the same equivalence relation: a comparison hashes both sides with one
    function, and a digest never crosses substrates
    (``tests/canonical/test_expected_state_hash_is_not_portable.py``).

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
        numeric_string_fields: Record-level field names whose numeric-looking
            STRING values should ALSO fold ("130.00" == "130.0" == "130"). This
            is deliberately PER-FIELD, not global: a string that looks numeric
            can carry meaning in its exact representation (versions "1.10" vs
            "1.1", zero-padded codes), so folding is opt-in only for the money /
            quantity fields a task declares here (grading config
            ``state_checks.numeric_string_fields``). Matched by the immediate
            record key at any depth. Ignored when canonicalize_numbers is False.
        auto_mask_clock_columns: When True, drop every column named in
            :data:`AUTO_MASKED_CLOCK_COLUMNS` from every table row before
            hashing. Composes with ``unstable_fields`` — pack-declared masks
            still apply on top of this one. Grading config
            ``state_checks.auto_mask_clock_columns``.
        auto_normalize_nullables: When True, fold every scalar column
            under the two null-vs-empty equivalences (``None ≡ [] ≡ {} ≡
            ""``) before hashing. Timezone-suffix stripping is opt-in
            per-column via
            :attr:`ColumnCompareRule.normalize_timezone_suffix` and not
            part of this pass. Symmetric with the core substrate's
            ``state_digest`` flag so the two continue to agree on which
            states are equal. Grading config
            ``state_checks.auto_normalize_nullables``.

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

    # Auto-mask conventional write-time clock columns (opt-in).
    if auto_mask_clock_columns:
        state = apply_auto_clock_mask(state)

    # Global nullable normalization (opt-in): fold every scalar column
    # under None ≡ [] ≡ {} ≡ "" and trailing-Z stripping so a pack does
    # not have to enumerate every nullable column in compare_columns.
    if auto_normalize_nullables:
        state = apply_global_nullable_normalize(state, True)

    # Convert datetime objects to strings
    serializable_state = _convert_datetime_to_str(state)

    # Collapse numerically-equal representations so a pure representation
    # difference is not graded as a state change: numeric TYPES always
    # (72 == 72.0); numeric-looking STRINGS only in the opt-in per-field set
    # ("130.00" == "130.0" — see numeric_string_fields).
    if canonicalize_numbers:
        string_fields = frozenset(numeric_string_fields) if numeric_string_fields else None
        serializable_state = _canonicalize_numbers(serializable_state, string_fields)

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
