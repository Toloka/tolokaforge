"""Coding-harness CLI support for terminal-bench trials.

A harness trial replaces the engine's LLM turn loop with a single invocation
of a vendor coding-harness CLI inside the task container. Two ends have to
agree on the same small set of facts — the image layer that installs the CLI
and the trial that invokes it — so both read them from here:

* :data:`INSTALL_SCRIPT` — the script the synthesised image layer runs.
* :data:`HARNESS_COMMANDS` — the argv each CLI is driven with.

The engine core never learns these names. The adapter resolves the harness
to a concrete shell command and publishes it on
``TaskDescription.metadata["agent_harness_command"]``; the conductor runs
whatever command it finds there.
"""

from __future__ import annotations

import shlex
from pathlib import Path

__all__ = [
    "ACCEPTED_HARNESSES",
    "HARNESS_COMMANDS",
    "INSTALL_SCRIPT",
    "NO_OP_HARNESS",
    "harness_command",
    "validate_harness",
]

NO_OP_HARNESS = "terminus-2"
"""Terminal-bench's own agent. Leaves the task image untouched and keeps the
engine's LLM turn loop — the escape hatch that makes harness mode opt-in."""

INSTALL_SCRIPT = Path(__file__).parent / "install-harness.sh"

HARNESS_COMMANDS: dict[str, tuple[str, ...]] = {
    NO_OP_HARNESS: (),
    "claude-code": ("claude", "--print"),
    "codex": ("codex", "exec"),
    "gemini-cli": ("gemini", "--prompt"),
}
"""Harness name → argv prefix. The task instruction is appended as a single
trailing argument. An empty prefix means the harness runs no CLI."""

ACCEPTED_HARNESSES: tuple[str, ...] = tuple(HARNESS_COMMANDS)


def validate_harness(agent_harness: str) -> str:
    """Return *agent_harness* unchanged, or raise naming the accepted set."""
    if agent_harness not in HARNESS_COMMANDS:
        raise ValueError(
            f"terminal-bench adapter: agent_harness {agent_harness!r} is not supported; "
            f"accepted: {list(ACCEPTED_HARNESSES)!r}."
        )
    return agent_harness


def harness_command(agent_harness: str, instruction: str) -> str:
    """Shell command that runs *agent_harness* against *instruction*.

    Every word is quoted for ``sh``: the compose-exec tool wrapper hands the
    returned string to ``bash -c`` inside the task container, so an
    instruction carrying quotes, newlines, or ``$`` must survive verbatim.

    Raises:
        ValueError: *agent_harness* is unknown, or is the no-op harness
            (which runs no CLI, so there is no command to build).
    """
    argv = HARNESS_COMMANDS[validate_harness(agent_harness)]
    if not argv:
        raise ValueError(
            f"terminal-bench adapter: agent_harness {agent_harness!r} runs no CLI; "
            "the trial goes through the engine's LLM turn loop instead."
        )
    return " ".join(shlex.quote(part) for part in (*argv, instruction))
