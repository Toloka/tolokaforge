"""Universal LLM Tool-Use Benchmarking Harness (ULB-H)"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

# Single source of truth: the version literal lives only in pyproject.toml
# (bumped by `cz bump`); we read it back from the installed distribution
# metadata so this can never drift out of sync. Falls back gracefully when
# running from a bare source checkout that has not been installed.
try:
    __version__ = _pkg_version("tolokaforge")
except PackageNotFoundError:  # pragma: no cover - source checkout without an install
    __version__ = "0.0.0+unknown"

# ---------------------------------------------------------------------------
# Lazy public API — symbols are loaded on first access, not at import time.
# This keeps `import tolokaforge` (and any sub-package import such as
# `from tolokaforge.core.tools_interface import DomainToolRegistry`) cheap:
# heavy dependencies (litellm, aiohttp, sqlalchemy, …) are only pulled in
# when the caller actually uses Orchestrator / metrics / etc.
# ---------------------------------------------------------------------------

_lazy: dict[str, str] = {
    "Orchestrator": "tolokaforge.core.orchestrator",
    "RunConfig": "tolokaforge.core.models",
    "TaskConfig": "tolokaforge.core.models",
    "Trajectory": "tolokaforge.core.models",
    "Grade": "tolokaforge.core.models",
    "compute_pass_at_k": "tolokaforge.core.metrics",
    "calculate_task_metrics": "tolokaforge.core.metrics",
    "calculate_aggregate_metrics": "tolokaforge.core.metrics",
    "attribute_failure": "tolokaforge.core.failure_attribution",
    "summarize_failure_attributions": "tolokaforge.core.failure_attribution",
    "create_run_queue": "tolokaforge.core.run_queue",
}


def __getattr__(name: str):
    if name in _lazy:
        import importlib

        mod = importlib.import_module(
            _lazy[name]
        )  # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
        obj = getattr(mod, name)
        globals()[name] = obj  # cache — subsequent access skips __getattr__
        return obj
    raise AttributeError(f"module 'tolokaforge' has no attribute {name!r}")


__all__ = [
    "Orchestrator",
    "RunConfig",
    "TaskConfig",
    "Trajectory",
    "Grade",
    "compute_pass_at_k",
    "calculate_task_metrics",
    "calculate_aggregate_metrics",
    "attribute_failure",
    "summarize_failure_attributions",
    "create_run_queue",
    "__version__",
]
