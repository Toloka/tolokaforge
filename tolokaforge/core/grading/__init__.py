"""Grading system

This package provides the grading infrastructure for TolokaForge tasks.

The entry points are :mod:`~tolokaforge.core.grading.combine` (the in-process
core substrate) and the names re-exported below; every author-facing
``grading.yaml`` key and the substrate that evaluates it are enumerated in
:mod:`~tolokaforge.core.grading.key_manifest`, which is the map worth reading
first.
"""

from tolokaforge.core.grading.check_runner import (
    CheckExecutor,
    CheckExecutorCallLog,
    CheckRunner,
    InMemoryCheckExecutor,
    run_custom_checks,
)
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
    # Executor seam (ADR-0012 / Pattern A)
    "CheckExecutor",
    "CheckExecutorCallLog",
    "CheckRunner",
    "InMemoryCheckExecutor",
    "run_custom_checks",
    # State comparison
    "ComparisonResult",
    "FieldDifference",
    "FuzzyStateComparator",
    "HashComparator",
    "StateComparator",
    "create_comparator",
]
