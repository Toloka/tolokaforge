"""State-based grading checks"""

import fnmatch
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any, Union

from jsonpath_ng.ext import parse

from tolokaforge.core.logging import get_logger
from tolokaforge.core.utils.diff import calculate_state_diff, format_diff_summary

# Tau-bench compatible hash types
ToHashable = Union[str, int, float, dict[str, "ToHashable"], list["ToHashable"], set["ToHashable"]]
Hashable = Union[str, int, float, tuple["Hashable"], tuple[tuple[str, "Hashable"]]]


def to_hashable(item: ToHashable) -> Hashable:
    """Convert item to hashable representation (tau-bench compatible)"""
    if isinstance(item, dict):
        return tuple((key, to_hashable(value)) for key, value in sorted(item.items()))
    elif isinstance(item, list):
        return tuple(to_hashable(element) for element in item)
    elif isinstance(item, set):
        return tuple(sorted(to_hashable(element) for element in item))
    else:
        return item


def consistent_hash(value: Hashable) -> str:
    """Compute consistent SHA256 hash (tau-bench compatible)"""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


class StateChecker:
    """Check final environment state against expectations"""

    def __init__(self):
        self.logger = get_logger("state_checker")

    @staticmethod
    def _eq(actual: Any, expected: Any, ci: bool = False) -> bool:
        """Compare values, optionally case-insensitive for strings."""
        if ci and isinstance(actual, str) and isinstance(expected, str):
            return actual.casefold() == expected.casefold()
        return actual == expected

    def _contains(self, haystack: Any, needle: Any, ci: bool = False) -> bool:
        """
        Recursive contains check used by `contains` and `contains_ci`.
        - Strings: substring match
        - Lists/Tuples/Sets: any element recursively contains/matches
        - Dicts: any value recursively contains/matches
        - Scalars: exact match
        """
        if isinstance(haystack, str) and isinstance(needle, str):
            if ci:
                return needle.casefold() in haystack.casefold()
            return needle in haystack

        if isinstance(haystack, (list, tuple, set)):
            return any(self._contains(item, needle, ci) for item in haystack)

        if isinstance(haystack, dict):
            return any(self._contains(v, needle, ci) for v in haystack.values())

        return self._eq(haystack, needle, ci=ci)

    def check_jsonpaths(
        self, state: dict[str, Any], assertions: list[dict[str, Any]]
    ) -> tuple[float, list[str]]:
        """
        Check JSONPath assertions against state

        Args:
            state: Final environment state
            assertions: List of JSONPath assertions with expected values

        Returns:
            (score 0-1, list of reasons)
        """
        if not assertions:
            return 1.0, []

        satisfied = 0
        reasons = []

        for assertion in assertions:
            path = assertion.get("path")
            path_glob = assertion.get("path_glob")
            expected_equals = assertion.get("equals")
            expected_equals_ci = assertion.get("equals_ci")
            expected_contains = assertion.get("contains")
            expected_contains_ci = assertion.get("contains_ci")
            description = assertion.get("description", "")

            try:
                match_values: list[Any] = []
                target = path

                if path_glob is not None:
                    filesystem = state.get("filesystem", {})
                    if not isinstance(filesystem, dict):
                        reasons.append(
                            f"filesystem is missing/non-dict for glob: {path_glob} ({description})"
                        )
                        continue
                    target = path_glob
                    match_values = [
                        value
                        for key, value in filesystem.items()
                        if fnmatch.fnmatch(str(key), str(path_glob))
                    ]
                else:
                    if path is None:
                        reasons.append(f"Missing path/path_glob assertion target ({description})")
                        continue
                    jsonpath_expr = parse(path)
                    matches = jsonpath_expr.find(state)
                    match_values = [m.value for m in matches]

                if not match_values:
                    reasons.append(f"Path not found: {target} ({description})")
                    continue

                # Check based on operator
                found_match = False

                active_operators = sum(
                    1
                    for v in (
                        expected_equals,
                        expected_equals_ci,
                        expected_contains,
                        expected_contains_ci,
                    )
                    if v is not None
                )
                if active_operators > 1:
                    reasons.append(f"Assertion has multiple operators at {path} ({description})")
                    continue

                if expected_equals is not None:
                    # Equals check: exact match required
                    for value in match_values:
                        if self._eq(value, expected_equals):
                            found_match = True
                            satisfied += 1
                            break

                    if not found_match:
                        actual = match_values[0] if match_values else None
                        reasons.append(
                            f"Path {target}: expected {expected_equals}, got {actual} ({description})"
                        )

                elif expected_equals_ci is not None:
                    # Equals check (case-insensitive string compare)
                    for value in match_values:
                        if self._eq(value, expected_equals_ci, ci=True):
                            found_match = True
                            satisfied += 1
                            break

                    if not found_match:
                        actual = match_values[0] if match_values else None
                        reasons.append(
                            f"Path {target}: expected (ci) {expected_equals_ci}, got {actual} ({description})"
                        )

                elif expected_contains is not None:
                    # Contains check (case-sensitive)
                    for value in match_values:
                        if self._contains(value, expected_contains, ci=False):
                            found_match = True
                            satisfied += 1
                            break

                    if not found_match:
                        actual = match_values[0] if match_values else None
                        reasons.append(
                            f"Path {target}: {expected_contains} not found in {actual} ({description})"
                        )

                elif expected_contains_ci is not None:
                    # Contains check (case-insensitive string compare)
                    for value in match_values:
                        if self._contains(value, expected_contains_ci, ci=True):
                            found_match = True
                            satisfied += 1
                            break

                    if not found_match:
                        actual = match_values[0] if match_values else None
                        reasons.append(
                            f"Path {target}: {expected_contains_ci} not found in {actual} ({description})"
                        )

                else:
                    # No operator specified, just check path exists
                    found_match = True
                    satisfied += 1

            except Exception as e:
                reasons.append(f"Error checking {path}: {str(e)} ({description})")

        score = satisfied / len(assertions) if assertions else 1.0
        return score, reasons

    def check_hash(
        self,
        state: dict[str, Any],
        expected_hash: str,
    ) -> tuple[float, str]:
        """
        Check state hash against expected using tau-bench algorithm

        Args:
            state: Final environment state
            expected_hash: Expected SHA256 hash of normalized state

        Returns:
            (score 0 or 1, reason)
        """
        try:
            # Use tau-bench compatible hashing
            actual_hash = consistent_hash(to_hashable(state))

            if actual_hash == expected_hash:
                return 1.0, "State hash matches (tau-bench algorithm)"
            else:
                return (
                    0.0,
                    f"State hash mismatch: expected {expected_hash[:16]}..., got {actual_hash[:16]}...",
                )
        except Exception as e:
            return 0.0, f"Error computing hash: {str(e)}"

    def grade(
        self,
        state: dict[str, Any],
        jsonpath_assertions: list[dict[str, Any]],
        expected_hash: str | None = None,
        hash_weight: float = 0.5,
    ) -> tuple[float, str]:
        """
        Grade state with combination of JSONPath and hash checks

        Args:
            state: Final environment state
            jsonpath_assertions: JSONPath assertions
            expected_hash: Optional expected state hash
            hash_weight: Weight for hash check vs JSONPath

        Returns:
            (score 0-1, reasons)
        """
        jsonpath_score, jsonpath_reasons = self.check_jsonpaths(state, jsonpath_assertions)

        if expected_hash:
            hash_score, hash_reason = self.check_hash(state, expected_hash)
            # Weighted combination
            final_score = (jsonpath_score * (1 - hash_weight)) + (hash_score * hash_weight)
            reasons = jsonpath_reasons + [hash_reason]
        else:
            final_score = jsonpath_score
            reasons = jsonpath_reasons

        return final_score, "; ".join(reasons)

    def _execute_golden_actions(
        self,
        golden_actions: list[dict[str, Any]],
        task_dir: Path,
        initial_state_path: str,
        mcp_server_path: str,
        task_domain: str,
    ) -> dict[str, Any]:
        """
        Execute golden actions on fresh initial state and return resulting state.

        Args:
            golden_actions: List of actions with "name" and "kwargs" keys
            task_dir: Task directory containing data files
            initial_state_path: Path to initial state JSON file (relative to task_dir)
            mcp_server_path: Path to MCP server module (relative to task_dir)
            task_domain: Domain name (e.g., "airline", "telecom")

        Returns:
            State after executing golden actions
        """
        # 1. Load fresh initial state
        initial_state_file = task_dir / initial_state_path
        if not initial_state_file.exists():
            self.logger.error("Initial state file not found", path=str(initial_state_file))
            raise ValueError(f"Initial state file not found: {initial_state_file}")

        with open(initial_state_file) as f:
            data = json.load(f)

        self.logger.debug("Loaded initial state", path=str(initial_state_file))

        # 2. Import MCP server module
        mcp_server_file = task_dir / mcp_server_path
        if not mcp_server_file.exists():
            self.logger.error("MCP server file not found", path=str(mcp_server_file))
            raise ValueError(f"MCP server file not found: {mcp_server_file}")

        spec = importlib.util.spec_from_file_location("mcp_server", mcp_server_file)
        if not spec or not spec.loader:
            self.logger.error("Could not load MCP server module", path=mcp_server_path)
            raise ValueError(f"Could not load MCP server module: {mcp_server_path}")

        mcp_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mcp_module)

        # 3. Get tools map
        tools_map = getattr(mcp_module, "TOOLS", None)
        if not tools_map:
            self.logger.error(
                "TOOLS not found in MCP server", domain=task_domain, path=mcp_server_path
            )
            raise ValueError(
                f"Could not find TOOLS in MCP server for domain {task_domain}: {mcp_server_path}"
            )

        self.logger.debug("Executing golden actions", count=len(golden_actions))

        # 4. Execute golden actions
        for action in golden_actions:
            action_name = action.get("name")
            action_kwargs = action.get("kwargs", {})

            if action_name not in tools_map:
                self.logger.warning("Golden action tool not found in TOOLS map", action=action_name)
                continue

            tool_class = tools_map[action_name]
            try:
                # Tau-bench tools have invoke(data=data, **kwargs) signature
                tool_class.invoke(data=data, **action_kwargs)
                self.logger.debug(
                    "Executed golden action", action=action_name, kwargs=action_kwargs
                )
            except Exception as e:
                # Log but continue (some actions might fail if preconditions not met)
                # This matches tau-bench behavior - it continues even if some actions fail
                self.logger.warning("Golden action failed", action=action_name, error=str(e))

        return data

    def compute_tau_style_expected_hash(
        self,
        golden_actions: list[dict[str, Any]],
        task_dir: Path,
        initial_state_path: str,
        mcp_server_path: str,
        task_domain: str,
    ) -> str:
        """
        Compute expected hash by executing golden actions on fresh data (tau-bench style).

        This is more robust than pre-computed hashes because:
        - Golden actions are the source of truth
        - Hash is computed dynamically from current initial state
        - Adding unrelated data doesn't break grading

        Args:
            golden_actions: List of actions with "name" and "kwargs" keys
            task_dir: Task directory containing data files
            initial_state_path: Path to initial state JSON file (relative to task_dir)
            mcp_server_path: Path to MCP server module (relative to task_dir)
            task_domain: Domain name (e.g., "airline", "telecom")

        Returns:
            Expected SHA256 hash of state after executing golden actions
        """
        data = self._execute_golden_actions(
            golden_actions, task_dir, initial_state_path, mcp_server_path, task_domain
        )
        return consistent_hash(to_hashable(data))

    def grade_tau_style(
        self,
        state: dict[str, Any],
        jsonpath_assertions: list[dict[str, Any]],
        golden_actions: list[dict[str, Any]],
        task_dir: Path,
        initial_state_path: str,
        mcp_server_path: str,
        task_domain: str,
        hash_weight: float = 1.0,
    ) -> tuple[float, str, dict[str, Any] | None]:
        """
        Grade state using tau-bench style with diff calculation.

        Executes golden actions to get expected state, compares with actual state,
        and returns detailed diff if they don't match.

        Args:
            state: Final environment state
            jsonpath_assertions: JSONPath assertions
            golden_actions: List of golden actions to execute
            task_dir: Task directory
            initial_state_path: Path to initial state JSON (relative to task_dir)
            mcp_server_path: Path to MCP server module (relative to task_dir)
            task_domain: Domain name (e.g., "airline")
            hash_weight: Weight for hash check vs JSONPath

        Returns:
            (score 0-1, reasons, diff_result dict or None)
        """
        jsonpath_score, jsonpath_reasons = self.check_jsonpaths(state, jsonpath_assertions)

        # Execute golden actions to get expected state
        try:
            expected_state = self._execute_golden_actions(
                golden_actions, task_dir, initial_state_path, mcp_server_path, task_domain
            )
        except Exception as e:
            error_msg = f"Error executing golden actions: {str(e)}"
            self.logger.error("Failed to execute golden actions", error=str(e))
            return 0.0, error_msg, None

        # Extract database state from final state structure
        # Final state has structure: {"agent": {...}, "user": {...}, "db": {...}, ...}
        # For airline/telecom tasks, we want to hash the "db" key (legacy format) or "agent" key
        # The initial state JSON file contains the raw database state
        db_state = state.get("db", state.get("agent", state))

        # Compute hashes
        expected_hash = consistent_hash(to_hashable(expected_state))
        actual_hash = consistent_hash(to_hashable(db_state))

        # Calculate diff if states don't match
        diff_result = None
        if expected_hash != actual_hash:
            self.logger.info(
                "State hash mismatch, calculating diff",
                expected_hash=expected_hash[:16],
                actual_hash=actual_hash[:16],
            )
            diff_result = calculate_state_diff(expected_state, db_state)
            diff_summary = format_diff_summary(diff_result, max_lines=50)

            hash_score = 0.0
            hash_reason = f"State hash mismatch. Diff:\n{diff_summary}"

            self.logger.error(
                "State mismatch in golden set grading",
                expected_hash=expected_hash[:16],
                actual_hash=actual_hash[:16],
                diff_lines=diff_result["diff_lines"],
            )
        else:
            hash_score = 1.0
            hash_reason = "State hash matches"
            self.logger.info(
                "State hash matches", expected_hash=expected_hash[:16], actual_hash=actual_hash[:16]
            )

        # Weighted combination
        final_score = (jsonpath_score * (1 - hash_weight)) + (hash_score * hash_weight)
        reasons = jsonpath_reasons + [hash_reason]

        return final_score, "; ".join(reasons), diff_result
