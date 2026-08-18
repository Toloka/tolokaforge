"""Runtime-shaped contracts the harness registry is written against.

A :class:`~tolokaforge_coding_harnesses.HarnessSpec` describes one CLI, not one
runtime. The conventions of the runtime it lands in — where a home directory is,
where a config file belongs — are the property of whoever drives the adapter, and
reach it through the contracts in this module.

Deliberately free of Docker: an implementation of one of these contracts may be
image-shaped, but the contract itself is what a second runtime implements to
consume the *same* registry data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

__all__ = [
    "PATH_CONSTRUCT_PATTERN",
    "PathResolver",
    "SkillDelivery",
    "SkillsBundle",
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


@dataclass(frozen=True)
class SkillsBundle:
    """A task pack's skills bundle, ready to reach a harness runtime."""

    task_dir: Path
    """Absolute path to the task-pack root the bundle ships inside."""

    source_rel: str
    """Bundle directory relative to :attr:`task_dir` — the task's
    ``harness_skills_dir``."""

    target: str
    """Directory inside the runtime the CLI reads skills from, already through
    the run's :class:`PathResolver` — absolute unless that resolver deferred a
    construct it does not know."""

    staging_dir: Path
    """Absolute path to the materialised trial substrate."""


@runtime_checkable
class SkillDelivery(Protocol):
    """Put a task pack's skills bundle where the CLI will read it.

    Four clauses an implementation is written against:

    - :meth:`deliver` is called **at most once per materialised task**, and
      only when the task ships a bundle *and* the selected harness declares a
      ``skills_dir_target``.
    - It is called **after** the harness build context exists at
      ``staging_dir/_harness/`` and **before** the synthesised compose file is
      written, so an implementation may contribute to either.
    - :attr:`SkillsBundle.target` has been through the run's
      :class:`PathResolver`. Delivery never resolves paths; the two seams
      compose in exactly one direction.
    - Raising aborts materialisation. A delivery that cannot place the bundle
      must not return quietly — a trial whose agent had no skills must never
      read back as one that did.
    """

    def deliver(self, bundle: SkillsBundle) -> None: ...
