"""
Custom Python Checks Interface - Version 1.0

This module provides the Pydantic models and decorators for defining
custom validation checks in task grading. Tasks can create a checks.py
file with @init and @check decorated functions to perform arbitrary
Python validation logic.

Usage in checks.py:
    from tolokaforge.core.grading.checks_interface import (
        CheckContext, CheckPassed, CheckFailed, CheckSkipped, init, check
    )

    @init(interface_version="1.0")
    def setup(ctx: CheckContext):
        global data
        data = ctx.effects.get("my_data", {})

    @check
    def my_check():
        if condition:
            return CheckPassed("Everything is good")
        return CheckFailed("Something went wrong")
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import Enum
from typing import Any, Union

from pydantic import BaseModel, Field

# =============================================================================
# Version Constants
# =============================================================================

CHECKS_INTERFACE_VERSION = "1.0"
SUPPORTED_VERSIONS = ["1.0"]


# =============================================================================
# Enums
# =============================================================================


class CheckStatus(str, Enum):
    """Result status of a check"""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


class ToolCallStatus(str, Enum):
    """Status of a tool call"""

    SUCCESS = "success"
    ERROR = "error"


# =============================================================================
# Input Models - Data provided TO the checks
# =============================================================================


class ToolCall(BaseModel):
    """Represents a tool call made during the episode"""

    name: str = Field(..., description="Tool name")
    arguments: dict[str, Any] = Field(default_factory=dict, description="Tool arguments")
    result: str | None = Field(None, description="Tool result (may be truncated)")
    status: ToolCallStatus = Field(default=ToolCallStatus.SUCCESS)
    timestamp: datetime | None = Field(None, description="When the call was made")

    model_config = {"use_enum_values": True}


class Message(BaseModel):
    """Represents a message in the conversation"""

    role: str = Field(..., description="Message role: 'user', 'assistant', 'system', 'tool'")
    content: str = Field(..., description="Message content")
    tool_calls: list[ToolCall] = Field(
        default_factory=list, description="Tool calls in this message"
    )
    name: str | None = Field(None, description="Name for tool messages")

    model_config = {"extra": "allow"}  # Allow additional fields for forward compat


class Transcript(BaseModel):
    """Full conversation transcript"""

    messages: list[Message] = Field(default_factory=list)

    @property
    def agent_messages(self) -> list[Message]:
        """Get only assistant messages"""
        return [m for m in self.messages if m.role == "assistant"]

    @property
    def user_messages(self) -> list[Message]:
        """Get only user messages"""
        return [m for m in self.messages if m.role == "user"]

    @property
    def all_tool_calls(self) -> list[ToolCall]:
        """Get all tool calls from all messages"""
        calls = []
        for m in self.messages:
            calls.extend(m.tool_calls)
        return calls

    @property
    def last_assistant_response(self) -> str | None:
        """Get the last assistant message content"""
        for m in reversed(self.messages):
            if m.role == "assistant":
                return m.content
        return None


class EnvironmentState(BaseModel):
    """Environment state at a point in time"""

    data: dict[str, Any] = Field(default_factory=dict, description="Full state data")

    def get(self, path: str, default: Any = None) -> Any:
        """
        Get value by dot-notation path like 'users.user_123.name'

        Args:
            path: Dot-separated path to the value
            default: Default value if path not found

        Returns:
            Value at path or default
        """
        parts = path.split(".")
        current = self.data
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part)
                if current is None:
                    return default
            else:
                return default
        return current

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def keys(self):
        return self.data.keys()

    def items(self):
        return self.data.items()

    def values(self):
        return self.data.values()

    def __contains__(self, key: str) -> bool:
        return key in self.data


class TaskContext(BaseModel):
    """Context information about the task"""

    task_id: str = Field(..., description="Unique task identifier")
    task_name: str = Field(default="", description="Human-readable task name")
    task_description: str = Field(default="", description="Task description")
    domain: str = Field(default="", description="Task domain (e.g., 'airline', 'retail')")
    tags: list[str] = Field(default_factory=list, description="Task tags")
    extra: dict[str, Any] = Field(default_factory=dict, description="Additional context")


class CheckContext(BaseModel):
    """
    Complete context provided to check functions.

    This is the main input model that @init functions receive.
    It provides all data needed for validation checks.

    Attributes:
        interface_version: Version of the interface for compatibility checking
        initial_state: Environment state before the episode started
        final_state: Environment state after the episode ended
        transcript: Full conversation transcript with all messages
        task: Task metadata including ID, name, description

    Properties:
        effects: Alias for final_state.data (legacy tool-use compatibility)
        tool_calls: All tool calls from the transcript
        response: Last assistant response text
    """

    # Version for interface compatibility
    interface_version: str = Field(default=CHECKS_INTERFACE_VERSION)

    # Core data
    initial_state: EnvironmentState = Field(..., description="State before episode")
    final_state: EnvironmentState = Field(..., description="State after episode")
    transcript: Transcript = Field(..., description="Full conversation transcript")
    task: TaskContext = Field(..., description="Task metadata")

    # Convenience accessors
    @property
    def effects(self) -> dict[str, Any]:
        """Get final state data (legacy tool-use compatibility)"""
        return self.final_state.data

    @property
    def tool_calls(self) -> list[ToolCall]:
        """Get all tool calls from transcript"""
        return self.transcript.all_tool_calls

    @property
    def response(self) -> str | None:
        """Get last assistant response"""
        return self.transcript.last_assistant_response


# =============================================================================
# Output Models - Check Results (Declarative API)
# =============================================================================


class CheckPassed(BaseModel):
    """
    Return this from a @check function to indicate success.

    Usage:
        @check
        def my_check():
            if everything_is_good:
                return CheckPassed("Validation passed")
            return CheckFailed("Something broken")
    """

    message: str = Field(default="Check passed")
    score: float = Field(default=1.0, ge=0.0, le=1.0)
    details: dict[str, Any] = Field(default_factory=dict)

    def __init__(self, message: str = "Check passed", **data):
        """Allow positional argument for message for convenience."""
        super().__init__(message=message, **data)


class CheckFailed(BaseModel):
    """
    Return this from a @check function to indicate failure.

    Usage:
        @check
        def my_check():
            if not valid:
                return CheckFailed("Expected X but got Y", details={"got": Y})
    """

    message: str = Field(default="Check failed")
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    details: dict[str, Any] = Field(default_factory=dict)

    def __init__(self, message: str = "Check failed", **data):
        """Allow positional argument for message for convenience."""
        super().__init__(message=message, **data)


class CheckSkipped(BaseModel):
    """
    Return this from a @check function to skip the check.

    Use when prerequisites aren't met but that's not a failure.

    Usage:
        @check
        def my_check():
            if precondition_not_met:
                return CheckSkipped("Precondition X not met, skipping")
    """

    message: str = Field(default="Check skipped")

    def __init__(self, message: str = "Check skipped", **data):
        """Allow positional argument for message for convenience."""
        super().__init__(message=message, **data)


# Type alias for check function return types
CheckReturnType = Union[CheckPassed, CheckFailed, CheckSkipped]


class CheckResult(BaseModel):
    """
    Internal result of a single check (used by framework).

    This is what the CheckRunner produces after executing a check function.
    """

    check_name: str = Field(..., description="Name/identifier of the check")
    status: CheckStatus = Field(..., description="Pass/fail/error/skip status")
    score: float = Field(default=1.0, ge=0.0, le=1.0, description="Score from 0 to 1")
    message: str = Field(default="", description="Human-readable result message")
    details: dict[str, Any] = Field(default_factory=dict, description="Additional details")

    model_config = {"use_enum_values": True}


class CheckResultSet(BaseModel):
    """Collection of check results from running all checks in a module"""

    results: list[CheckResult] = Field(default_factory=list)
    error: str | None = Field(None, description="Top-level error if checks couldn't run")
    execution_time_ms: float = Field(default=0.0, description="Total execution time")

    @property
    def passed(self) -> int:
        """Count of passed checks"""
        return sum(1 for r in self.results if r.status == CheckStatus.PASSED)

    @property
    def failed(self) -> int:
        """Count of failed checks"""
        return sum(1 for r in self.results if r.status == CheckStatus.FAILED)

    @property
    def errors(self) -> int:
        """Count of errored checks"""
        return sum(1 for r in self.results if r.status == CheckStatus.ERROR)

    @property
    def skipped(self) -> int:
        """Count of skipped checks"""
        return sum(1 for r in self.results if r.status == CheckStatus.SKIPPED)

    @property
    def total(self) -> int:
        """Total number of checks"""
        return len(self.results)

    @property
    def aggregate_score(self) -> float:
        """Average score across all checks (excluding skipped)"""
        scored = [r for r in self.results if r.status != CheckStatus.SKIPPED]
        if not scored:
            return 0.0
        return sum(r.score for r in scored) / len(scored)

    @property
    def all_passed(self) -> bool:
        """True if all checks passed (ignoring skipped)"""
        scored = [r for r in self.results if r.status != CheckStatus.SKIPPED]
        return all(r.status == CheckStatus.PASSED for r in scored)


# =============================================================================
# Decorator-based API
# =============================================================================

# Module-level registry for decorated functions
# These are populated when checks.py is loaded
_check_registry: dict[str, Callable[[], CheckReturnType]] = {}
_init_func: Callable[[CheckContext], None] | None = None
_interface_version: str = CHECKS_INTERFACE_VERSION


def init(interface_version: str = CHECKS_INTERFACE_VERSION):
    """
    Decorator to mark the initialization function.

    The init function is called once before any checks with the CheckContext.
    Use it to extract data from the context into module-level globals that
    your check functions can access.

    Args:
        interface_version: The interface version this checks.py was written for.
                          Default is the current version.

    Usage:
        @init(interface_version="1.0")
        def setup(ctx: CheckContext):
            global reservations, users
            reservations = ctx.effects.get("reservations", {})
            users = ctx.effects.get("users", {})
    """

    def decorator(func: Callable[[CheckContext], None]) -> Callable[[CheckContext], None]:
        global _init_func, _interface_version
        _init_func = func
        _interface_version = interface_version
        return func

    return decorator


def check(func: Callable[[], CheckReturnType]) -> Callable[[], CheckReturnType]:
    """
    Decorator to mark a function as a check.

    The function name becomes the check name in results.
    Function should take no arguments and return CheckPassed, CheckFailed,
    or CheckSkipped.

    Usage:
        @check
        def reservation_was_cancelled():
            cancelled = [r for r in reservations if r["status"] == "cancelled"]
            if len(cancelled) == 1:
                return CheckPassed("Found 1 cancelled reservation")
            return CheckFailed(f"Expected 1 cancelled, found {len(cancelled)}")
    """
    _check_registry[func.__name__] = func
    return func


def get_registered_checks() -> dict[str, Callable[[], CheckReturnType]]:
    """Get all registered check functions"""
    return _check_registry.copy()


def get_init_func() -> Callable[[CheckContext], None] | None:
    """Get the registered init function"""
    return _init_func


def get_interface_version() -> str:
    """Get the declared interface version"""
    return _interface_version


def reset_registry():
    """
    Reset the registry to empty state.

    Call this before loading a new checks.py module to ensure
    clean state. Used by CheckRunner.
    """
    global _check_registry, _init_func, _interface_version
    _check_registry = {}
    _init_func = None
    _interface_version = CHECKS_INTERFACE_VERSION


# =============================================================================
# Configuration for grading.yaml
# =============================================================================


class CustomChecksConfig(BaseModel):
    """
    Configuration for custom checks in grading.yaml.

    Example grading.yaml:
        custom_checks:
          enabled: true
          file: "checks.py"
          interface_version: "1.0"
          timeout_seconds: 30
          weight: 1.0
          fail_on_error: true
          relative_imports:
            - "../.."
    """

    enabled: bool = Field(default=False, description="Whether custom checks are enabled")
    file: str = Field(default="checks.py", description="Path to checks file (relative to task dir)")
    interface_version: str = Field(default="1.0", description="Expected interface version")
    timeout_seconds: float = Field(default=30.0, description="Max execution time")
    weight: float = Field(default=1.0, ge=0.0, le=1.0, description="Weight in final score")
    fail_on_error: bool = Field(default=True, description="Treat errors as failures")
    relative_imports: list[str] = Field(
        default_factory=list,
        description="Additional paths (relative to task dir) to add to sys.path",
    )


# =============================================================================
# Public API exports
# =============================================================================

__all__ = [
    # Version constants
    "CHECKS_INTERFACE_VERSION",
    "SUPPORTED_VERSIONS",
    # Enums
    "CheckStatus",
    "ToolCallStatus",
    # Input models
    "ToolCall",
    "Message",
    "Transcript",
    "EnvironmentState",
    "TaskContext",
    "CheckContext",
    # Output models
    "CheckPassed",
    "CheckFailed",
    "CheckSkipped",
    "CheckResult",
    "CheckResultSet",
    # Decorators
    "init",
    "check",
    # Registry access
    "get_registered_checks",
    "get_init_func",
    "get_interface_version",
    "reset_registry",
    # Configuration
    "CustomChecksConfig",
]
