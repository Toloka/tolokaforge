"""Env-var parsers for operational overrides on numeric config knobs.

Precedence contract: caller resolves ``env → config → default`` at the
callsite; these helpers cover the leftmost hop. Missing env var returns
``default`` unchanged; a value that fails to parse or falls outside the
non-negative / positive band logs a warning through the caller-provided
logger and returns ``default`` (never raises — an operational override
should never take a running trial down).
"""

from __future__ import annotations

import os
from typing import Any, Protocol

__all__ = ["parse_env_non_negative_int", "parse_env_positive_float"]


class _StructuredWarn(Protocol):
    def warning(self, msg: str, **fields: Any) -> None: ...


def parse_env_positive_float(
    name: str,
    default: float | None,
    *,
    logger: _StructuredWarn,
) -> float | None:
    """Read ``name`` as a strictly positive float; else ``default``."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
        if value <= 0:
            raise ValueError("must be positive")
        return value
    except ValueError:
        logger.warning("Invalid env-var float; ignoring", env_var=name, value=raw, default=default)
        return default


def parse_env_non_negative_int(
    name: str,
    default: int | None,
    *,
    logger: _StructuredWarn,
) -> int | None:
    """Read ``name`` as a non-negative int; else ``default``."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
        if value < 0:
            raise ValueError("must be non-negative")
        return value
    except ValueError:
        logger.warning("Invalid env-var int; ignoring", env_var=name, value=raw, default=default)
        return default
