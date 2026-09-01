"""
Grading Helper Functions for Runner Service

This module provides helper functions for the GradeTrial RPC implementation:
- compute_state_diff: Compute human-readable diff between two stable states
- evaluate_jsonpath_checks / evaluate_jsonpath_file_checks / evaluate_jsonpath_state_checks:
  runner-side JSONPath assertion evaluators
- evaluate_db_probes: substrate SQL probes against a task-declared postgres

See docs/GRPC_PROTOCOL.md for grading algorithm specification.
"""

import glob
import logging
from pathlib import Path
from typing import Any

from jsonpath_ng.ext import parse

from tolokaforge.core.grading.predicates import contains
from tolokaforge.core.hash import canonical_number
from tolokaforge.runner.models import (
    StateDiff,
    TableDiff,
)

logger = logging.getLogger(__name__)


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

    return TableDiff(missing=missing, extra=extra, different=different)


def _records_might_match(record1: dict[str, Any], record2: dict[str, Any]) -> bool:
    """
    Check if two records might be the same entity with different values.

    Matches records by any shared field whose name ends with ``_id`` or is
    exactly ``id``.  This is domain-agnostic — it works for any entity type
    (lot_id, sku_id, allocation_id, capa_id, equipment_id, etc.) without
    requiring a hardcoded list.
    """
    # Find all shared identifier-like fields (ending with _id or exactly "id")
    common_keys = set(record1.keys()) & set(record2.keys())
    id_fields = sorted(f for f in common_keys if f == "id" or f.endswith("_id"))

    # Match on the first shared ID field (most specific). Compare via the same
    # canonical form as the record hashing, so numeric-TYPE ids pair across
    # representations (123 == 123.0 == Decimal("123")). A numeric-looking STRING
    # id ("123") is NOT equated with the number 123 here: string folding is the
    # opt-in per-field tier, off on this reason-only diff path.
    for field in id_fields:
        if record1[field] is not None and record2[field] is not None:
            return _make_hashable(record1[field]) == _make_hashable(record2[field])

    # Fallback: check if they share at least 50% of fields with same values
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


def evaluate_jsonpath_file_checks(
    checks: list[dict[str, Any]],
) -> tuple[float, str]:
    """
    Evaluate jsonpath file assertions against the Runner container's filesystem.

    This evaluator only understands ``path_glob:`` + ``contains_ci:`` (file-content
    checks). It does not handle ``path:``-style JSONPath assertions on env state
    (those are evaluated host-side by ``StateChecker.check_jsonpaths``). An
    assertion missing ``path_glob:`` is treated as **failed** with an actionable
    reason, so a misrouted assertion is named rather than vanishing from grading.

    Each check has:
    - path_glob: glob pattern for files (e.g., "/env/fs/agent-visible/submissions/*")
    - contains_ci: case-insensitive substring to find in file content
    - description: human-readable description of the check

    Args:
        checks: List of jsonpath check dicts from grading.yaml

    Returns:
        Tuple of (score, reasons_string) where score is fraction of checks passed.
        Score is -1.0 (sentinel) when checks list is empty.
    """
    if not checks:
        return -1.0, ""

    passed = 0
    total = len(checks)
    reasons_parts: list[str] = []

    for check in checks:
        path_pattern = check.get("path_glob", "")
        contains_ci = check.get("contains_ci", "")
        description = check.get("description", f"Check: {contains_ci}")

        if not path_pattern:
            # Named rather than skipped: a skip scores as not-passed while reading
            # as SKIP, which is how a misrouted assertion goes unnoticed.
            other_path = check.get("path")
            if other_path:
                reasons_parts.append(
                    f"FAIL: assertion uses 'path' (env-state JSONPath) but this "
                    f"runner-side evaluator only supports 'path_glob' (file glob); "
                    f"path-style assertions are graded host-side — {description}"
                )
            else:
                reasons_parts.append(
                    f"FAIL: assertion missing 'path_glob' (runner-side jsonpath "
                    f"evaluator only supports path_glob + contains_ci) — {description}"
                )
            continue

        # Translate logical /env/fs/agent-visible/ paths to the runner's
        # actual /work/ tree (matching where the file tools and BashTool
        # operate). This keeps grading/runtime consistent: the agent
        # writes via write_file under /work/, and the grader reads from
        # the same place.
        resolved_pattern = path_pattern
        if resolved_pattern.startswith("/env/fs/agent-visible/"):
            resolved_pattern = "/work/" + resolved_pattern[len("/env/fs/agent-visible/") :]
        elif resolved_pattern == "/env/fs/agent-visible":
            resolved_pattern = "/work"

        # Glob for matching files on the container filesystem
        matching_files = glob.glob(resolved_pattern)

        if not matching_files:
            reasons_parts.append(f"FAIL: No files match {path_pattern} — {description}")
            continue

        # Check if any matching file contains the expected text
        found = False
        for file_path in matching_files:
            try:
                content = Path(file_path).read_text(encoding="utf-8", errors="replace")
                if contains_ci.lower() in content.lower():
                    found = True
                    break
            except Exception as exc:
                logger.warning("Failed to read file %s: %s", file_path, exc)

        if found:
            passed += 1
            reasons_parts.append(f"PASS: {description}")
        else:
            reasons_parts.append(f"FAIL: {description}")

    score = passed / total if total > 0 else 0.0
    reasons = "; ".join(reasons_parts)
    return score, reasons


