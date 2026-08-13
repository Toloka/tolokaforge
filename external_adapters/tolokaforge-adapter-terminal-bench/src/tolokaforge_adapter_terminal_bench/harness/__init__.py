"""Coding-harness CLI support for terminal-bench trials.

A harness trial replaces the engine's LLM turn loop with a single invocation
of a vendor coding-harness CLI inside the task container. Several ends have to
agree on the same small set of facts — the image layer that installs the CLI,
the trial that invokes it, and the artifact that records what drove it — so
all of them read :data:`HARNESSES`.

The engine core never learns these names. The adapter resolves the harness to
a concrete shell command and publishes it on
``TaskDescription.metadata["agent_harness_command"]``; the conductor runs
whatever command it finds there.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ACCEPTED_HARNESSES",
    "ENGINE_LOOP",
    "HARNESSES",
    "INSTALL_SCRIPT",
    "OPENROUTER_PREFIX",
    "PROVIDER_ENV_INPUT_PREFIX",
    "PROVIDER_ENV_KEYS",
    "HarnessSpec",
    "harness_command",
    "harness_model",
    "provider_env_input",
    "validate_harness",
    "validate_provider_env_keys",
]

ENGINE_LOOP = "engine-loop"
"""The default: tolokaforge's own turn loop drives the trial.

Named for what actually runs. The trial goes through
:class:`~tolokaforge.core.loop.ToolCallingLoop` with the run config's model,
its own system prompt, and the adapter's ``bash`` tool — a different scaffold
from terminal-bench's Terminus-2 agent, which this repo does not install. A
trial recorded as ``terminus-2`` would be claiming a comparison it did not run.
"""


@dataclass(frozen=True)
class HarnessSpec:
    """One coding-harness CLI: how to install it, how to drive it."""

    npm_package: str
    """Global npm package providing the CLI."""

    version: str
    """Exact version installed. Pinned because the agent is the largest
    variable in a coding benchmark: the layered image tag is stable, and the
    orchestrator skips a build whose tag already resolves locally, so a
    floating version would freeze per-machine on first build, differ between
    contributors, and appear in no artifact."""

    argv_prefix: tuple[str, ...]
    """Words before the model flag."""

    argv_suffix: tuple[str, ...]
    """Words between the model and the trailing instruction argument."""

    model_flag: str = "--model"


HARNESSES: dict[str, HarnessSpec] = {
    "claude-code": HarnessSpec(
        npm_package="@anthropic-ai/claude-code",
        version="2.1.228",
        argv_prefix=("claude",),
        # ``--permission-mode=bypassPermissions`` is mandatory: without it the CLI
        # blocks at the first tool-permission prompt in ``--print`` mode and burns
        # the whole episode budget without ever calling the LLM. Harbor's own
        # invocation carries the same flag for the same reason.
        argv_suffix=(
            "--permission-mode=bypassPermissions",
            "--print",
        ),
    ),
    "codex": HarnessSpec(
        npm_package="@openai/codex",
        version="0.147.0",
        argv_prefix=("codex", "exec"),
        # ``--dangerously-bypass-approvals-and-sandbox`` skips the approval prompt
        # that ``codex exec`` otherwise blocks on when it wants to write or run a
        # command — the harness has no interactive stdin to answer it.
        argv_suffix=("--dangerously-bypass-approvals-and-sandbox",),
    ),
    "gemini-cli": HarnessSpec(
        npm_package="@google/gemini-cli",
        version="0.55.1",
        argv_prefix=("gemini",),
        # ``--yolo`` accepts every tool call without asking. ``--prompt`` sits at
        # the end so it stays adjacent to the trailing instruction argument.
        argv_suffix=(
            "--yolo",
            "--prompt",
        ),
    ),
}

ACCEPTED_HARNESSES: tuple[str, ...] = (ENGINE_LOOP, *HARNESSES)

INSTALL_SCRIPT = Path(__file__).parent / "install-harness.sh"

OPENROUTER_PREFIX = "openrouter/"
"""Route marker litellm reads to select its OpenRouter handler.

A vendor CLI does not go through litellm — it reaches OpenRouter through the
``*_BASE_URL`` variables :data:`PROVIDER_ENV_KEYS` forwards. Left on the model
name, the CLI instead selects its own direct-vendor handler, reads the
deliberately blank vendor key, and fails with a 401. So a vendor harness gets
the prefix stripped, while the engine loop keeps it (litellm needs it).
"""

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

An allow-list, not an open surface: every forwarded value lands in the
per-trial compose ``.env`` and then in the container the agent works in, so an
open surface would let a run config shadow the task's own environment with
arbitrary values."""

PROVIDER_ENV_INPUT_PREFIX = "TBENCH_PROVIDER_"
"""Prefix for the compose variable that carries a provider value.

The synthesised compose file writes ``ANTHROPIC_API_KEY=${TBENCH_PROVIDER_ANTHROPIC_API_KEY}``
rather than naming the provider variable on both sides. Compose resolves
``${VAR}`` from the invoking shell's environment before the per-trial ``.env``,
so an un-prefixed name would let whatever ``ANTHROPIC_API_KEY`` the operator's
shell happens to hold silently replace the value the run config declared —
putting a real key inside a benchmark container and into its trial artifacts.
Nothing sets the prefixed name by accident.
"""


def provider_env_input(key: str) -> str:
    """Compose-input name carrying *key*'s value into the agent service."""
    return f"{PROVIDER_ENV_INPUT_PREFIX}{key}"


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
    if agent_harness not in ACCEPTED_HARNESSES:
        raise ValueError(
            f"terminal-bench adapter: agent_harness {agent_harness!r} is not supported; "
            f"accepted: {list(ACCEPTED_HARNESSES)!r}."
        )
    return agent_harness


def harness_model(model: str) -> str:
    """Model name as a vendor CLI must receive it — see :data:`OPENROUTER_PREFIX`."""
    return model[len(OPENROUTER_PREFIX) :] if model.startswith(OPENROUTER_PREFIX) else model


def harness_command(agent_harness: str, instruction: str, model: str) -> str:
    """Shell command that runs *agent_harness* against *instruction*.

    Every word is quoted for ``sh``: the compose-exec tool wrapper hands the
    returned string to ``bash -c`` inside the task container, so an instruction
    carrying quotes, newlines, or ``$`` must survive verbatim.

    Raises:
        ValueError: *agent_harness* is unknown, or is :data:`ENGINE_LOOP`
            (which runs no CLI, so there is no command to build).
    """
    validate_harness(agent_harness)
    spec = HARNESSES.get(agent_harness)
    if spec is None:
        raise ValueError(
            f"terminal-bench adapter: agent_harness {agent_harness!r} runs no CLI; "
            "the trial goes through the engine's LLM turn loop instead."
        )
    argv = (
        *spec.argv_prefix,
        spec.model_flag,
        harness_model(model),
        *spec.argv_suffix,
        instruction,
    )
    return " ".join(shlex.quote(part) for part in argv)
