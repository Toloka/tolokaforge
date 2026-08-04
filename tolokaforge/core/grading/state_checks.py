"""State-based grading checks"""

import fnmatch
import hashlib
import importlib.util
import json
from collections.abc import Collection
from pathlib import Path
from typing import Any, Union

from jsonpath_ng.ext import parse

from tolokaforge.core.grading.golden_replay import (
    FailedGoldenAction,
    GoldenReplayError,
    GoldenReplayRecord,
    resolve_golden_action_names,
)
from tolokaforge.core.grading.predicates import contains
from tolokaforge.core.hash import canonical_number
from tolokaforge.core.logging import get_logger
from tolokaforge.core.utils.diff import calculate_state_diff, format_diff_summary

# Tau-bench compatible hash types
ToHashable = Union[str, int, float, dict[str, "ToHashable"], list["ToHashable"], set["ToHashable"]]
Hashable = Union[str, int, float, tuple["Hashable"], tuple[tuple[str, "Hashable"]]]


def to_hashable(
    item: ToHashable,
    string_fields: frozenset[str] | None = None,
    *,
    _normalize_strings: bool = False,
) -> Hashable:
    """Convert item to hashable representation (tau-bench compatible).

    Scalar values pass through :func:`canonical_number` so numerically-equal
    NUMERIC-TYPE representations (``130`` / ``130.0`` / ``Decimal("130.00")``)
    hash identically and a pure formatting difference is not graded as a state
    change.

    Numeric-looking STRINGS (``"130.00"`` == ``"130.0"``) fold only for a value
    sitting under a record key named in ``string_fields`` — the per-field opt-in
    that mirrors :func:`tolokaforge.core.hash.compute_stable_hash`. Key-aware
    exactly like ``_canonicalize_numbers``: ``_normalize_strings`` carries the
    per-key decision down through enclosing list(s)/set(s); a nested dict
    re-decides per its own keys.
    """
    if isinstance(item, dict):
        return tuple(
            (
                key,
                to_hashable(
                    value,
                    string_fields,
                    _normalize_strings=(string_fields is not None and key in string_fields),
                ),
            )
            for key, value in sorted(item.items())
        )
    elif isinstance(item, list):
        return tuple(
            to_hashable(element, string_fields, _normalize_strings=_normalize_strings)
            for element in item
        )
    elif isinstance(item, set):
        # Type-stable sort key: canonicalized scalars can mix bool with tagged
        # numeric/str tokens, which are not mutually orderable under a bare sort.
        return tuple(
            sorted(
                (
                    to_hashable(element, string_fields, _normalize_strings=_normalize_strings)
                    for element in item
                ),
                key=lambda x: (type(x).__name__, str(x)),
            )
        )
    else:
        return canonical_number(item, normalize_strings=_normalize_strings)


def consistent_hash(value: Hashable) -> str:
    """Compute consistent SHA256 hash (tau-bench compatible)"""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _tool_in_pack(name: str, tools: Collection[str]) -> str | None:
    """Core resolves a golden-action name against the pack's ``TOOLS`` map, exactly.

    Not the runner's rule, which also accepts a single ``…_<name>`` suffix over the
    tools it registered for the trial; #815 owns unifying the two namespaces.
    """
    return name if name in tools else None


