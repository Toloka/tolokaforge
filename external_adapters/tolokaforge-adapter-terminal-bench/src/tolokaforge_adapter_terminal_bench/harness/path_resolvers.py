"""Shipped :class:`~.protocols.PathResolver` implementations."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from .protocols import PATH_CONSTRUCT_PATTERN, PathResolver

__all__ = [
    "DEFAULT_PATH_RESOLVER",
    "LinuxRootResolver",
]

_LINUX_ROOT_VOCABULARY: Final[Mapping[str, str]] = {
    "HOME": "/root",
    "CONFIG_HOME": "/root/.config",
}


@dataclass(frozen=True)
class LinuxRootResolver:
    """Paths as they land in a task container running its CLI as ``root``.

    The runtime is a POSIX shell inside that container, so an unknown construct
    is **deferred** — left verbatim for the shell to expand. That is what
    codex's ``${CODEX_HOME:-$HOME/.codex}`` relies on, and what keeps an
    operator overlay naming a variable this resolver never heard of working
    exactly as it does without one.

    Deferral is why a mistyped construct in the shipped registry surfaces at CI
    (at the shipped-vocabulary test) rather than at registry load.
    """

    def resolve(self, path: str) -> str:
        def substitute(construct: re.Match[str]) -> str:
            value = _LINUX_ROOT_VOCABULARY.get(construct.group(1))
            return construct.group(0) if value is None else value

        return PATH_CONSTRUCT_PATTERN.sub(substitute, path)


DEFAULT_PATH_RESOLVER: Final[PathResolver] = LinuxRootResolver()
"""The resolver every adapter surface falls back to when a caller names none."""