def evaluate_jsonpath_state_checks(
    checks: list[dict[str, Any]],
    state: dict[str, Any],
) -> tuple[float, str]:
    """
    Evaluate JSONPath assertions against the trial's structured DB state.

    This is the state-oriented counterpart to evaluate_jsonpath_file_checks().
    It supports the same JSONPath assertion shape used by the core native
    grader: path + one of equals, equals_ci, contains, contains_ci.
    """
    if not checks:
        return -1.0, ""

    passed = 0
    total = len(checks)
    reasons_parts: list[str] = []

    for check in checks:
        path = check.get("path")
        description = check.get("description", path or "JSONPath state check")
        if not path:
            reasons_parts.append(f"FAIL: Missing path — {description}")
            continue

        operators = [
            ("equals", check.get("equals")),
            ("equals_ci", check.get("equals_ci")),
            ("contains", check.get("contains")),
            ("contains_ci", check.get("contains_ci")),
        ]
        active = [(name, expected) for name, expected in operators if expected is not None]
        if len(active) > 1:
            reasons_parts.append(f"FAIL: Multiple operators at {path} — {description}")
            continue

        try:
            matches = [match.value for match in parse(path).find(state)]
        except Exception as exc:
            reasons_parts.append(f"FAIL: Invalid JSONPath {path}: {exc} — {description}")
            continue

        if not matches:
            reasons_parts.append(f"FAIL: Path not found {path} — {description}")
            continue

        if not active:
            # Fail loud — mirrors the core native grader's contract: an
            # assertion with no recognized operator (a typo'd or unsupported
            # operator such as ``op:``/``expected:``, or a bare ``path:`` with
            # no comparison) is an author error, not a passing existence check.
            # Silently passing it would turn a strict-looking assertion into a
            # no-op and diverge from host-side grading.
            unknown_keys = sorted(
                key for key in check if key not in {"path", "path_glob", "description"}
            )
            reasons_parts.append(
                f"FAIL: no recognized operator at {path} (got keys: {unknown_keys}); "
                f"supported operators are 'equals', 'equals_ci', 'contains', "
                f"'contains_ci' — {description}"
            )
            continue

        op_name, expected = active[0]
        found = False
        for value in matches:
            if op_name == "equals" and value == expected:
                found = True
                break
            if op_name == "equals_ci" and isinstance(value, str) and isinstance(expected, str):
                if value.casefold() == expected.casefold():
                    found = True
                    break
            if op_name == "contains" and contains(value, expected):
                found = True
                break
            if op_name == "contains_ci" and contains(value, expected, ci=True):
                found = True
                break

        if found:
            passed += 1
            reasons_parts.append(f"PASS: {description}")
        else:
            reasons_parts.append(
                f"FAIL: {description} ({path}: expected {op_name} {expected!r}, got {matches[0]!r})"
            )

    score = passed / total if total > 0 else 0.0
    reasons = "; ".join(reasons_parts)
    return score, reasons


