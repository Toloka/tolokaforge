"""The diff between a trial's state and the state it is compared against.

:func:`compute_state_diff` reports, table by table, the rows the golden holds that
the trial does not (``missing``), the rows the trial holds that the golden does not
(``extra``), the pairs that look like one record with different values
(``different``) and a table whose rows agree as a set but not in order
(``order_mismatch``). It is the diff a hash mismatch is reported with on both grading
substrates: the runner's raw ``state_diff`` and the view diff a comparison view
records, on the runner and in core alike — which is why it lives here rather than in
:mod:`tolokaforge.runner.grading`, which core grading may not import.
"""

from __future__ import annotations

from typing import Any

from tolokaforge.core.hash import canonical_number
from tolokaforge.runner.models import StateDiff, TableDiff

__all__ = ["compute_state_diff"]


def compute_state_diff(trial_state: dict[str, Any], golden_state: dict[str, Any]) -> StateDiff:
    """
    Compute human-readable diff between two stable states.

    Compares table by table and returns differences in a structured format.

    Args:
        trial_state: The state produced by the agent's actions
        golden_state: The expected state from golden path execution

    Returns:
        StateDiff with tables and summary
    """
    tables_diff: dict[str, TableDiff] = {}
    differences_found = []

    # Get all table names from both states
    all_tables = set(trial_state.keys()) | set(golden_state.keys())

    for table_name in sorted(all_tables):
        trial_records = trial_state.get(table_name, [])
        golden_records = golden_state.get(table_name, [])

        table_diff = _compare_table_records(trial_records, golden_records)

        if table_diff.missing or table_diff.extra or table_diff.different:
            tables_diff[table_name] = table_diff
            differences_found.append(
                f"{table_name}: {len(table_diff.missing)} missing, "
                f"{len(table_diff.extra)} extra, "
                f"{len(table_diff.different)} different"
            )
        elif table_diff.order_mismatch:
            tables_diff[table_name] = table_diff
            differences_found.append(f"{table_name}: rows in wrong order")

    # Build summary
    if differences_found:
        summary = "State mismatch: " + "; ".join(differences_found)
    else:
        summary = "States match"

    return StateDiff(tables=tables_diff, summary=summary)


def _make_hashable(value: Any) -> Any:
    """Make a value hashable for comparison.

    Scalars pass through :func:`canonical_number` so numerically-equal
    representations (``"130.00"`` / ``"130.0"`` / ``130``) compare equal and a
    pure decimal-formatting difference is not reported as a row/field change.
    """
    if isinstance(value, dict):
        return tuple(sorted((k, _make_hashable(v)) for k, v in value.items()))
    elif isinstance(value, list):
        return tuple(_make_hashable(v) for v in value)
    return canonical_number(value)