def extract_db_state(final_env_state: dict[str, Any]) -> dict[str, Any]:
    """Return the level of a final environment state that a hash is computed over.

    A final environment state is wrapped — ``{"agent": …, "user": …, "db": …}`` —
    while the golden state comes from the task's initial-state JSON, which holds
    the raw database. Both the tau-bench hash and the runner's
    ``compute_stable_hash`` therefore describe the unwrapped level this returns.
    JSONPath assertions read the wrapped state instead, so ``$.db.<table>`` is
    what an author writes.
    """
    return final_env_state.get("db", final_env_state.get("agent", final_env_state))


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

    def check_jsonpaths(
        self, state: dict[str, Any], assertions: list[dict[str, Any]]
    ) -> tuple[float, list[str]]:
        """
        Check JSONPath assertions against state.

        Each assertion is a dict with one of:

        - ``path``: a JSONPath expression evaluated against ``state``
        - ``path_glob``: a glob evaluated against ``state["filesystem"]``

        plus exactly one of these recognized comparison operators:

        - ``equals``: exact equality
        - ``equals_ci``: case-insensitive string equality
        - ``contains``: substring (case-sensitive)
        - ``contains_ci``: substring (case-insensitive)

        Assertions with an unrecognized operator (e.g. ``op: gte`` /
        ``expected: 5``) are treated as **failed** with an actionable reason,
        because a strict-looking assertion that cannot fail is worse than none.

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
                        if contains(value, expected_contains, ci=False):
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
                        if contains(value, expected_contains_ci, ci=True):
                            found_match = True
                            satisfied += 1
                            break

                    if not found_match:
                        actual = match_values[0] if match_values else None
                        reasons.append(
                            f"Path {target}: {expected_contains_ci} not found in {actual} ({description})"
                        )

                else:
                    # A misspelled or unsupported operator (``op: gte`` /
                    # ``expected: 5``) would otherwise turn a strict-looking assertion
                    # into one that passes wherever the path exists, so name the
                    # operators the checker consumes and fail.
                    unknown_keys = sorted(
                        k for k in assertion if k not in {"path", "path_glob", "description"}
                    )
                    suffix = f" ({description})" if description else ""
                    reasons.append(
                        f"Assertion at {target} has no recognized operator "
                        f"(got keys: {unknown_keys}); supported operators are "
                        f"'equals', 'equals_ci', 'contains', 'contains_ci'{suffix}"
                    )

            except Exception as e:
                reasons.append(f"Error checking {path}: {str(e)} ({description})")

        score = satisfied / len(assertions) if assertions else 1.0
        return score, reasons

    def check_hash(
        self,
        state: dict[str, Any],
        expected_hash: str,
        *,
        numeric_string_fields: list[str] | None = None,
    ) -> tuple[float, str]:
        """
        Check state hash against expected using tau-bench algorithm

        Args:
            state: Final environment state
            expected_hash: Expected SHA256 hash of normalized state
            numeric_string_fields: Record field names whose numeric-looking
                string values fold when hashing (per-field opt-in).

        Returns:
            (score 0 or 1, reason)
        """
        try:
            # Use tau-bench compatible hashing
            string_fields = frozenset(numeric_string_fields) if numeric_string_fields else None
            actual_hash = consistent_hash(to_hashable(state, string_fields))

            if actual_hash == expected_hash:
                return 1.0, "State hash matches (tau-bench algorithm)"
            else:
                return (
                    0.0,
                    f"State hash mismatch: expected {expected_hash[:16]}..., got {actual_hash[:16]}...",
                )
        except Exception as e:
            return 0.0, f"Error computing hash: {str(e)}"

    def _execute_golden_actions(
        self,
        golden_actions: list[dict[str, Any]],
        task_dir: Path,
        initial_state_path: str,
        mcp_server_path: str,
        task_domain: str,
    ) -> tuple[dict[str, Any], GoldenReplayRecord]:
        """
        Execute golden actions on fresh initial state and return resulting state.

        Args:
            golden_actions: List of actions with "name" and "kwargs" keys
            task_dir: Task directory containing data files
            initial_state_path: Path to initial state JSON file (relative to task_dir)
            mcp_server_path: Path to MCP server module (relative to task_dir)
            task_domain: Domain name (e.g., "airline", "retail")

        Returns:
            The state after executing golden actions, and the record of which of them
            ran — an action that raised leaves the state short of the authored world
            and the record is the only thing that says so.

        Raises:
            UnresolvableGoldenAction: an action names no tool in the pack's ``TOOLS``
                map, or names nothing at all. Raised before the first ``invoke``, so a
                partial golden world is never built and never hashed against.
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

        # 4. Resolve every authored name
        resolved_names = resolve_golden_action_names(
            [action.get("name") for action in golden_actions],
            candidates=tools_map.keys(),
            match=_tool_in_pack,
        )

        self.logger.debug("Executing golden actions", count=len(golden_actions))

        # 5. Execute golden actions
        failures: list[FailedGoldenAction] = []
        for index, (action_name, action) in enumerate(
            zip(resolved_names, golden_actions, strict=True)
        ):
            action_kwargs = action.get("kwargs", {})
            tool_class = tools_map[action_name]
            try:
                # Tau-bench tools have invoke(data=data, **kwargs) signature
                tool_class.invoke(data=data, **action_kwargs)
                self.logger.debug(
                    "Executed golden action", action=action_name, kwargs=action_kwargs
                )
            except Exception as e:
                self.logger.warning("Golden action failed", action=action_name, error=str(e))
                failures.append(FailedGoldenAction.from_exception(index, action_name, e))

        return data, GoldenReplayRecord(authored=len(golden_actions), failures=tuple(failures))

    def check_hash_against_golden_replay(
        self,
        db_state: dict[str, Any],
        golden_actions: list[dict[str, Any]],
        task_dir: Path,
        initial_state_path: str,
        mcp_server_path: str,
        task_domain: str,
        *,
        numeric_string_fields: list[str] | None = None,
    ) -> tuple[float, str, dict[str, Any] | None, GoldenReplayRecord]:
        """
        Check state against the state a golden-action replay produces (tau-bench style).

        Args:
            db_state: Unwrapped database state (see :func:`extract_db_state`)
            golden_actions: List of golden actions to execute
            task_dir: Task directory
            initial_state_path: Path to initial state JSON (relative to task_dir)
            mcp_server_path: Path to MCP server module (relative to task_dir)
            task_domain: Domain name (e.g., "airline")
            numeric_string_fields: Record field names whose numeric-looking
                string values fold when hashing (per-field opt-in).

        Returns:
            (score 0 or 1, reason, diff_result dict or None, replay record). The verdict
            stands whether or not every action ran; the record carries what did not, for
            the caller to report beside the score.

        Raises:
            GoldenReplayError: the replay could not be executed, so there is no
                expected state to compare against and therefore no verdict. An action
                whose name resolves to no tool raises the ``UnresolvableGoldenAction``
                subclass, which names every offending action.
        """
        try:
            expected_state, replay = self._execute_golden_actions(
                golden_actions, task_dir, initial_state_path, mcp_server_path, task_domain
            )
        except GoldenReplayError:
            # The wrapper below would flatten a subclass into the base class, losing
            # which of the replay's preconditions the pack failed.
            raise
        except Exception as e:
            self.logger.error("Failed to execute golden actions", error=str(e))
            raise GoldenReplayError(f"Error executing golden actions: {e}") from e

        # Compute hashes
        string_fields = frozenset(numeric_string_fields) if numeric_string_fields else None
        expected_hash = consistent_hash(to_hashable(expected_state, string_fields))
        actual_hash = consistent_hash(to_hashable(db_state, string_fields))

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

        return hash_score, hash_reason, diff_result, replay
