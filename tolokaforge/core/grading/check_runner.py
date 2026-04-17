"""
CheckRunner - Safely loads and executes custom check modules.

This module provides the CheckRunner class that handles loading checks.py
files from tasks, managing relative imports, and executing checks with
timeout protection.

Usage:
    from tolokaforge.core.grading.check_runner import CheckRunner
    from tolokaforge.core.grading.checks_interface import CheckContext, CustomChecksConfig

    runner = CheckRunner()
    result = runner.run(
        checks_file=Path("path/to/task/checks.py"),
        task_dir=Path("path/to/task"),
        ctx=check_context,
        config=custom_checks_config,
    )
"""

from __future__ import annotations

import importlib.util
import sys
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path
from types import ModuleType
from typing import Any

from tolokaforge.core.grading.checks_interface import (
    SUPPORTED_VERSIONS,
    CheckContext,
    CheckFailed,
    CheckPassed,
    CheckResult,
    CheckResultSet,
    CheckReturnType,
    CheckSkipped,
    CheckStatus,
    CustomChecksConfig,
    get_init_func,
    get_interface_version,
    get_registered_checks,
    reset_registry,
)
from tolokaforge.core.logging import get_logger


class CheckRunner:
    """
    Safely loads and executes custom check modules.

    Features:
    - Version validation against SUPPORTED_VERSIONS
    - Timeout enforcement via ThreadPoolExecutor
    - Relative imports support via sys.path manipulation
    - Error isolation with detailed traceback capture
    - Structured logging

    Attributes:
        executor_type: Type of executor ("thread" or "process")
        max_workers: Number of workers for the executor

    Example:
        runner = CheckRunner()
        result = runner.run(
            checks_file=task_dir / "checks.py",
            task_dir=task_dir,
            ctx=context,
            config=CustomChecksConfig(enabled=True, timeout_seconds=30),
        )

        if result.all_passed:
            print("All checks passed!")
        else:
            for r in result.results:
                if r.status == CheckStatus.FAILED:
                    print(f"FAIL: {r.check_name} - {r.message}")
    """

    def __init__(
        self,
        executor_type: str = "thread",
        max_workers: int = 1,
    ):
        """
        Initialize the CheckRunner.

        Args:
            executor_type: Executor type for isolation. Currently only "thread"
                          is fully supported. "process" may be added later.
            max_workers: Number of executor workers (default 1 for sequential)
        """
        self.logger = get_logger("check_runner")
        self.executor_type = executor_type
        self.max_workers = max_workers

    def _add_import_paths(
        self,
        task_dir: Path,
        relative_imports: list[str],
    ) -> list[str]:
        """
        Add relative import paths to sys.path.

        Args:
            task_dir: Task directory (base for relative paths)
            relative_imports: List of relative paths to add

        Returns:
            List of absolute paths that were added (for later cleanup)
        """
        added = []
        for rel_path in relative_imports:
            abs_path = str((task_dir / rel_path).resolve())
            if abs_path not in sys.path:
                sys.path.insert(0, abs_path)
                added.append(abs_path)
                self.logger.debug("Added import path", path=abs_path)
        return added

    def _remove_import_paths(self, paths: list[str]):
        """
        Remove previously added import paths from sys.path.

        Args:
            paths: List of absolute paths to remove
        """
        for path in paths:
            if path in sys.path:
                sys.path.remove(path)
                self.logger.debug("Removed import path", path=path)

    def _clear_cached_modules(self, module_patterns: list[str]):
        """
        Clear any cached modules matching patterns to avoid stale imports.

        This is important when multiple checks.py files import common modules
        like 'check_helpers' - we need to ensure the correct version is loaded.

        Args:
            module_patterns: Module name patterns to clear from cache
        """
        modules_to_remove = []
        for module_name in sys.modules:
            for pattern in module_patterns:
                if module_name == pattern or module_name.startswith(pattern + "."):
                    modules_to_remove.append(module_name)
                    break

        for module_name in modules_to_remove:
            del sys.modules[module_name]
            self.logger.debug("Cleared cached module", module=module_name)

    def load_checks_module(
        self,
        checks_file: Path,
        task_dir: Path,
        relative_imports: list[str],
        expected_version: str,
    ) -> tuple[
        Callable[[CheckContext], None] | None, dict[str, Callable[[], CheckReturnType]], str
    ]:
        """
        Load checks.py and validate interface version.

        This function:
        1. Resets the decorator registry
        2. Adds relative import paths to sys.path
        3. Loads and executes the checks module
        4. Extracts decorated functions from registry
        5. Validates the interface version
        6. Cleans up import paths

        Args:
            checks_file: Path to checks.py file
            task_dir: Task directory (base for relative imports)
            relative_imports: Paths to add for imports
            expected_version: Expected interface version from config

        Returns:
            Tuple of (init_func, checks_dict, interface_version)

        Raises:
            ValueError: If file not found, can't load, or version unsupported
        """
        if not checks_file.exists():
            raise ValueError(f"Checks file not found: {checks_file}")

        # Reset the registry before loading to ensure clean state
        reset_registry()

        # Clear potentially stale cached modules that might conflict
        # (e.g., check_helpers from a different project)
        self._clear_cached_modules(["check_helpers", "task_helpers", "helpers"])

        # Add import paths
        added_paths = self._add_import_paths(task_dir, relative_imports)

        try:
            # Load module dynamically
            spec = importlib.util.spec_from_file_location("task_checks", checks_file)
            if not spec or not spec.loader:
                raise ValueError(f"Could not create module spec for: {checks_file}")

            module: ModuleType = importlib.util.module_from_spec(spec)

            # Add to sys.modules temporarily so relative imports work
            sys.modules["task_checks"] = module
            try:
                spec.loader.exec_module(module)
            finally:
                # Remove from sys.modules to avoid pollution
                if "task_checks" in sys.modules:
                    del sys.modules["task_checks"]

            # Get registered functions from decorators
            init_func = get_init_func()
            checks = get_registered_checks()
            version = get_interface_version()

            self.logger.debug(
                "Loaded checks module",
                file=str(checks_file),
                init_found=init_func is not None,
                check_count=len(checks),
                version=version,
            )

            # Validate version
            if version not in SUPPORTED_VERSIONS:
                raise ValueError(
                    f"Unsupported interface version: {version}. Supported: {SUPPORTED_VERSIONS}"
                )

            if expected_version != version:
                self.logger.warning(
                    "Version mismatch between config and module",
                    expected=expected_version,
                    actual=version,
                )

            return init_func, checks, version

        finally:
            # Clean up import paths even if error occurred
            self._remove_import_paths(added_paths)

    def _execute_checks(
        self,
        init_func: Callable[[CheckContext], None] | None,
        checks: dict[str, Callable[[], CheckReturnType]],
        ctx: CheckContext,
    ) -> CheckResultSet:
        """
        Execute all checks in the current context.

        This function:
        1. Runs the @init function if provided
        2. Runs each @check function in order
        3. Converts return values to CheckResult
        4. Captures any exceptions as ERROR results

        Args:
            init_func: Optional initialization function
            checks: Dictionary of check_name -> check_function
            ctx: CheckContext with all episode data

        Returns:
            CheckResultSet with all results
        """
        start_time = time.time()
        results: list[CheckResult] = []

        try:
            # Run init if provided
            if init_func:
                self.logger.debug("Running init function")
                init_func(ctx)
                self.logger.debug("Init function completed")

            # Run each check
            for check_name, check_func in checks.items():
                try:
                    self.logger.debug("Running check", check_name=check_name)
                    check_output = check_func()

                    # Convert to CheckResult based on return type
                    if isinstance(check_output, CheckPassed):
                        results.append(
                            CheckResult(
                                check_name=check_name,
                                status=CheckStatus.PASSED,
                                score=check_output.score,
                                message=check_output.message,
                                details=check_output.details,
                            )
                        )
                        self.logger.debug(
                            "Check passed",
                            check_name=check_name,
                            msg=check_output.message,
                        )
                    elif isinstance(check_output, CheckFailed):
                        results.append(
                            CheckResult(
                                check_name=check_name,
                                status=CheckStatus.FAILED,
                                score=check_output.score,
                                message=check_output.message,
                                details=check_output.details,
                            )
                        )
                        self.logger.debug(
                            "Check failed",
                            check_name=check_name,
                            msg=check_output.message,
                        )
                    elif isinstance(check_output, CheckSkipped):
                        results.append(
                            CheckResult(
                                check_name=check_name,
                                status=CheckStatus.SKIPPED,
                                score=0.0,
                                message=check_output.message,
                            )
                        )
                        self.logger.debug(
                            "Check skipped",
                            check_name=check_name,
                            msg=check_output.message,
                        )
                    else:
                        # Invalid return type
                        type_name = type(check_output).__name__ if check_output else "None"
                        results.append(
                            CheckResult(
                                check_name=check_name,
                                status=CheckStatus.ERROR,
                                score=0.0,
                                message=f"Invalid return type: {type_name}. "
                                f"Expected CheckPassed, CheckFailed, or CheckSkipped.",
                            )
                        )
                        self.logger.warning(
                            "Check returned invalid type",
                            check_name=check_name,
                            returned_type=type_name,
                        )

                except Exception as e:
                    # Check function raised an exception
                    tb = traceback.format_exc()
                    results.append(
                        CheckResult(
                            check_name=check_name,
                            status=CheckStatus.ERROR,
                            score=0.0,
                            message=f"Check raised exception: {str(e)}",
                            details={"traceback": tb},
                        )
                    )
                    self.logger.warning(
                        "Check raised exception",
                        check_name=check_name,
                        error=str(e),
                    )

            return CheckResultSet(
                results=results,
                execution_time_ms=(time.time() - start_time) * 1000,
            )

        except Exception as e:
            # Init or other top-level error
            tb = traceback.format_exc()
            return CheckResultSet(
                error=f"Init or execution failed: {str(e)}\n{tb}",
                execution_time_ms=(time.time() - start_time) * 1000,
            )

    def run(
        self,
        checks_file: Path,
        task_dir: Path,
        ctx: CheckContext,
        config: CustomChecksConfig,
    ) -> CheckResultSet:
        """
        Execute custom checks with safety controls.

        This is the main entry point for running checks. It:
        1. Loads the checks module
        2. Validates the interface version
        3. Executes checks with timeout protection
        4. Returns results or error information

        Args:
            checks_file: Path to checks.py
            task_dir: Task directory (for relative imports)
            ctx: CheckContext with all episode data
            config: Configuration for execution

        Returns:
            CheckResultSet with all results or error

        Example:
            runner = CheckRunner()
            result = runner.run(
                checks_file=task_dir / "checks.py",
                task_dir=task_dir,
                ctx=context,
                config=CustomChecksConfig(
                    enabled=True,
                    timeout_seconds=30,
                    relative_imports=["../.."],
                ),
            )
        """
        start_time = time.time()

        self.logger.info(
            "Starting custom checks",
            file=str(checks_file),
            timeout=config.timeout_seconds,
        )

        try:
            # Load module and get functions
            init_func, checks, version = self.load_checks_module(
                checks_file,
                task_dir,
                config.relative_imports,
                config.interface_version,
            )

            if not checks:
                self.logger.warning("No @check decorated functions found")
                return CheckResultSet(
                    error="No @check decorated functions found in checks.py",
                    execution_time_ms=(time.time() - start_time) * 1000,
                )

            self.logger.info(
                "Loaded checks",
                check_count=len(checks),
                check_names=list(checks.keys()),
                has_init=init_func is not None,
            )

            # Execute with timeout using ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                future = executor.submit(self._execute_checks, init_func, checks, ctx)
                try:
                    result = future.result(timeout=config.timeout_seconds)
                except FuturesTimeoutError:
                    error_msg = f"Checks timed out after {config.timeout_seconds}s"
                    self.logger.error(
                        "Checks timed out",
                        timeout=config.timeout_seconds,
                    )
                    return CheckResultSet(
                        error=error_msg,
                        execution_time_ms=(time.time() - start_time) * 1000,
                    )

            self.logger.info(
                "Checks completed",
                passed=result.passed,
                failed=result.failed,
                errors=result.errors,
                skipped=result.skipped,
                aggregate_score=result.aggregate_score,
                execution_time_ms=result.execution_time_ms,
            )

            return result

        except Exception as e:
            tb = traceback.format_exc()
            error_msg = f"Failed to load/run checks: {str(e)}"
            self.logger.error(
                "Failed to run checks",
                error=str(e),
            )
            return CheckResultSet(
                error=f"{error_msg}\n{tb}",
                execution_time_ms=(time.time() - start_time) * 1000,
            )

    def result_to_score(
        self,
        result: CheckResultSet,
        config: CustomChecksConfig,
    ) -> tuple[float, str]:
        """
        Convert CheckResultSet to score and reason string.

        This function produces the final score and human-readable summary
        from check results. Useful for integrating into the grading pipeline.

        Args:
            result: CheckResultSet from run()
            config: Configuration (for fail_on_error handling)

        Returns:
            Tuple of (score 0.0-1.0, reason string)

        Example:
            result = runner.run(...)
            score, reason = runner.result_to_score(result, config)
            print(f"Score: {score}")
            print(reason)
        """
        if result.error:
            if config.fail_on_error:
                return 0.0, f"Check error: {result.error}"
            else:
                return 0.5, f"Check error (non-fatal): {result.error}"

        if not result.results:
            return 1.0, "No custom checks defined"

        # Build human-readable reason string
        reasons = []
        for r in result.results:
            if r.status == CheckStatus.PASSED:
                reasons.append(f"✓ {r.check_name}: {r.message}")
            elif r.status == CheckStatus.FAILED:
                reasons.append(f"✗ {r.check_name}: {r.message}")
            elif r.status == CheckStatus.ERROR:
                reasons.append(f"⚠ {r.check_name}: ERROR - {r.message}")
            elif r.status == CheckStatus.SKIPPED:
                reasons.append(f"- {r.check_name}: (skipped) {r.message}")

        summary = (
            f"Custom checks: {result.passed}/{result.total} passed, "
            f"score: {result.aggregate_score:.2f}"
        )
        reasons.insert(0, summary)

        return result.aggregate_score, "\n".join(reasons)


