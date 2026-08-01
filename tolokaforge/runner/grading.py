"""
Grading Helper Functions for Runner Service

This module provides helper functions for the GradeTrial RPC implementation:
- compute_state_diff: Compute human-readable diff between two stable states
- evaluate_transcript_rules: Evaluate transcript rules against the trial's event timeline
- combine_grade_components: Combine component scores into final grade

See docs/GRPC_PROTOCOL.md for grading algorithm specification.
"""

import glob
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonpath_ng.ext import parse

from tolokaforge.core.grading.combine_method import (
    combine_by_method,
    validate_combine_method,
)
from tolokaforge.core.grading.grade_components import GRADE_COMPONENTS
from tolokaforge.core.grading.state_composition import (
    compose_state_checks_score,
    inert_hash_weight_reason,
)
from tolokaforge.core.grading.trace_timeline import (
    AttemptedCall,
    TrialTimeline,
    assistant_texts,
    attempted_calls,
)
from tolokaforge.core.hash import canonical_number
from tolokaforge.runner.grading_ledger import (
    COMMUNICATE_INFO_KEY,
    DISALLOW_REGEX_KEY,
    EVALUATED,
    MAX_TURNS_KEY,
    MIN_ASSISTANT_TURNS_KEY,
    MUST_CONTAIN_KEY,
    REQUIRED_ACTIONS_KEY,
    TOOL_EXPECTATIONS_KEY,
)
from tolokaforge.runner.models import (
    KeyAccountingRecord,
    StateDiff,
    TableDiff,
    ToolExecutionStatus,
    ToolExecutorIdentity,
    TranscriptEvaluationResult,
    TranscriptRuleResult,
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


def evaluate_transcript_rules(
    timeline: TrialTimeline,
    rules: dict[str, Any],
) -> TranscriptEvaluationResult:
    """
    Evaluate the author-facing ``TranscriptRulesConfig`` against the trial's
    event timeline.

    ``rules`` is a single ``TranscriptRulesConfig.model_dump()`` dict — the
    schema task authors actually write in ``grading.yaml``. It is decomposed
    into one independent sub-check per field entry, each of which produces a
    visible ``TranscriptRuleResult`` in ``details``:

    - ``must_contain`` (list[str]): each string must appear (case-insensitive
      substring) in some assistant message → one sub-check per string.
    - ``disallow_regex`` (list[str]): none of the regexes may match any
      assistant message → one sub-check per regex.
    - ``max_turns`` (int | None): the number of assistant turns must be within
      the limit → one sub-check when set.
    - ``min_assistant_turns`` (int | None): the trial must have produced at least
      this many assistant turns. A **gate on the whole component**, not a
      sub-check: an unmet floor appends one failing row and forces ``score`` to
      ``0.0``, and a met floor appends no row so it never dilutes the fraction.
    - ``tool_expectations`` ({required_tools, disallowed_tools}): one sub-check
      per declared tool. A required tool must have been called *successfully*; a
      disallowed tool must not have run at **any** status, because an attempted
      forbidden call is itself the violation. Both fail on a timeline that carries
      no records for a call the message view declared — whether it ran is then
      unknown, and a "did not run" reading would pass every forbidden call.
    - ``required_actions`` (list[RequiredAction]): each declared tool call must
      appear on the timeline, matched by ``tool_name`` + ``requestor`` and
      by the argument subset named in ``compare_args`` (``None`` = compare all
      declared args, ``[]`` = compare none) → one sub-check per action.
    - ``communicate_info`` (list[{info, required}]): each ``required`` info
      string must appear in an assistant message → one sub-check per required
      entry (non-required entries are advisory and not scored).

    Scoring: ``score`` is the fraction of sub-checks that passed — unless a
    declared ``min_assistant_turns`` floor is unmet, in which case it is ``0.0``
    whatever the other sub-checks did. ``passed`` is True iff every sub-check
    passed. The component score feeds
    ``combine_grade_components`` where ``pass_threshold`` is applied, so a
    fraction (rather than all-or-nothing) lets authors set partial-credit
    thresholds (e.g. ``pass_threshold: 0.75``). A config that produces no sub-check
    rows is a no-op pass with ``score=1.0`` — no fields set, or a met floor as the
    only declared rule, leaves nothing that could have been violated.

    Unknown / missing data is surfaced as a FAILING sub-check, never silently
    passed (AGENTS.md: surface failures explicitly).

    Args:
        timeline: The trial's event timeline, built by
            :func:`~tolokaforge.core.grading.trace_timeline.build_trial_timeline`
        rules: A single ``TranscriptRulesConfig.model_dump()`` dict

    Returns:
        TranscriptEvaluationResult with passed, score, per-sub-check details, and
        the author keys this call decomposed
    """
    details: list[TranscriptRuleResult] = []
    accounted_keys: dict[str, KeyAccountingRecord] = {}

    must_contain: list[str] = rules.get("must_contain", []) or []
    disallow_regex: list[str] = rules.get("disallow_regex", []) or []
    max_turns: int | None = rules.get("max_turns")
    min_assistant_turns: int | None = rules.get("min_assistant_turns")
    tool_expectations: dict[str, Any] = rules.get("tool_expectations") or {}
    required_tools: list[str] = tool_expectations.get("required_tools") or []
    disallowed_tools: list[str] = tool_expectations.get("disallowed_tools") or []
    required_actions: list[dict[str, Any]] = rules.get("required_actions", []) or []
    communicate_info: list[dict[str, Any]] = rules.get("communicate_info", []) or []

    assistant_messages = assistant_texts(timeline)
    calls = attempted_calls(timeline)
    records_present = timeline.records_present

    unmet_floor: TranscriptRuleResult | None = None
    if min_assistant_turns is not None:
        accounted_keys[MIN_ASSISTANT_TURNS_KEY] = EVALUATED
        unmet_floor = _unmet_activity_floor(min_assistant_turns, len(assistant_messages))
        if unmet_floor is not None:
            details.append(unmet_floor)

    if must_contain:
        accounted_keys[MUST_CONTAIN_KEY] = EVALUATED
        for text in must_contain:
            details.append(_check_must_contain(text, assistant_messages))

    if disallow_regex:
        accounted_keys[DISALLOW_REGEX_KEY] = EVALUATED
        for pattern in disallow_regex:
            details.append(_check_disallow_regex(pattern, assistant_messages))

    if max_turns is not None:
        accounted_keys[MAX_TURNS_KEY] = EVALUATED
        details.append(_check_max_turns(max_turns, len(assistant_messages)))

    if required_tools or disallowed_tools:
        accounted_keys[TOOL_EXPECTATIONS_KEY] = EVALUATED
        for tool_name in required_tools:
            details.append(_check_required_tool(tool_name, calls, records_present=records_present))
        for tool_name in disallowed_tools:
            details.append(
                _check_disallowed_tool(tool_name, calls, records_present=records_present)
            )

    if required_actions:
        accounted_keys[REQUIRED_ACTIONS_KEY] = EVALUATED
        for action in required_actions:
            details.append(_check_required_action(action, calls, records_present=records_present))

    if communicate_info:
        accounted_keys[COMMUNICATE_INFO_KEY] = EVALUATED
        for info in communicate_info:
            check = _check_communicate_info(info, assistant_messages)
            if check is not None:
                details.append(check)

    if not details:
        # No sub-check rows — nothing can be violated. Either nothing was declared,
        # or a met activity floor was the only declaration and emits no row.
        return TranscriptEvaluationResult(
            passed=True, score=1.0, details=[], accounted_keys=accounted_keys
        )

    passed_count = sum(1 for d in details if d.passed)
    total_count = len(details)
    # An unmet activity floor vetoes the whole component: a trial that did not act
    # makes the other sub-checks' verdicts evidence of nothing, and a fraction
    # would let any pass_threshold at or below it swallow the violation.
    score = 0.0 if unmet_floor is not None else passed_count / total_count
    all_passed = passed_count == total_count

    return TranscriptEvaluationResult(
        passed=all_passed, score=score, details=details, accounted_keys=accounted_keys
    )


# Map the RequiredAction.requestor vocabulary ("assistant"/"user", the
# author-facing role names) onto the recorded executor identity. They name the
# same actor.
_REQUESTOR_TO_EXECUTOR: dict[str, ToolExecutorIdentity] = {
    "assistant": ToolExecutorIdentity.AGENT,
    "user": ToolExecutorIdentity.USER,
}


_UNRECORDED = (
    "the trial carries no tool-call record, so nothing knows whether the calls it "
    "declared ran. A timeline rebuilt from a recorded bundle is in that state, "
    "because tool_log is not written to trajectory.yaml"
)


def _declared(calls: Sequence[AttemptedCall], tool_name: str) -> list[AttemptedCall]:
    """Every call to ``tool_name`` the agent asked for, whether or not it ran."""
    return [call for call in calls if call.tool_name == tool_name]


def _executed(calls: Sequence[AttemptedCall], tool_name: str) -> list[AttemptedCall]:
    """The calls to ``tool_name`` that actually ran, so carry an outcome.

    Separates ran from did-not-run only while the timeline carries records.
    Without them every call reads as un-run, so callers ask
    :func:`_outcome_unknown` first instead of reading absent evidence as a fact.
    """
    return [call for call in _declared(calls, tool_name) if call.status is not None]


def _outcome_unknown(
    calls: Sequence[AttemptedCall], tool_name: str, *, records_present: bool
) -> bool:
    """Whether the timeline can say if the declared calls to ``tool_name`` ran.

    A record can only name a call the message view declared, so a tool the agent
    never asked for never ran — knowable from the message view alone. Whatever it
    did ask for is unknown the moment the record view is missing.
    """
    return not records_present and bool(_declared(calls, tool_name))


def _check_must_contain(text: str, assistant_messages: Sequence[str]) -> TranscriptRuleResult:
    """Each required string must appear (case-insensitive) in some assistant message."""
    needle = text.lower()
    found = any(needle in content.lower() for content in assistant_messages)
    return TranscriptRuleResult(
        rule_type="must_contain",
        rule={"must_contain": text},
        passed=found,
        message=(
            f"Found required text {text!r} in an assistant message"
            if found
            else f"Required text {text!r} not found in any assistant message"
        ),
    )


def _check_disallow_regex(pattern: str, assistant_messages: Sequence[str]) -> TranscriptRuleResult:
    """No assistant message may match the disallowed regex.

    An invalid regex is an author error — surface it as a FAIL rather than
    silently treating it as 'no match'.
    """
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return TranscriptRuleResult(
            rule_type="disallow_regex",
            rule={"disallow_regex": pattern},
            passed=False,
            message=f"Invalid disallow_regex {pattern!r}: {exc}",
        )

    for content in assistant_messages:
        if compiled.search(content):
            return TranscriptRuleResult(
                rule_type="disallow_regex",
                rule={"disallow_regex": pattern},
                passed=False,
                message=f"Disallowed pattern {pattern!r} matched an assistant message",
            )
    return TranscriptRuleResult(
        rule_type="disallow_regex",
        rule={"disallow_regex": pattern},
        passed=True,
        message=f"Disallowed pattern {pattern!r} did not match any assistant message",
    )


def _check_max_turns(max_turns: int, turn_count: int) -> TranscriptRuleResult:
    """Assistant turn count must be within the limit.

    A "turn" is one assistant generation. This counts the agent's responses
    rather than user messages so the limit caps the agent's activity, which is
    what authors intend to bound.
    """
    within = turn_count <= max_turns
    return TranscriptRuleResult(
        rule_type="max_turns",
        rule={"max_turns": max_turns},
        passed=within,
        message=(
            f"Assistant turn count {turn_count} within limit of {max_turns}"
            if within
            else f"Assistant turn count {turn_count} exceeds limit of {max_turns}"
        ),
    )


def _unmet_activity_floor(min_assistant_turns: int, turn_count: int) -> TranscriptRuleResult | None:
    """The failing row for an unmet activity floor, or ``None`` when it is met.

    A met floor emits no row at all. The floor is a gate on the whole component,
    so a passing row would dilute the fraction the other sub-checks produce.
    """
    if turn_count >= min_assistant_turns:
        return None
    return TranscriptRuleResult(
        rule_type="min_assistant_turns",
        rule={"min_assistant_turns": min_assistant_turns},
        passed=False,
        message=(
            f"Assistant turn count {turn_count} below min_assistant_turns of {min_assistant_turns}"
        ),
    )


def _check_required_tool(
    tool_name: str, calls: Sequence[AttemptedCall], *, records_present: bool
) -> TranscriptRuleResult:
    """A required tool must have been called successfully at least once.

    Same "a failed call did not happen" rule ``_check_required_action`` applies:
    an errored call did not accomplish the work the author required.
    """
    if _outcome_unknown(calls, tool_name, records_present=records_present):
        return TranscriptRuleResult(
            rule_type="required_tool",
            rule={"required_tool": tool_name},
            passed=False,
            message=f"Required tool {tool_name!r}: {_UNRECORDED}",
        )
    called = any(call.status is ToolExecutionStatus.SUCCESS for call in _executed(calls, tool_name))
    return TranscriptRuleResult(
        rule_type="required_tool",
        rule={"required_tool": tool_name},
        passed=called,
        message=(
            f"Required tool {tool_name!r} was called successfully"
            if called
            else f"Required tool {tool_name!r} was never called successfully"
        ),
    )


def _check_disallowed_tool(
    tool_name: str, calls: Sequence[AttemptedCall], *, records_present: bool
) -> TranscriptRuleResult:
    """A disallowed tool must not have run, at any status.

    Status-insensitive on purpose: attempting a forbidden call is the violation,
    so an errored attempt fails the check just like a successful one. A call the
    agent declared on a terminating turn never reached the substrate and is not
    counted; naming intent as a violation is a matcher question, tracked on #678.
    That exclusion needs the record view, so a declared call the timeline holds no
    records for fails the check instead of reading as one that never ran.
    """
    if _outcome_unknown(calls, tool_name, records_present=records_present):
        return TranscriptRuleResult(
            rule_type="disallowed_tool",
            rule={"disallowed_tool": tool_name},
            passed=False,
            message=f"Disallowed tool {tool_name!r}: {_UNRECORDED}",
        )
    offending = _executed(calls, tool_name)
    if not offending:
        message = f"Disallowed tool {tool_name!r} was never called"
    else:
        statuses = ", ".join(sorted({call.status.value for call in offending if call.status}))
        message = (
            f"Disallowed tool {tool_name!r} was called {len(offending)} "
            f"time(s) (statuses: {statuses})"
        )
    return TranscriptRuleResult(
        rule_type="disallowed_tool",
        rule={"disallowed_tool": tool_name},
        passed=not offending,
        message=message,
    )


def _check_required_action(
    action: dict[str, Any], calls: Sequence[AttemptedCall], *, records_present: bool
) -> TranscriptRuleResult:
    """A declared tool call must appear on the timeline.

    Matching:
    - ``tool_name`` must match exactly;
    - ``requestor`` ("assistant"/"user") must match the call's ``executor``
      ("agent"/"user");
    - the call must have ``status == "success"`` (a failed call did not happen);
    - arguments named by ``compare_args`` must match the declared values.
      ``compare_args is None`` compares every declared argument; ``[]`` compares
      none (presence of the tool call is enough).
    """
    action_id = action.get("action_id", "")
    tool_name = action.get("tool_name", "")
    requestor = action.get("requestor", "")
    declared_args = action.get("arguments", {}) or {}
    compare_args = action.get("compare_args")

    expected_executor = _REQUESTOR_TO_EXECUTOR.get(requestor)

    if compare_args is None:
        keys_to_compare = list(declared_args.keys())
    else:
        keys_to_compare = list(compare_args)

    label = f"required_action {action_id!r} (tool={tool_name!r}, requestor={requestor!r})"

    if not tool_name:
        return TranscriptRuleResult(
            rule_type="required_action",
            rule=action,
            passed=False,
            message=f"{label}: no tool_name declared",
        )

    if _outcome_unknown(calls, tool_name, records_present=records_present):
        return TranscriptRuleResult(
            rule_type="required_action",
            rule=action,
            passed=False,
            message=f"{label}: {_UNRECORDED}",
        )

    for call in calls:
        if call.tool_name != tool_name:
            continue
        if expected_executor is not None and call.executor is not expected_executor:
            continue
        if call.status is not ToolExecutionStatus.SUCCESS:
            continue
        if all(call.arguments.get(k) == declared_args.get(k) for k in keys_to_compare):
            return TranscriptRuleResult(
                rule_type="required_action",
                rule=action,
                passed=True,
                message=f"{label}: matched a successful tool call",
            )

    return TranscriptRuleResult(
        rule_type="required_action",
        rule=action,
        passed=False,
        message=(
            f"{label}: no matching successful tool call found"
            + (f" with args {keys_to_compare}" if keys_to_compare else "")
        ),
    )


def _check_communicate_info(
    info: dict[str, Any], assistant_messages: Sequence[str]
) -> TranscriptRuleResult | None:
    """A required info string must appear in an assistant message.

    Returns ``None`` (no sub-check, not scored) for non-required info — those
    are advisory hints, not gating requirements.
    """
    text = info.get("info", "")
    required = info.get("required", True)
    if not required:
        return None

    needle = text.lower()
    found = any(needle in content.lower() for content in assistant_messages)
    return TranscriptRuleResult(
        rule_type="communicate_info",
        rule=info,
        passed=found,
        message=(
            f"Communicated required info {text!r}"
            if found
            else f"Required info {text!r} was not communicated to the user"
        ),
    )


def evaluate_jsonpath_file_checks(
    checks: list[dict[str, Any]],
) -> tuple[float, str]:
    """
    Evaluate jsonpath file assertions against the Runner container's filesystem.

    This evaluator only understands ``path_glob:`` + ``contains_ci:`` (file-content
    checks). It does not handle ``path:``-style JSONPath assertions on env state
    (those are evaluated host-side by ``StateChecker.check_jsonpaths``). An
    assertion missing ``path_glob:`` is treated as **failed** with an actionable
    reason — previously such assertions were silently skipped, which made
    misrouted assertions vanish from grading without notice.

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
            # Fail loud — historically this branch silently skipped (effectively
            # counting as not-passed in the score, but presenting as SKIP in
            # the reasons text, so misrouted assertions were invisible). Name
            # what the evaluator actually accepts so the author can fix it.
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


def _contains(haystack: Any, needle: Any, ci: bool = False) -> bool:
    if isinstance(haystack, str) and isinstance(needle, str):
        return needle.casefold() in haystack.casefold() if ci else needle in haystack
    if isinstance(haystack, list | tuple | set):
        return any(_contains(item, needle, ci=ci) for item in haystack)
    if isinstance(haystack, dict):
        return any(_contains(value, needle, ci=ci) for value in haystack.values())
    return haystack == needle


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
            if op_name == "contains" and _contains(value, expected):
                found = True
                break
            if op_name == "contains_ci" and _contains(value, expected, ci=True):
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
    """Fold the runner's three state sources into one ``state_checks`` score.

    Translates the runner's ``-1.0``-means-not-evaluated sentinel into the ``None``
    the shared composer reads. ``db_probes`` is the sole state source for the tasks
    that declare it (mixing it with hash/jsonpath in one task is out of scope), so
    its score fills the slot outright and hides whatever the other two produced —
    which is why the probe branch reports the weight as unconsulted too.

    Raises ``ValueError`` when a hash verdict and a JSONPath score are both real and
    no ``hash_weight`` says how to fold them.
    """
    probes_decide = db_probe_score >= 0
    hash_source = None if probes_decide or hash_score < 0 else hash_score
    jsonpath_source = None if probes_decide or jsonpath_score < 0 else jsonpath_score
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
) -> tuple[float, bool]:
    """
    Combine component scores into final grade.

    Supports combination methods:
    - "all": All components must pass (score >= threshold)
    - "weighted": Weighted average of component scores
    - "any": Any component passing is sufficient

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
        Tuple of (score: float, binary_pass: bool)

    Raises:
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

    # If no components are active but grading was configured, fail explicitly.
    # This prevents refusal tasks (empty golden_actions) or misconfigured
    # grading from silently passing with score=1.0. A component counts as
    # configured only when it is both weighted AND carries a config section
    # (a section present in the model by default says nothing about the author).
    if not active_components:
        configured = {
            spec.name
            for spec in GRADE_COMPONENTS
            if spec.name in weights and grading_config.get(spec.config_section) is not None
        }

        if configured:
            logger.warning(
                "Grading configured for %s but no components were evaluated — failing",
                configured,
            )
            return 0.0, False
        # Truly no grading configured at all — pass by default
        return 1.0, True

    # Computed for every method, read only by ``weighted``: the shared dispatch
    # decides the aggregation and this substrate keeps its own mean.
    total_weight = 0.0
    weighted_sum = 0.0
    for component_name, score in active_components.items():
        weight = weights.get(component_name, 1.0)
        weighted_sum += score * weight
        total_weight += weight

    if total_weight > 0:
        weighted_mean = weighted_sum / total_weight
    else:
        weighted_mean = 1.0

    return combine_by_method(
        method=method,
        component_scores=active_components,
        weighted_mean=weighted_mean,
        pass_threshold=threshold,
    )


def build_grade_reasons(
    components: dict[str, Any],
    state_diff: dict[str, Any] | None = None,
    transcript_result: dict[str, Any] | None = None,
    judge_reasons: str | None = None,
) -> str:
    """
    Build human-readable reasons string for the grade.

    Args:
        components: Component scores dict
        state_diff: State diff if hash comparison failed
        transcript_result: Transcript evaluation result

    Returns:
        Human-readable reasons string
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

    # Trace checks reason
    trace_checks_score = components.get("trace_checks_score", -1.0)
    if trace_checks_score >= 0:
        reasons.append(f"Trace checks: score={trace_checks_score:.2f}")

    # LLM judge reason
    llm_judge_score = components.get("llm_judge_score", -1.0)
    if llm_judge_score >= 0:
        if judge_reasons:
            reasons.append(f"Judge: score={llm_judge_score:.2f} ({judge_reasons})")
        else:
            reasons.append(f"Judge: score={llm_judge_score:.2f}")

    return " | ".join(reasons) if reasons else "No grading components evaluated"
