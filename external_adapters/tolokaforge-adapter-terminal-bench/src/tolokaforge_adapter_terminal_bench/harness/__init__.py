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
from collections.abc import Iterable
from pathlib import Path

__all__ = [
    "ACCEPTED_HARNESSES",
    "HARNESS_COMMANDS",
    "INSTALL_SCRIPT",
    "NO_OP_HARNESS",
    "PROVIDER_ENV_KEYS",
    "harness_command",
    "validate_harness",
    "validate_provider_env_keys",
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

PROVIDER_ENV_KEYS: frozenset[str] = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "GOOGLE_API_KEY",
    }
)
"""Environment variables a harness CLI may be given inside the task container.

An allow-list rather than a pass-through: everything named here lands in the
per-trial compose ``.env``, so an open surface would let a run config push
arbitrary values — including ones shadowing the task's own environment —
into the container the agent works in."""


def validate_provider_env_keys(keys: Iterable[str]) -> None:
    """Raise unless every key is one a harness CLI is allowed to receive."""
    rejected = sorted(k for k in keys if k not in PROVIDER_ENV_KEYS)
    if rejected:
        raise ValueError(
            f"terminal-bench adapter: agent_provider_env key(s) {rejected!r} are not "
            f"forwardable; accepted: {sorted(PROVIDER_ENV_KEYS)!r}."
        )


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
