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
    elif rule_type == "max_turns":
        result_dict = _evaluate_max_turns(rule, messages)
    else:
        result_dict = {"passed": True, "message": f"Unknown rule type: {rule_type}"}

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
        return {"passed": True, "message": "No tool_name specified, rule passes"}

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
) -> tuple[float, str]:
    """
    Evaluate jsonpath file assertions against the Runner container's filesystem.

    Each check has:
    - path_glob: glob pattern for files (e.g., "/env/fs/agent-visible/submissions/*")
    - contains_ci: case-insensitive substring to find in file content
    - description: human-readable description of the check

    Args:
        checks: List of jsonpath check dicts from grading.yaml

    Returns:
        Tuple of (score, reasons_string) where score is fraction of checks passed.
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
            reasons_parts.append(f"SKIP: No path_glob — {description}")
            continue

        # Glob for matching files on the container filesystem
        matching_files = glob.glob(path_pattern)

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

        if actually_configured:
            logger.warning(
                "Grading configured for %s but no components were evaluated — failing",
                actually_configured,
            )
            return 0.0, False
        # Truly no grading configured at all — pass by default
        return 1.0, True

    if method == "all":
        # All components must pass (score >= threshold)
        all_pass = all(score >= threshold for score in active_components.values())
        # Score is minimum of all component scores
        final_score = min(active_components.values())
        return final_score, all_pass

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

        binary_pass = final_score >= threshold
        return final_score, binary_pass

    elif method == "any":
        # Any component passing is sufficient
        any_pass = any(score >= threshold for score in active_components.values())
        # Score is maximum of all component scores
        final_score = max(active_components.values())
        return final_score, any_pass

    else:
        # Unknown method - default to "all" behavior
        logger.warning(f"Unknown combine_method '{method}', defaulting to 'all'")
        all_pass = all(score >= threshold for score in active_components.values())
        final_score = min(active_components.values())
        return final_score, all_pass


def build_grade_reasons(
    components: dict[str, Any],
    state_diff: dict[str, Any] | None = None,
    transcript_result: dict[str, Any] | None = None,
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
            reasons.append(f"Files: {jsonpath_reasons}")
        elif jsonpath_score == 1.0:
            reasons.append("Files: all jsonpath checks passed")
        else:
            reasons.append(f"Files: jsonpath score={jsonpath_score:.2f}")

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

    return " | ".join(reasons) if reasons else "No grading components evaluated"