def _compare_table_records(
    trial_records: list[dict[str, Any]], golden_records: list[dict[str, Any]]
) -> TableDiff:
    """
    Compare records between trial and golden states for a single table.

    Uses a hash-based approach to identify matching records, then compares
    field values for records that might be the same but have differences.

    Args:
        trial_records: Records from trial state
        golden_records: Records from golden state

    Returns:
        TableDiff with missing, extra, and different lists
    """
    missing: list[dict[str, Any]] = []
    extra: list[dict[str, Any]] = []
    different: list[dict[str, Any]] = []

    # Convert records to comparable tuples for set operations
    def record_to_tuple(record: dict[str, Any]) -> tuple:
        """Convert record to hashable tuple for comparison."""
        return tuple(sorted((k, _make_hashable(v)) for k, v in record.items()))

    # Ordered sequences drive the order-mismatch check below. Set-diff still
    # dominates: any missing/extra/different suppresses the order-mismatch flag.
    trial_ordered = [record_to_tuple(r) for r in trial_records]
    golden_ordered = [record_to_tuple(r) for r in golden_records]

    trial_tuples = {record_to_tuple(r): r for r in trial_records}
    golden_tuples = {record_to_tuple(r): r for r in golden_records}

    trial_set = set(trial_tuples.keys())
    golden_set = set(golden_tuples.keys())

    # Records in golden but not in trial (missing)
    for t in golden_set - trial_set:
        missing.append(golden_tuples[t])

    # Records in trial but not in golden (extra)
    for t in trial_set - golden_set:
        extra.append(trial_tuples[t])

    # For records that might be "different", we need a more sophisticated approach
    # Try to match records by primary key or first field
    if missing and extra:
        # Try to find records that might be the same but with different values
        matched_missing = set()
        matched_extra = set()

        for i, missing_record in enumerate(missing):
            for j, extra_record in enumerate(extra):
                if j in matched_extra:
                    continue
                # Check if they share a common identifier
                if _records_might_match(missing_record, extra_record):
                    different.append(
                        {
                            "expected": missing_record,
                            "actual": extra_record,
                            "field_diffs": _get_field_diffs(missing_record, extra_record),
                        }
                    )
                    matched_missing.add(i)
                    matched_extra.add(j)
                    break

        # Remove matched records from missing/extra
        missing = [r for i, r in enumerate(missing) if i not in matched_missing]
        extra = [r for i, r in enumerate(extra) if i not in matched_extra]

    # Order-mismatch fires only when set-diff shows nothing (same set, different
    # order in the underlying sequence). Any set-diff dominates.
    order_mismatch = False
    if not missing and not extra and not different:
        order_mismatch = trial_ordered != golden_ordered

    return TableDiff(
        missing=missing, extra=extra, different=different, order_mismatch=order_mismatch
    )


def _records_might_match(record1: dict[str, Any], record2: dict[str, Any]) -> bool:
    """
    Check if two records might be the same entity with different values.

    Matches records by any shared field whose name ends with ``_id`` or is
    exactly ``id``. This is domain-agnostic — it works for any entity type
    (lot_id, sku_id, allocation_id, capa_id, equipment_id, etc.) without
    requiring a hardcoded list.

    Iterates every shared id-suffixed field and returns True as soon as one
    matches. A record whose surrogate ``id`` was reassigned by the substrate
    still pairs with its golden counterpart when any co-recorded id (e.g.
    ``customer_id``, ``order_id``) is stable — surrogate-id divergence on its
    own does not fabricate a missing-plus-extra false diff. A field where
    either side is null is skipped rather than treated as a mismatch: null
    carries no identity signal.
    """
    common_keys = set(record1.keys()) & set(record2.keys())
    id_fields = sorted(f for f in common_keys if f == "id" or f.endswith("_id"))

    # Compare via the same canonical form as record hashing, so numeric-TYPE
    # ids pair across representations (123 == 123.0 == Decimal("123")). A
    # numeric-looking STRING id ("123") is NOT equated with the number 123
    # here: string folding is the opt-in per-field tier, off on this
    # reason-only diff path.
    for field in id_fields:
        if record1[field] is None or record2[field] is None:
            continue
        if _make_hashable(record1[field]) == _make_hashable(record2[field]):
            return True

    # Fallback: no id-field matched, share ≥ 50% of common fields' values.
    if not common_keys:
        return False

    matching_values = sum(
        1 for f in common_keys if _make_hashable(record1[f]) == _make_hashable(record2[f])
    )
    return matching_values >= len(common_keys) * 0.5


def _get_field_diffs(expected: dict[str, Any], actual: dict[str, Any]) -> list[dict[str, Any]]:
    """Get list of field differences between two records."""
    diffs = []
    all_fields = set(expected.keys()) | set(actual.keys())

    for field in sorted(all_fields):
        exp_val = expected.get(field)
        act_val = actual.get(field)
        # Compare via canonical form so a numerically-equal value in a different
        # decimal format ("130.00" vs "130.0") is not reported as a field diff.
        if _make_hashable(exp_val) != _make_hashable(act_val):
            diffs.append({"field": field, "expected": exp_val, "actual": act_val})

    return diffs
