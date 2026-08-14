"""Runtime-shaped contracts the harness registry is written against.

A :class:`~tolokaforge_adapter_terminal_bench.harness.HarnessSpec` describes one
CLI, not one runtime. The conventions of the runtime it lands in — where a home
directory is, where a config file belongs — are the property of whoever drives
the adapter, and reach it through the contracts in this module.

Deliberately free of Docker: an implementation of one of these contracts may be
image-shaped, but the contract itself is what a second runtime implements to
consume the *same* registry data.
"""

from __future__ import annotations

import re
from typing import Final, Protocol, runtime_checkable

__all__ = [
    "PATH_CONSTRUCT_PATTERN",
    "PathResolver",
]

PATH_CONSTRUCT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}"
)
"""The whole path vocabulary a registry entry may write: ``${VAR}`` and
``${VAR:-default}``.

Part of the contract rather than any one resolver's implementation detail — a
resolver, the ``skills_dir_target`` validator, and the tests that read a
variable name out of a path all match against this one pattern.

A brace-less ``$VAR`` is deliberately *not* a construct: leaving it alone is
what keeps ``$HOME`` inside codex's ``${CODEX_HOME:-$HOME/.codex}`` default
clause in the hands of the container's own shell.
"""


@runtime_checkable
class PathResolver(Protocol):
    """Where a :attr:`HarnessSpec` path lands in this runtime.

    Three clauses, and an implementation is only correct if it honours all
    three:

    - A path carrying no :data:`PATH_CONSTRUCT_PATTERN` construct comes back
      unchanged.
    - A construct whose variable is in the resolver's vocabulary is replaced by
      the resolver's value. A ``:-default`` clause on a known name is
      **discarded** — the resolver is the authority on its own vocabulary, so a
      registry entry's fallback never overrides it.
    - A construct whose variable is unknown is the runtime's decision. A
      resolver whose runtime expands variables itself (a POSIX shell in the
      task container) leaves the construct verbatim for that shell; a resolver
      whose runtime cannot expand anything (a native process, a remote API)
      raises :class:`ValueError` naming the variable, the path, and its own
      vocabulary.
    """

    def resolve(self, path: str) -> str: ...
