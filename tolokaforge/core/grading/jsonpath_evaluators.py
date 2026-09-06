"""JSONPath assertion evaluators for author-facing ``state_checks.jsonpaths``.

Three evaluators score the same JSONPath assertion shape against different
substrates: :func:`evaluate_jsonpath_file_checks` scores ``path_glob:`` +
``contains_ci:`` checks against the runner container's filesystem (globs
``/env/fs/agent-visible/…`` paths after translating them to ``/work/…``,
where the file tools and BashTool actually operate);
:func:`evaluate_jsonpath_state_checks` scores ``path:`` checks with one of
``equals`` / ``equals_ci`` / ``contains`` / ``contains_ci`` against a
structured ``{db, tables, filesystem}`` state dict;
:func:`evaluate_jsonpath_checks` dispatches a mixed list to the appropriate
sub-evaluator per check shape.

All three return ``(score, reasons)`` where ``score`` is the fraction of
checks that passed and ``reasons`` is a ``"; "``-joined ``PASS: ...`` /
``FAIL: ...`` breakdown. An empty checks list returns the sentinel
``(-1.0, "")`` — the composite fold reads that as "no evaluation happened".

Author-error handling is loud: a state-check with no recognized operator, or
with two operators active, or with a bare ``path:`` but no ``path_glob:`` on
a file-only pack, scores as failed with an actionable reason rather than
silently passing. Silent passes would turn a strict-looking assertion into
a no-op and diverge from host-side grading.
"""

from __future__ import annotations

import glob
import logging
from pathlib import Path
from typing import Any

from jsonpath_ng.ext import parse

from tolokaforge.core.grading.predicates import contains

logger = logging.getLogger(__name__)


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
