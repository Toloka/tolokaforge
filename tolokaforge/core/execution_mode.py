"""How a single trial is driven to completion.

The engine runs a trial in one of two shapes. Under
:attr:`ExecutionMode.ENGINE_LOOP` the engine's own LLM turn loop drives the
agent. Under :attr:`ExecutionMode.DELEGATED` the task brings its own agent —
a coding-harness CLI named on ``TaskDescription.metadata`` — and the engine
provisions and grades the trial without running the loop.

The mode is classified from task metadata at dispatch time by
:func:`select_execution_mode`; it is never written back into metadata or any
wire artifact. This seam lives engine-side only — the coding-harnesses
package (which imports no engine module) keeps its own string capability flag.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any

HARNESS_COMMAND_METADATA_KEY = "agent_harness_command"
"""Task-metadata key whose presence routes a trial to the delegated agent.

A non-blank string is the fully-assembled command the delegated agent is
invoked with; the engine reads it but never derives or writes it."""


class ExecutionMode(str, Enum):
    """The shape one trial runs in.

    ``ENGINE_LOOP`` — the engine's own LLM turn loop drives the agent.
    ``DELEGATED`` — a task-provided agent (a coding-harness CLI) drives the
    trial; the engine provisions and grades but does not run the turn loop.

    Distinct from the hyphenated ``engine-loop`` harness-registry sentinel in
    :mod:`tolokaforge_coding_harnesses`: that names which scaffold a run
    selects, this names which code path drives the trial.
    """

    ENGINE_LOOP = "engine_loop"
    DELEGATED = "delegated"


def select_execution_mode(metadata: Mapping[str, Any]) -> ExecutionMode:
    """Classify how a trial runs from its task metadata.

    Returns :attr:`ExecutionMode.DELEGATED` iff ``agent_harness_command`` is
    present and a non-blank string — the task declares its own agent and the
    assembled command to run it. An absent key returns
    :attr:`ExecutionMode.ENGINE_LOOP`: the engine drives its own turn loop.

    A present-but-blank or non-string command is a broken adapter, not a
    request to run the turn loop, and raises :class:`RuntimeError` naming the
    key — the same fail-loud validation the dispatch carried before.
    """
    harness_command = metadata.get(HARNESS_COMMAND_METADATA_KEY)
    if harness_command is None:
        return ExecutionMode.ENGINE_LOOP
    if not isinstance(harness_command, str) or not harness_command.strip():
        raise RuntimeError(
            f"task metadata {HARNESS_COMMAND_METADATA_KEY!r} must be a non-blank "
            f"string; got {harness_command!r}. Omit the key to run the LLM turn loop."
        )
    return ExecutionMode.DELEGATED
