"""Runner-wire helpers around GradeTrial's state-diff family.

Three responsibilities live here — all runner-wire adjacent, all consumed
by ``runner.service`` on the GradeTrial path:

- :func:`compute_state_diff` (+ private helpers) — human-readable diff
  between the trial's final stable state and the golden state, shaped
  into the runner-side :class:`~tolokaforge.runner.models.StateDiff` /
  :class:`~tolokaforge.runner.models.TableDiff` types.
- :func:`project_state_checks_to_runner_wire` — encode the composite
  fold's neutral ``None``-means-not-evaluated ``state_checks`` slot into
  the runner wire's ``-1.0`` sentinel.
- :func:`project_check_result_to_runner_wire` — encode a composite-produced
  :class:`~tolokaforge.core.grading.checks_interface.CheckResult` into the
  wire ``pb2.CustomCheckResult`` for ``Grade.custom_checks``.

See docs/GRPC_PROTOCOL.md for the grading algorithm specification. The
JSONPath assertion evaluators and the SQL-probe evaluator live in
:mod:`tolokaforge.core.grading.jsonpath_evaluators` and
:mod:`tolokaforge.core.grading.db_probes` — pure grading libraries the
runner-side GradeTrial and the standalone Grader v3 service both drive.
"""

import json
import logging
from typing import Any

from tolokaforge.core.grading.checks_interface import CheckResult
from tolokaforge.core.hash import canonical_number
from tolokaforge.core.models.grade import Grade
from tolokaforge.runner import runner_pb2 as pb2
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


def project_state_checks_to_runner_wire(slot_component: float | None) -> float:
    """Encode the fold's ``state_checks`` slot into the runner wire's sentinel.

    The composite fold reports ``None`` where the ``state_checks`` block was
    not evaluated; the runner's ``pb2.GradeComponents`` field is a plain
    ``float`` and reserves ``-1.0`` as the not-evaluated sentinel. Kept
    beside the runner's other wire helpers so the pure fold module owes
    nothing to ``pb2``.
    """
    return -1.0 if slot_component is None else slot_component


def project_check_result_to_runner_wire(result: CheckResult) -> pb2.CustomCheckResult:
    """Encode a :class:`CheckResult` to the wire ``pb2.CustomCheckResult``.

    ``details`` is JSON-encoded into the proto's ``details_json`` string —
    empty when the check emitted no details. The status is projected via the
    enum's ``.value`` (or ``str(...)`` for a plain-string status), so the
    ``pb2.CustomCheckResult.status`` field always carries the lowercased
    literal (``passed`` / ``failed`` / ``skipped`` / ``error``) the wire
    contract pins. Kept beside :func:`project_state_checks_to_runner_wire` —
    every runner wire encoder scoped to a composite output lives here so
    the composite package owes nothing to ``pb2``.
    """
    status_str = result.status.value if hasattr(result.status, "value") else str(result.status)
    details_json = json.dumps(result.details) if result.details else ""
    return pb2.CustomCheckResult(
        check_name=result.check_name,
        status=status_str,
        score=result.score,
        message=result.message,
        details_json=details_json,
    )


def grade_to_runner_wire(grade: Grade) -> pb2.Grade:
    """Encode a Pydantic :class:`Grade` to the wire ``pb2.Grade``.

    Component scores are encoded with the runner-wire sentinel: ``None`` (tier
    did not run) → ``-1.0`` for the four scalar components; ``trace_checks``
    stays ``None`` when the tier did not run (proto3 optional presence), else
    the scored value or ``-1.0`` when explicitly set to "not evaluated".

    ``state_diff`` is JSON-encoded into ``state_diff_json`` — empty string
    when absent. ``custom_checks_details`` maps to the wire's ``custom_checks``
    repeated field; each entry's ``details`` dict is JSON-encoded into
    ``details_json`` — empty when the detail carried no dict.

    ``reasons`` is projected as a string: a dict-form (per-criterion reasons)
    is JSON-encoded so the wire's string field always carries a scalar.
    """
    state_checks_wire = project_state_checks_to_runner_wire(grade.components.state_checks)
    transcript_rules_wire = (
        -1.0 if grade.components.transcript_rules is None else grade.components.transcript_rules
    )
    llm_judge_wire = -1.0 if grade.components.llm_judge is None else grade.components.llm_judge
    custom_checks_wire = (
        -1.0 if grade.components.custom_checks is None else grade.components.custom_checks
    )
    components_kwargs: dict[str, float] = {
        "state_checks": state_checks_wire,
        "transcript_rules": transcript_rules_wire,
        "llm_judge": llm_judge_wire,
        "custom_checks": custom_checks_wire,
    }
    if grade.components.trace_checks is not None:
        components_kwargs["trace_checks"] = grade.components.trace_checks

    custom_check_wire: list[pb2.CustomCheckResult] = []
    for detail in grade.custom_checks_details or ():
        details_json = json.dumps(detail.details) if detail.details else ""
        custom_check_wire.append(
            pb2.CustomCheckResult(
                check_name=detail.check_name,
                status=detail.status,
                score=detail.score,
                message=detail.message,
                details_json=details_json,
            )
        )

    reasons_wire = grade.reasons if isinstance(grade.reasons, str) else json.dumps(grade.reasons)
    state_diff_json = json.dumps(grade.state_diff) if grade.state_diff else ""

    return pb2.Grade(
        binary_pass=grade.binary_pass,
        score=grade.score,
        components=pb2.GradeComponents(**components_kwargs),
        reasons=reasons_wire,
        state_diff_json=state_diff_json,
        custom_checks=custom_check_wire,
    )
