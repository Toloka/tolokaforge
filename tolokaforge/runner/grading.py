"""
Grading Helper Functions for Runner Service

This module provides helper functions for the GradeTrial RPC implementation:
- compute_state_diff: Compute human-readable diff between two stable states
- evaluate_transcript_rules: Evaluate transcript rules against conversation history
- combine_grade_components: Combine component scores into final grade

See docs/GRPC_PROTOCOL.md for grading algorithm specification.
"""

import glob
import logging
import re
from pathlib import Path
from typing import Any

from jsonpath_ng.ext import parse

from tolokaforge.runner.models import (
    StateDiff,
    TableDiff,
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

    def _make_hashable(value: Any) -> Any:
        """Make a value hashable for comparison."""
        if isinstance(value, dict):
            return tuple(sorted((k, _make_hashable(v)) for k, v in value.items()))
        elif isinstance(value, list):
            return tuple(_make_hashable(v) for v in value)
        return value

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

    # Match on the first shared ID field (most specific)
    for field in id_fields:
        if record1[field] is not None and record2[field] is not None:
            return record1[field] == record2[field]

    # Fallback: check if they share at least 50% of fields with same values
    if not common_keys:
        return False

    matching_values = sum(1 for f in common_keys if record1[f] == record2[f])
    return matching_values >= len(common_keys) * 0.5


def _get_field_diffs(expected: dict[str, Any], actual: dict[str, Any]) -> list[dict[str, Any]]:
    """Get list of field differences between two records."""
    diffs = []
    all_fields = set(expected.keys()) | set(actual.keys())

    for field in sorted(all_fields):
        exp_val = expected.get(field)
        act_val = actual.get(field)
        if exp_val != act_val:
            diffs.append({"field": field, "expected": exp_val, "actual": act_val})

    return diffs


def evaluate_transcript_rules(
    messages: list[dict[str, Any]],
    tool_history: list[dict[str, Any]],
    rules: list[dict[str, Any]],
) -> TranscriptEvaluationResult:
    """
    Evaluate transcript rules against conversation history.

    Supports rule types:
    - must_contain: Check if any assistant message contains the text
    - must_not_contain: Check no assistant message contains the text
    - required_tool_call: Check tool_history has this tool call
    - max_turns: Count turns, verify under limit

    Args:
        messages: LLM conversation messages (role, content)
        tool_history: List of tool call records from trial context
        rules: List of rule definitions from grading config

    Returns:
        TranscriptEvaluationResult with pass, score, and details
    """
    details: list[TranscriptRuleResult] = []
    rules = _expand_transcript_rule_bundles(rules)

    if not rules:
        return TranscriptEvaluationResult(passed=True, score=1.0, details=[])

    passed_count = 0
    total_count = len(rules)

    for rule in rules:
        rule_type = rule.get("type", "")
        rule_result = _evaluate_single_rule(rule_type, rule, messages, tool_history)
        details.append(rule_result)

        if rule_result.passed:
            passed_count += 1

    # Calculate score as fraction of rules passed
    score = passed_count / total_count if total_count > 0 else 1.0
    all_passed = passed_count == total_count

    return TranscriptEvaluationResult(passed=all_passed, score=score, details=details)


def _expand_transcript_rule_bundles(
    rules: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Expand a serialized ``TranscriptRulesConfig`` into executable rules.

    The Runner receives the typed transcript configuration as one mapping, while
    :func:`evaluate_transcript_rules` historically expected a list of
    ``{"type": ...}`` records. Treating that mapping as an unknown rule made
    every native transcript contract pass without evaluation. Explicit legacy
    rules remain unchanged; typed bundles are expanded fail-closed.
    """
    expanded: list[dict[str, Any]] = []
    for rule in rules:
        if rule.get("type"):
            expanded.append(rule)
            continue

        recognized = False
        for text in rule.get("must_contain", []) or []:
            expanded.append({"type": "must_contain", "text": text})
            recognized = True
        for pattern in rule.get("disallow_regex", []) or []:
            expanded.append(
                {
                    "type": "must_not_contain",
                    "text": pattern,
                    "regex": True,
                }
            )
            recognized = True

        max_turns = rule.get("max_turns")
        if max_turns is not None:
            expanded.append(
                {
                    "type": "max_turns",
                    "max": max_turns,
                    "count_method": "assistant_messages",
                }
            )
            recognized = True

        for action in rule.get("required_actions", []) or []:
            arguments = action.get("arguments", {}) or {}
            compare_args = action.get("compare_args")
            if compare_args is not None:
                arguments = {key: arguments.get(key) for key in compare_args}
            requestor = action.get("requestor")
            tool_name = action.get("tool_name") or action.get("name", "")
            expanded.append(
                {
                    "type": "required_tool_call",
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "executor": (
                        "agent"
                        if requestor == "assistant"
                        else "user"
                        if requestor == "user"
                        else None
                    ),
                    "action_id": action.get("action_id", ""),
                }
            )
            recognized = True

        for item in rule.get("communicate_info", []) or []:
            if item.get("required", True):
                expanded.append({"type": "must_contain", "text": item.get("info", "")})
                recognized = True

        expectations = rule.get("tool_expectations") or {}
        for tool_name in expectations.get("required_tools", []) or []:
            expanded.append({"type": "required_tool_call", "tool_name": tool_name})
            recognized = True
        for tool_name in expectations.get("disallowed_tools", []) or []:
            expanded.append({"type": "forbidden_tool_call", "tool_name": tool_name})
            recognized = True

        if not recognized:
            expanded.append(
                {
                    "type": "invalid_transcript_rule",
                    "raw_rule": rule,
                }
            )
    return expanded


def _evaluate_single_rule(
    rule_type: str,
    rule: dict[str, Any],
    messages: list[dict[str, Any]],
    tool_history: list[dict[str, Any]],
) -> TranscriptRuleResult:
    """Evaluate a single transcript rule."""
    if rule_type == "must_contain":
        result_dict = _evaluate_must_contain(rule, messages)
    elif rule_type == "must_not_contain":
        result_dict = _evaluate_must_not_contain(rule, messages)
    elif rule_type == "required_tool_call":
        result_dict = _evaluate_required_tool_call(rule, tool_history)
    elif rule_type == "forbidden_tool_call":
        required = _evaluate_required_tool_call(
            {**rule, "min_calls": 1},
            tool_history,
        )
        result_dict = {
            "passed": not required["passed"],
            "message": (
                f"Forbidden {required['message']}"
                if required["passed"]
                else f"Forbidden tool '{rule.get('tool_name', '')}' was not called"
            ),
        }
    elif rule_type == "max_turns":
        result_dict = _evaluate_max_turns(rule, messages)
    else:
        result_dict = {"passed": False, "message": f"Unknown rule type: {rule_type}"}

    return TranscriptRuleResult(
        rule_type=rule_type,
        rule=rule,
        passed=result_dict["passed"],
        message=result_dict["message"],
    )


def _evaluate_must_contain(rule: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Check if any assistant message contains the required text.

    Rule format:
    {
        "type": "must_contain",
        "text": "string to find",
        "case_sensitive": false,  # optional, default false
        "regex": false            # optional, if true treat text as regex
    }
    """
    text = rule.get("text", "")
    case_sensitive = rule.get("case_sensitive", False)
    is_regex = rule.get("regex", False)

    if not text:
        return {"passed": True, "message": "No text specified, rule passes"}

    # Get all assistant messages
    assistant_messages = [m.get("content", "") for m in messages if m.get("role") == "assistant"]

    for content in assistant_messages:
        if content is None:
            continue

        if is_regex:
            flags = 0 if case_sensitive else re.IGNORECASE
            if re.search(text, content, flags):
                return {"passed": True, "message": f"Found pattern '{text}' in assistant message"}
        else:
            search_text = text if case_sensitive else text.lower()
            search_content = content if case_sensitive else content.lower()
            if search_text in search_content:
                return {"passed": True, "message": f"Found '{text}' in assistant message"}

    return {"passed": False, "message": f"Text '{text}' not found in any assistant message"}


def _evaluate_must_not_contain(
    rule: dict[str, Any], messages: list[dict[str, Any]]
) -> dict[str, Any]:
    """
    Check that no assistant message contains the forbidden text.

    Rule format:
    {
        "type": "must_not_contain",
        "text": "string to avoid",
        "case_sensitive": false,  # optional, default false
        "regex": false            # optional, if true treat text as regex
    }
    """
    text = rule.get("text", "")
    case_sensitive = rule.get("case_sensitive", False)
    is_regex = rule.get("regex", False)

    if not text:
        return {"passed": True, "message": "No text specified, rule passes"}

    # Get all assistant messages
    assistant_messages = [m.get("content", "") for m in messages if m.get("role") == "assistant"]

    for content in assistant_messages:
        if content is None:
            continue

        if is_regex:
            flags = 0 if case_sensitive else re.IGNORECASE
            if re.search(text, content, flags):
                return {
                    "passed": False,
                    "message": f"Found forbidden pattern '{text}' in assistant message",
                }
        else:
            search_text = text if case_sensitive else text.lower()
            search_content = content if case_sensitive else content.lower()
            if search_text in search_content:
                return {
                    "passed": False,
                    "message": f"Found forbidden text '{text}' in assistant message",
                }

    return {"passed": True, "message": f"Text '{text}' not found (as expected)"}


def _evaluate_required_tool_call(
    rule: dict[str, Any], tool_history: list[dict[str, Any]]
) -> dict[str, Any]:
    """
    Check that a specific tool was called with optional argument matching.

    Rule format:
    {
        "type": "required_tool_call",
        "tool_name": "book_reservation",
        "arguments": {"user_id": "mia_li_3668"},  # optional, partial match
        "min_calls": 1,                           # optional, default 1
        "executor": "agent"                       # optional, default any
    }
    """
    tool_name = rule.get("tool_name", "")
    required_args = rule.get("arguments", {})
    min_calls = rule.get("min_calls", 1)
    required_executor = rule.get("executor")

    if not tool_name:
        return {"passed": False, "message": "Required tool rule has no tool_name"}

    matching_calls = 0

    for call in tool_history:
        # Check tool name
        if call.get("tool_name") != tool_name:
            continue

        # Check executor if specified
        if required_executor and call.get("executor") != required_executor:
            continue

        # Check arguments if specified (partial match)
        if required_args:
            call_args = call.get("arguments", {})
            args_match = all(call_args.get(k) == v for k, v in required_args.items())
            if not args_match:
                continue

        # Check status (only count successful calls)
        if call.get("status") == "success":
            matching_calls += 1

    if matching_calls >= min_calls:
        return {
            "passed": True,
            "message": f"Tool '{tool_name}' called {matching_calls} times (required: {min_calls})",
        }
    else:
        return {
            "passed": False,
            "message": f"Tool '{tool_name}' called {matching_calls} times (required: {min_calls})",
        }


def _evaluate_max_turns(rule: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Check that conversation doesn't exceed maximum turns.

    A "turn" is typically counted as one user message + one assistant response.

    Rule format:
    {
        "type": "max_turns",
        "max": 10,
        "count_method": "user_messages"  # optional: "user_messages", "assistant_messages", "exchanges"
    }
    """
    max_turns = rule.get("max", 10)
    count_method = rule.get("count_method", "user_messages")

    if count_method == "user_messages":
        turn_count = sum(1 for m in messages if m.get("role") == "user")
    elif count_method == "assistant_messages":
        turn_count = sum(1 for m in messages if m.get("role") == "assistant")
    elif count_method == "exchanges":
        # Count pairs of user + assistant messages
        user_count = sum(1 for m in messages if m.get("role") == "user")
        assistant_count = sum(1 for m in messages if m.get("role") == "assistant")
        turn_count = min(user_count, assistant_count)
    else:
        # Default to counting all non-system messages
        turn_count = sum(1 for m in messages if m.get("role") in ("user", "assistant"))

    if turn_count <= max_turns:
        return {
            "passed": True,
            "message": f"Turn count {turn_count} within limit of {max_turns}",
        }
    else:
        return {
            "passed": False,
            "message": f"Turn count {turn_count} exceeds limit of {max_turns}",
        }


def evaluate_jsonpath_file_checks(
    checks: list[dict[str, Any]],
    workspace_path: str = "/work",
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
            resolved_pattern = str(
                Path(workspace_path) / resolved_pattern[len("/env/fs/agent-visible/") :]
            )
        elif resolved_pattern == "/env/fs/agent-visible":
            resolved_pattern = workspace_path
        elif resolved_pattern.startswith("/work/"):
            resolved_pattern = str(Path(workspace_path) / resolved_pattern[len("/work/") :])
        elif resolved_pattern == "/work":
            resolved_pattern = workspace_path

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


def evaluate_jsonpath_checks(
    checks: list[dict[str, Any]],
    state: dict[str, Any] | None = None,
    workspace_path: str = "/work",
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
        file_score, file_reasons = evaluate_jsonpath_file_checks(
            file_checks, workspace_path=workspace_path
        )
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
                "pass_threshold": 1.0
            }

    Returns:
        Tuple of (score: float, binary_pass: bool)
    """
    method = grading_config.get("combine_method", "all")
    weights = grading_config.get("weights", {})
    component_minimums = grading_config.get("component_minimums", {})
    threshold = grading_config.get("pass_threshold", 1.0)

    # Extract component scores
    hash_score = components.get("hash_score", -1.0)
    jsonpath_score = components.get("jsonpath_score", -1.0)
    transcript_score = components.get("transcript_score", -1.0)

    # Determine which components are active (score >= 0 means evaluated)
    active_components: dict[str, float] = {}
    # state_checks: combine hash and jsonpath scores if both are available
    if hash_score >= 0 and jsonpath_score >= 0:
        # Both evaluated — use product for strictness
        active_components["state_checks"] = hash_score * jsonpath_score
    elif hash_score >= 0:
        active_components["state_checks"] = hash_score
    elif jsonpath_score >= 0:
        active_components["state_checks"] = jsonpath_score
    if transcript_score >= 0:
        active_components["transcript_rules"] = transcript_score

    # LLM judge
    llm_judge_score = components.get("llm_judge_score", -1.0)
    if llm_judge_score >= 0:
        active_components["llm_judge"] = llm_judge_score

    # Test-execution verifiers expose their deterministic reward as a
    # custom-checks component so it can be combined with an optional judge.
    custom_checks_score = components.get("custom_checks_score", -1.0)
    if custom_checks_score >= 0:
        active_components["custom_checks"] = custom_checks_score

    # If no components are active but grading was configured, fail explicitly.
    # This prevents refusal tasks (empty golden_actions) or misconfigured
    # grading from silently passing with score=1.0.
    #
    # A component is "actually configured" when:
    #   1. It appears in weights, AND
    #   2. Its config section exists in grading_config (not just a model default)
    if not active_components:
        actually_configured: set[str] = set()
        if "state_checks" in weights and grading_config.get("state_checks") is not None:
            actually_configured.add("state_checks")
        if "transcript_rules" in weights and grading_config.get("transcript_rules") is not None:
            actually_configured.add("transcript_rules")
        if "llm_judge" in weights and grading_config.get("llm_judge") is not None:
            actually_configured.add("llm_judge")
        if "custom_checks" in weights and grading_config.get("grading_method") == "test_execution":
            actually_configured.add("custom_checks")

        if actually_configured:
            logger.warning(
                "Grading configured for %s but no components were evaluated — failing",
                actually_configured,
            )
            return 0.0, False
        # Truly no grading configured at all — pass by default
        return 1.0, True

    minimums_pass = True
    for component_name, minimum in component_minimums.items():
        score = active_components.get(component_name)
        if score is None or score < float(minimum):
            minimums_pass = False
            logger.info(
                "Grading component %s=%s did not meet its minimum %.3f",
                component_name,
                "not evaluated" if score is None else f"{score:.3f}",
                float(minimum),
            )

    if method == "all":
        # All components must pass (score >= threshold)
        all_pass = all(score >= threshold for score in active_components.values())
        # Score is minimum of all component scores
        final_score = min(active_components.values())
        return final_score, all_pass and minimums_pass

    elif method == "weighted":
        # Weighted average of component scores
        total_weight = 0.0
        weighted_sum = 0.0

        for component_name, score in active_components.items():
            weight = weights.get(component_name, 1.0)
            weighted_sum += score * weight
            total_weight += weight

        if total_weight > 0:
            final_score = weighted_sum / total_weight
        else:
            final_score = 1.0

        binary_pass = final_score >= threshold and minimums_pass
        return final_score, binary_pass

    elif method == "any":
        # Any component passing is sufficient
        any_pass = any(score >= threshold for score in active_components.values())
        # Score is maximum of all component scores
        final_score = max(active_components.values())
        return final_score, any_pass and minimums_pass

    else:
        # Unknown method - default to "all" behavior
        logger.warning(f"Unknown combine_method '{method}', defaulting to 'all'")
        all_pass = all(score >= threshold for score in active_components.values())
        final_score = min(active_components.values())
        return final_score, all_pass and minimums_pass


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

    # Transcript rules reason
    transcript_score = components.get("transcript_score", -1.0)
    if transcript_score >= 0:
        if transcript_result:
            passed = sum(1 for d in transcript_result.get("details", []) if d.get("passed"))
            total = len(transcript_result.get("details", []))
            if passed == total:
                reasons.append(f"Transcript: all {total} rules passed")
            else:
                failed = total - passed
                reasons.append(f"Transcript: {failed} of {total} rules failed")
        else:
            if components.get("transcript_pass", False):
                reasons.append("Transcript: passed")
            else:
                reasons.append("Transcript: failed")

    # LLM judge reason
    llm_judge_score = components.get("llm_judge_score", -1.0)
    if llm_judge_score >= 0:
        if judge_reasons:
            reasons.append(f"Judge: score={llm_judge_score:.2f} ({judge_reasons})")
        else:
            reasons.append(f"Judge: score={llm_judge_score:.2f}")

    custom_checks_score = components.get("custom_checks_score", -1.0)
    if custom_checks_score >= 0:
        reasons.append(f"Test execution: reward={custom_checks_score:.4f}")

    return " | ".join(reasons) if reasons else "No grading components evaluated"


# ---------------------------------------------------------------------------
# LLM judge evaluator
# ---------------------------------------------------------------------------


def evaluate_llm_judge(
    llm_judge_config: dict[str, Any],
    llm_messages: list[dict[str, Any]],
) -> tuple[float, str]:
    """Evaluate a transcript with an LLM-as-judge via litellm.

    Args:
        llm_judge_config: Dict with ``model_ref``, ``rubric``,
            ``output_schema`` keys (matching ``LLMJudgeConfig``).
        llm_messages: Conversation messages from the trial — the same
            list the orchestrator passes to ``GradeTrial``.

    Returns:
        Tuple of (score 0.0–1.0, reasons string). Returns ``(-1.0, msg)``
        only when the judge is *not configured* (no model_ref / rubric).
        Evaluation **failures** return ``(0.0, error_msg)`` so the score
        is included in the weighted grade — penalising rather than hiding
        the failure.
    """
    import litellm

    model_ref = llm_judge_config.get("model_ref", "")
    rubric = llm_judge_config.get("rubric", "")

    if not model_ref or not rubric:
        logger.warning("LLM judge not configured (missing model_ref or rubric)")
        return -1.0, "LLM judge not configured"

    # Mirror resolved secrets to os.environ so litellm finds the provider
    # API key (litellm reads env directly rather than accepting a secret
    # manager). This is the legitimate boundary use of export_to_environ.
    from tolokaforge.secrets import get_default

    sm = get_default()
    sm.export_to_environ(sm.list_all_keys())

    transcript_text = _format_transcript_for_judge(llm_messages)

    structured_rubric = llm_judge_config.get("structured_rubric")
    if structured_rubric is not None:
        criterion_ids = [
            criterion.get("id")
            for criterion in structured_rubric.get("criteria", [])
            if isinstance(criterion, dict)
        ]
        system_prompt = (
            "You are a grading judge. Evaluate every criterion in the provided "
            "structured rubric independently against the agent transcript. "
            "Respond ONLY with a JSON object containing 'criteria', an object "
            "whose keys are exactly the criterion IDs and whose values are JSON "
            "booleans, plus 'reasons', a concise string. Do not return an overall "
            "score; the runner computes it deterministically. Required IDs: "
            f"{criterion_ids!r}.\n\nRubric:\n{rubric}"
        )
    else:
        system_prompt = (
            "You are a grading judge. Evaluate the following agent transcript "
            "against the provided rubric. Respond ONLY with a JSON object containing "
            "'score' (float 0.0-1.0) and 'reasons' (string explaining the score). "
            "No other text.\n\n"
            f"Rubric:\n{rubric}"
        )

    user_prompt = f"Transcript to evaluate:\n\n{transcript_text}"

    try:
        response = litellm.completion(
            model=model_ref,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
        )

        content = response.choices[0].message.content or ""
        if not content.strip():
            logger.error("LLM judge returned empty response")
            return 0.0, "LLM judge returned empty response"

        result = _parse_judge_json(content)
        if structured_rubric is not None:
            score, reasons = _score_structured_judge_result(
                structured_rubric,
                result,
            )
            logger.info("Structured LLM judge evaluation: score=%.2f", score)
            return score, reasons
        score = max(0.0, min(1.0, float(result.get("score", 0.0))))
        reasons = str(result.get("reasons", result.get("reasoning", "")))
        logger.info("LLM judge evaluation: score=%.2f", score)
        return score, reasons

    except Exception as e:
        logger.error("LLM judge evaluation failed: %s", e, exc_info=True)
        return 0.0, f"LLM judge failed: {e}"


def _score_structured_judge_result(
    rubric: dict[str, Any],
    result: dict[str, Any],
) -> tuple[float, str]:
    """Apply binary weights and hard gates to criterion-level judge output."""
    criteria = rubric.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        raise ValueError("structured rubric has no criteria")

    configured: dict[str, dict[str, Any]] = {}
    for criterion in criteria:
        if not isinstance(criterion, dict):
            raise ValueError("structured rubric criterion is not an object")
        criterion_id = criterion.get("id")
        weight = criterion.get("weight")
        if (
            not isinstance(criterion_id, str)
            or not criterion_id
            or criterion_id in configured
            or criterion.get("kind") != "binary"
            or not isinstance(weight, (int, float))
            or isinstance(weight, bool)
            or weight <= 0
            or not isinstance(criterion.get("required"), bool)
        ):
            raise ValueError("structured rubric criterion is invalid")
        configured[criterion_id] = criterion

    decisions = result.get("criteria")
    if not isinstance(decisions, dict):
        raise ValueError("structured judge response has no criteria object")
    expected_ids = set(configured)
    actual_ids = set(decisions)
    if actual_ids != expected_ids:
        missing = sorted(expected_ids - actual_ids)
        extra = sorted(actual_ids - expected_ids)
        raise ValueError(f"structured judge criterion IDs differ: missing={missing}, extra={extra}")
    if any(type(value) is not bool for value in decisions.values()):
        raise ValueError("structured judge decisions must be JSON booleans")

    failed_required = sorted(
        criterion_id
        for criterion_id, criterion in configured.items()
        if criterion["required"] and not decisions[criterion_id]
    )
    total_weight = sum(float(criterion["weight"]) for criterion in configured.values())
    earned_weight = sum(
        float(configured[criterion_id]["weight"])
        for criterion_id, passed in decisions.items()
        if passed
    )
    score = 0.0 if failed_required else earned_weight / total_weight
    passed_ids = sorted(criterion_id for criterion_id, passed in decisions.items() if passed)
    failed_ids = sorted(criterion_id for criterion_id, passed in decisions.items() if not passed)
    model_reasons = result.get("reasons", "")
    if not isinstance(model_reasons, str):
        raise ValueError("structured judge reasons must be a string")
    summary = f"passed={passed_ids}; failed={failed_ids}; failed_required={failed_required}"
    if model_reasons:
        summary += f"; {model_reasons}"
    return score, summary


def _parse_judge_json(text: str) -> dict[str, Any]:
    """Parse JSON from a judge response — tolerant of code fences and prose.

    Tries direct ``json.loads`` first, then a ```json fenced block, then any
    ``{...}`` substring containing a ``"score"`` key. Raises ``ValueError``
    if none of those succeed; the caller maps that to ``(0.0, error)``.
    """
    import json as json_mod

    try:
        return json_mod.loads(text)
    except (json_mod.JSONDecodeError, ValueError):
        pass

    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        try:
            return json_mod.loads(match.group(1))
        except (json_mod.JSONDecodeError, ValueError):
            pass

    match = re.search(r"\{[^{}]*\"score\"[^{}]*\}", text, re.DOTALL)
    if match:
        try:
            return json_mod.loads(match.group(0))
        except (json_mod.JSONDecodeError, ValueError):
            pass

    raise ValueError(f"Could not parse JSON from judge response: {text[:200]}")


def _format_transcript_for_judge(messages: list[dict[str, Any]]) -> str:
    """Format conversation messages into a readable transcript for the judge.

    Includes tool-call details so the judge can evaluate tool usage quality.
    Tool outputs are truncated at 2000 chars to keep prompts tight.
    """
    parts = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        tool_calls = msg.get("tool_calls", [])
        tool_call_id = msg.get("tool_call_id")

        if role == "tool" and tool_call_id:
            truncated = content[:2000] + ("..." if len(content) > 2000 else "")
            parts.append(f"[tool result]: {truncated}")
        elif content:
            parts.append(f"[{role}]: {content}")

        for tc in tool_calls or []:
            name = tc.get("name", "?")
            args = tc.get("arguments", {})
            parts.append(f"  → Tool call: {name}({args})")

    return "\n\n".join(parts)