async def _fetch_probe_rows(dsn: str, query: str) -> list[dict[str, Any]]:
    """Connect to ``dsn`` via asyncpg, run ``query``, return rows as dicts.

    Isolated so unit tests inject rows without a live database; the asyncpg
    import is deferred so importing this module never requires the driver.
    """
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        records = await conn.fetch(query)
    finally:
        await conn.close()
    return [dict(record) for record in records]


async def evaluate_db_probes(probes: list[dict[str, Any]]) -> tuple[float, str]:
    """Evaluate substrate SQL probes against a task-declared postgres.

    For each probe, connect to its ``dsn``, run its read-only ``query``, shape
    the result into ``{"rows": [...], "row_count": N}``, and apply its ``expect``
    JSONPath assertions via ``evaluate_jsonpath_state_checks``.

    Two-level aggregation: a probe passes iff *every* ``expect`` assertion passes
    (its JSONPath score is 1.0); the component score is the fraction of passing
    probes. A connection/query failure is a FAILED probe with an actionable
    reason (fail loud — never a silent pass). Empty list → -1.0 sentinel,
    matching the file/state evaluators.
    """
    if not probes:
        return -1.0, ""

    passed = 0
    total = len(probes)
    reasons_parts: list[str] = []

    for probe in probes:
        name = probe.get("name", "")
        description = probe.get("description") or name
        expect = probe.get("expect", []) or []

        try:
            rows = await _fetch_probe_rows(probe.get("dsn", ""), probe.get("query", ""))
        except Exception as exc:
            reasons_parts.append(
                f"FAIL: probe {name!r} could not query postgres: "
                f"{type(exc).__name__}: {exc} — {description}"
            )
            continue

        state = {"rows": rows, "row_count": len(rows)}
        probe_score, probe_reasons = evaluate_jsonpath_state_checks(expect, state)
        if probe_score == 1.0:
            passed += 1
            reasons_parts.append(f"PASS: probe {name!r} — {probe_reasons}")
        else:
            reasons_parts.append(f"FAIL: probe {name!r} — {probe_reasons}")

    score = passed / total
    return score, "; ".join(reasons_parts)


def evaluate_jsonpath_checks(
    checks: list[dict[str, Any]],
    state: dict[str, Any] | None = None,
) -> tuple[float, str]:
    """
    Evaluate mixed JSONPath checks.

    Checks with path_glob remain file-content checks for backwards
    compatibility. Checks with path are evaluated against structured DB state.
    """
    if not checks:
        return -1.0, ""

    file_checks: list[dict[str, Any]] = []
    state_checks: list[dict[str, Any]] = []
    invalid_checks: list[dict[str, Any]] = []

    for check in checks:
        if check.get("path_glob") is not None and check.get("path") is None:
            file_checks.append(check)
        elif check.get("path") is not None:
            state_checks.append(check)
        else:
            invalid_checks.append(check)

    passed = 0.0
    total = len(checks)
    reasons_parts: list[str] = []

    if file_checks:
        file_score, file_reasons = evaluate_jsonpath_file_checks(file_checks)
        if file_score >= 0:
            passed += file_score * len(file_checks)
        if file_reasons:
            reasons_parts.append(f"Files: {file_reasons}")

    if state_checks:
        if state is None:
            reasons_parts.append("State: FAIL: DB state unavailable for JSONPath checks")
        else:
            state_score, state_reasons = evaluate_jsonpath_state_checks(state_checks, state)
            if state_score >= 0:
                passed += state_score * len(state_checks)
            if state_reasons:
                reasons_parts.append(f"State: {state_reasons}")

    for check in invalid_checks:
        description = check.get("description", "JSONPath check")
        reasons_parts.append(f"FAIL: Missing path/path_glob — {description}")

    score = passed / total if total > 0 else 0.0
    reasons = "; ".join(reasons_parts)
    return score, reasons