# =============================================================================
# Convenience function for simple usage
# =============================================================================


def run_custom_checks(
    checks_file: Path,
    task_dir: Path,
    initial_state: dict[str, Any],
    final_state: dict[str, Any],
    transcript_messages: list[dict[str, Any]],
    task_id: str,
    config: CustomChecksConfig | None = None,
) -> CheckResultSet:
    """
    Convenience function to run custom checks.

    Builds the CheckContext from raw dictionaries and runs checks.

    Args:
        checks_file: Path to checks.py
        task_dir: Task directory
        initial_state: Initial state dictionary
        final_state: Final state dictionary
        transcript_messages: List of message dictionaries
        task_id: Task identifier
        config: Optional config (default: enabled with 30s timeout)

    Returns:
        CheckResultSet with results
    """
    from tolokaforge.core.grading.checks_interface import (
        EnvironmentState,
        Message,
        TaskContext,
        ToolCall,
        Transcript,
    )

    # Build context from raw data
    ctx = CheckContext(
        initial_state=EnvironmentState(data=initial_state),
        final_state=EnvironmentState(data=final_state),
        transcript=Transcript(
            messages=[
                Message(
                    role=m.get("role", "user"),
                    content=m.get("content", ""),
                    tool_calls=[
                        ToolCall(
                            name=tc.get("name", ""),
                            arguments=tc.get("arguments", {}),
                            result=tc.get("result"),
                        )
                        for tc in m.get("tool_calls", [])
                    ],
                )
                for m in transcript_messages
            ]
        ),
        task=TaskContext(task_id=task_id),
    )

    if config is None:
        config = CustomChecksConfig(enabled=True)

    runner = CheckRunner()
    return runner.run(checks_file, task_dir, ctx, config)


# =============================================================================
# Public API exports
# =============================================================================

__all__ = [
    "CheckRunner",
    "run_custom_checks",
]
