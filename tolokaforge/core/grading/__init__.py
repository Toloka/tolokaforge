"""Grading system

This package provides the grading infrastructure for TolokaForge tasks.

Modules:
- state_checks: Environment state validation (jsonpaths, hash)
- transcript: Transcript analysis and tool usage checks
- judge: LLM-based evaluation
- combine: Combines multiple grading methods
- checks_interface: Custom Python checks Pydantic models and decorators
- checks_helpers: Generic helper functions for custom checks
- check_runner: Execution engine for custom checks
- fuzzy_compare: Field-level fuzzy state comparison
"""

from tolokaforge.core.grading.check_runner import CheckRunner, run_custom_checks
from tolokaforge.core.grading.checks_interface import (
    CHECKS_INTERFACE_VERSION,
    SUPPORTED_VERSIONS,
    CheckContext,
    CheckFailed,
    CheckPassed,
    CheckResult,
    CheckResultSet,
    CheckSkipped,
    CheckStatus,
    CustomChecksConfig,
    EnvironmentState,
    Message,
    TaskContext,
    ToolCall,
    Transcript,
    check,
    get_init_func,
    get_interface_version,
    get_registered_checks,
    init,
    reset_registry,
)
from tolokaforge.core.grading.fuzzy_compare import (
    ComparisonResult,
    FieldDifference,
    FuzzyStateComparator,
    HashComparator,
    StateComparator,
    create_comparator,
)

__all__ = [
    # Interface version
    "CHECKS_INTERFACE_VERSION",
    "SUPPORTED_VERSIONS",
    # Input models
    "CheckContext",
    "EnvironmentState",
    "Message",
    "TaskContext",
    "ToolCall",
    "Transcript",
    # Output models
    "CheckPassed",
    "CheckFailed",
    "CheckSkipped",
    "CheckResult",
    "CheckResultSet",
    "CheckStatus",
    # Decorators
    "init",
    "check",
    # Registry
    "get_registered_checks",
    "get_init_func",
    "get_interface_version",
    "reset_registry",
    # Config
    "CustomChecksConfig",
    # Runner
    "CheckRunner",
    "run_custom_checks",
    # State comparison
    "ComparisonResult",
    "FieldDifference",
    "FuzzyStateComparator",
    "HashComparator",
    "StateComparator",
    "create_comparator",
]
