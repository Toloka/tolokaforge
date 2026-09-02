"""
Grading Helper Functions for Runner Service

This module provides helper functions for the GradeTrial RPC implementation:
- compute_state_diff: Compute human-readable diff between two stable states
- combine_grade_components: Combine component scores into final grade
- compose_runner_trial_verdict: Apply the judge and trace gates around that fold, so an
  offline recomputation reaches the runner's verdict without repeating either gate

See docs/GRPC_PROTOCOL.md for grading algorithm specification.
"""

import glob
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonpath_ng.ext import parse

from tolokaforge.core.grading.combine_method import (
    combine_by_method,
    validate_combine_method,
)
from tolokaforge.core.grading.combine_weights import (
    FoldedGrade,
    require_component_weight,
    resolve_uncounted_fold,
)
from tolokaforge.core.grading.golden_replay import (
    GoldenReplayRecord,
    incomplete_replay_reason,
)
from tolokaforge.core.grading.grade_components import (
    GRADE_COMPONENTS,
    component_requested,
    runner_score_field,
)
from tolokaforge.core.grading.predicates import contains
from tolokaforge.core.grading.state_composition import (
    CONFLICTING_STATE_SOURCES_MESSAGE,
    compose_state_checks_score,
    inert_hash_weight_reason,
)
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


@dataclass(frozen=True)
class StateChecksOutcome:
    """The runner's ``state_checks`` slot, and what its declared weight decided.

    ``component`` is ``None`` when no state source produced a score, which the
    combine treats differently from a ``0.0`` it would fold in as a failure.
    ``inert_weight_reason`` carries the author-facing note for a weight the fold
    never consulted, so the runner reports the skip the way the core engine does.
    """

    component: float | None
    inert_weight_reason: str | None


def resolve_state_checks_component(
    *,
    hash_score: float,
    jsonpath_score: float,
    db_probe_score: float,
    hash_weight: float | None,
) -> StateChecksOutcome:
    """Fold the runner's state sources into one ``state_checks`` score.

    Translates the runner's ``-1.0``-means-not-evaluated sentinel into the ``None``
    the shared composer reads. ``db_probes`` is the block's only state source: a probe
    score beside a hash verdict or a JSONPath score is two verdicts for one component
    with no declared share between them, so the pair is refused rather than one of them
    discarded. A probe deciding alone reports a declared weight as unconsulted, the way
    any single-source fold does.

    Reads the scores rather than a config, because that is what this fold holds; the
    same rule over the keys an author writes is
    ``refuse_probes_beside_another_state_source``.

    Raises:
        ValueError: a probe score arrived beside another source, carrying
            ``CONFLICTING_STATE_SOURCES_MESSAGE`` — raised before the weight is read, so
            a block being refused outright is never answered with a demand for a
            ``hash.weight``; or a hash verdict and a JSONPath score are both real and no
            ``hash_weight`` says how to fold them.
    """
    hash_source = None if hash_score < 0 else hash_score
    jsonpath_source = None if jsonpath_score < 0 else jsonpath_score
    probes_decide = db_probe_score >= 0
    if probes_decide and (hash_source is not None or jsonpath_source is not None):
        raise ValueError(CONFLICTING_STATE_SOURCES_MESSAGE)
    return StateChecksOutcome(
        component=(
            db_probe_score
            if probes_decide
            else compose_state_checks_score(
                hash_score=hash_source,
                jsonpath_score=jsonpath_source,
                hash_weight=hash_weight,
            )
        ),
        inert_weight_reason=inert_hash_weight_reason(
            hash_score=hash_source,
            jsonpath_score=jsonpath_source,
            hash_weight=hash_weight,
        ),
    )


def combine_grade_components(
    components: dict[str, Any], grading_config: dict[str, Any]
) -> FoldedGrade:
    """
    Combine component scores into final grade.

    Supports combination methods:
    - "all": All components must pass (score >= threshold)
    - "weighted": Weighted average of component scores
    - "any": Any component passing is sufficient

    Every evaluated component carries a declared weight or the fold raises, and a fold
    with no weighted evaluated component decides before the aggregation and reports why
    — the two rules core's own fold applies, from the one shared definition.

    Args:
        components: Dict with component scores:
            {
                "hash_match": bool,
                "hash_score": float,
                "transcript_pass": bool,
                "transcript_score": float,
            }
        grading_config: Grading configuration from task description:
            {
                "combine_method": "all" | "weighted" | "any",
                "weights": {"state_checks": 1.0, "transcript_rules": 0.5},
                "pass_threshold": 1.0,
                "state_checks": {"hash_weight": 0.6}
            }

    Returns:
        The verdict, carrying the reason where the fold counted nothing.

    Raises:
        MissingComponentWeight: an evaluated component ``combine.weights`` declares no
            share for.
        ValueError: a hash verdict and a JSONPath score are both real and
            ``state_checks.hash_weight`` does not say how to fold them; or
            ``combine_method`` is missing or names no supported aggregation.
    """
    # Ahead of the zero-active-components return below, which never reaches the fold:
    # a request naming no supported aggregation must fail the grade rather than take
    # that path's verdict.
    method = validate_combine_method(
        grading_config.get("combine_method"), context="grading config combine_method"
    )
    weights = grading_config.get("weights", {})
    threshold = grading_config.get("pass_threshold", 1.0)

    # Determine which components are active (score >= 0 means evaluated)
    active_components: dict[str, float] = {}
    state_checks_slot = resolve_state_checks_component(
        hash_score=components.get("hash_score", -1.0),
        jsonpath_score=components.get("jsonpath_score", -1.0),
        db_probe_score=components.get("db_probe_score", -1.0),
        hash_weight=(grading_config.get("state_checks") or {}).get("hash_weight"),
    )
    if state_checks_slot.component is not None:
        active_components["state_checks"] = state_checks_slot.component
    for spec in GRADE_COMPONENTS:
        # state_checks is the composed slot resolved above; it has no single field here.
        if spec.runner_score_field is None:
            continue
        score = components.get(spec.runner_score_field, -1.0)
        if score >= 0:
            active_components[spec.name] = score

    shares = {name: require_component_weight(name, weights) for name in active_components}

    # A refusal task (empty golden_actions), a misconfigured pack and a deliberately
    # non-scoring one are three different answers, and the shared rule tells them apart.
    uncounted = resolve_uncounted_fold(
        scored=active_components,
        requested={
            spec.name
            for spec in GRADE_COMPONENTS
            if component_requested(spec, grading_config.get(spec.config_section))
        },
        weights=weights,
        method=method,
    )
    if uncounted is not None:
        if uncounted.reason:
            logger.warning("Grading counted nothing — failing: %s", uncounted.reason)
        return uncounted

    # Computed for every method, read only by ``weighted``: the shared dispatch
    # decides the aggregation and this substrate keeps its own mean.
    total_weight = sum(shares.values())
    weighted_sum = sum(score * shares[name] for name, score in active_components.items())
    score, binary_pass = combine_by_method(
        method=method,
        component_scores=active_components,
        weighted_mean=weighted_sum / total_weight if total_weight else 0.0,
        pass_threshold=threshold,
    )
    return FoldedGrade(score=score, binary_pass=binary_pass)


_JUDGE_SCORE_FIELD = runner_score_field("llm_judge")


@dataclass(frozen=True)
class RunnerTrialVerdict:
    """One trial's runner-side verdict: the two gates applied around the weighted fold.

    ``judge_component`` is the score the judge component carries *after* the required-criterion
    gate, which is what the wire grade and the reasons string report — not the weighted average
    the judge's own aggregate returned. ``reason`` is the fold's own sentence where it counted
    nothing, which no component's reasons would otherwise state.

    ``refusal`` is the fold's own discriminator: ``True`` when the fold declined to compose
    a grade (a declared component produced no verdict, or the scored components carry no
    weight), so the caller emits ``pb2.GradeTrialResponse(success=False, error=reason)``
    instead of writing the ``0.0`` verdict into a real Grade.
    """

    judge_component: float
    score: float
    binary_pass: bool
    reason: str | None
    refusal: bool


def compose_runner_trial_verdict(
    components: dict[str, Any],
    grading_config: dict[str, Any],
    *,
    judge_gate_failed: bool,
    trace_gate_failed: bool,
) -> RunnerTrialVerdict:
    """Fold ``components`` into a trial verdict, applying both gates around the combine.

    One rule, two gates, in the order they bind. A failed **required** rubric criterion is a
    hard fail of the judge *component*: its score is zeroed before the fold, so a high weighted
    average cannot rescue it and every downstream reader of the component sees the gate. A
    failed **trace** gate leaves the score alone and fails the trial outright. Either gate
    therefore fails the trial independently of ``pass_threshold`` and of how heavily any other
    component is weighted.

    ``components`` carries the judge's *raw* aggregate score under its runner field; the
    zeroing is this function's, so a caller reproducing a recorded verdict offline reaches the
    same verdict as the runner without repeating either gate. The core substrate composes
    independently (:mod:`~tolokaforge.core.grading.combine`) and never computes ``llm_judge``.

    Raises:
        ValueError: propagated from :func:`combine_grade_components` — an evaluated component
            with no declared weight, an undecidable ``state_checks`` fold, or a
            ``combine_method`` naming no supported aggregation.
    """
    folded = dict(components)
    if judge_gate_failed:
        folded[_JUDGE_SCORE_FIELD] = 0.0
    combined = combine_grade_components(folded, grading_config)
    return RunnerTrialVerdict(
        judge_component=folded.get(_JUDGE_SCORE_FIELD, -1.0),
        score=combined.score,
        binary_pass=combined.binary_pass and not (judge_gate_failed or trace_gate_failed),
        reason=combined.reason,
        refusal=combined.refusal,
    )


def build_grade_reasons(
    components: dict[str, Any],
    state_diff: dict[str, Any] | None = None,
    transcript_result: dict[str, Any] | None = None,
    judge_reasons: str | None = None,
    trace_checks_result: dict[str, Any] | None = None,
    golden_replay: GoldenReplayRecord | None = None,
    custom_checks_reasons: str | None = None,
) -> str:
    """
    Build human-readable reasons string for the grade.

    Args:
        components: Component scores dict
        state_diff: State diff if hash comparison failed
        transcript_result: Transcript evaluation result
        trace_checks_result: Trace checks evaluation result
        golden_replay: The golden replay behind the hash verdict, when one ran. An
            incomplete replay is named beside the verdict it produced, in the sentence
            the core engine emits too.
        custom_checks_reasons: The custom-checks suite's own account, rendered by
            :func:`~tolokaforge.core.grading.checks_helpers.custom_checks_reason`.
            Passed on the strength of the evaluator having something to say rather
            than on the component's score, so a suite that failed to run says why
            even though it scored nothing.

    Returns:
        The scored components' segments, joined — and empty where the trial scored
        none of them. A grade for such a trial is not silent: the fold decides it
        without reading a score and its own sentence says why, which the caller
        appends. A placeholder here would state the opposite of what a renderer
        omission means, and would be a second producer of the same account.
    """
    reasons = []

    # State checks reason — hash
    hash_score = components.get("hash_score", -1.0)
    if hash_score >= 0:
        if components.get("hash_match", False):
            reasons.append("State: hash match")
        else:
            if state_diff and state_diff.get("summary"):
                reasons.append(f"State: {state_diff['summary']}")
            else:
                reasons.append("State: hash mismatch")

    replay_reason = incomplete_replay_reason(golden_replay) if golden_replay is not None else None
    if replay_reason:
        reasons.append(replay_reason)

    # State checks reason — jsonpath file assertions
    jsonpath_score = components.get("jsonpath_score", -1.0)
    if jsonpath_score >= 0:
        jsonpath_reasons = components.get("jsonpath_reasons", "")
        if jsonpath_reasons:
            reasons.append(f"JSONPath: {jsonpath_reasons}")
        elif jsonpath_score == 1.0:
            reasons.append("JSONPath: all checks passed")
        else:
            reasons.append(f"JSONPath: score={jsonpath_score:.2f}")

    # State checks reason — db probes
    db_probe_score = components.get("db_probe_score", -1.0)
    if db_probe_score >= 0:
        db_probe_reasons = components.get("db_probe_reasons", "")
        if db_probe_reasons:
            reasons.append(f"DB probes: {db_probe_reasons}")
        elif db_probe_score == 1.0:
            reasons.append("DB probes: all probes passed")
        else:
            reasons.append(f"DB probes: score={db_probe_score:.2f}")

    # Transcript rules reason
    transcript_score = components.get("transcript_score", -1.0)
    if transcript_score >= 0:
        if transcript_result:
            details = transcript_result.get("details", [])
            failures = [d for d in details if not d.get("passed")]
            total = len(details)
            if not failures:
                reasons.append(f"Transcript: all {total} rules passed")
            else:
                # Name every failing sub-check: "2 of 5 failed" alone leaves the
                # author guessing which rule and why.
                failure_text = "; ".join(
                    str(d.get("message", d.get("rule_type"))) for d in failures
                )
                reasons.append(
                    f"Transcript: {len(failures)} of {total} rules failed — {failure_text}"
                )
        else:
            if components.get("transcript_pass", False):
                reasons.append("Transcript: passed")
            else:
                reasons.append("Transcript: failed")

    # Trace checks reason — the score and the route it was scored on, the gates
    # that shut, then every failing constraint by name. The gate and constraint
    # lines are the ones core's engine emits too, so a grade reads the same on
    # both substrates.
    trace_checks_score = components.get("trace_checks_score", -1.0)
    if trace_checks_score >= 0:
        trace_checks = trace_checks_result or {}
        route = trace_checks.get("winning_path") or ""
        reasons.append(
            f"Trace checks: score={trace_checks_score:.2f}" + (f" (route {route})" if route else "")
        )
        failed_gate_ids = trace_checks.get("failed_gate_ids") or []
        if failed_gate_ids:
            reasons.append(f"FAILED trace gates: {', '.join(failed_gate_ids)}")
        reasons.extend(
            f"Trace check {item['id']}: {item['message']}"
            for item in trace_checks.get("constraints", [])
            if not item["passed"]
        )

    # LLM judge reason
    llm_judge_score = components.get("llm_judge_score", -1.0)
    if llm_judge_score >= 0:
        if judge_reasons:
            reasons.append(f"Judge: score={llm_judge_score:.2f} ({judge_reasons})")
        else:
            reasons.append(f"Judge: score={llm_judge_score:.2f}")

    # Custom checks reason — registry order puts it last, and the segment is the
    # shared renderer's output verbatim so the two substrates carry one text.
    if custom_checks_reasons:
        reasons.append(custom_checks_reasons)

    return " | ".join(reasons)
