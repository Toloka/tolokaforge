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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

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
    """One coding-harness CLI: how to install it, how to drive it.

    Every per-harness parity knob lives on this dataclass so the answer to
    "how do I add / fix a harness" has one address. TECHDEL-569 tracks the
    consolidation follow-up (Pydantic + YAML overlay per ADR 0002)."""

    npm_package: str
    """Global npm package providing the CLI."""

    version: str
    """Exact version installed, or the literal ``"latest"``.

    Pinned by default because the agent is the largest variable in a coding
    benchmark: the layered image tag is stable, and the orchestrator skips a
    build whose tag already resolves locally, so a floating version would
    freeze per-machine on first build and differ between contributors.

    ``"latest"`` is available for exploration runs; when used, ``install-harness.sh``
    records the resolved version to a file the adapter reads back at
    trial-registration time and stamps on ``TaskDescription.metadata`` — so
    reproducibility is preserved by the *artifact*, not by the pin. An
    operator can also override per-run via
    :attr:`TerminalBenchAdapter.agent_harness_version_override`."""

    argv_prefix: tuple[str, ...]
    """Words before the flags block. The CLI executable (and its sub-command,
    if any) — e.g. ``("claude",)`` or ``("codex", "exec")``. Static env
    hardening (``IS_SANDBOX=1`` etc.) does NOT belong here — put it on
    :attr:`container_env` so the compose ``environment:`` block carries it."""

    argv_suffix: tuple[str, ...]
    """Words between the flags block and the trailing instruction argument.
    Typically the mandatory ``--permission-mode=…`` / ``--print`` group."""

    model_flag: str = "--model"
    """CLI flag that receives the model name. Ignored when
    :attr:`env_model_vars` is non-empty — env carries the model instead, and
    the CLI flag would be redundant."""

    pre_exec_shell: str = ""
    """Shell script chained before the CLI with ``&&``. Non-empty for CLIs that
    read runtime configuration from a file the compose ``environment:`` block
    alone can't populate — codex reads ``openai_base_url`` from
    ``$CODEX_HOME/config.toml`` and drops ``$OPENAI_BASE_URL`` on the floor
    otherwise (harbor writes the same file, ``harbor/agents/installed/codex.py:1406``).
    Runs inside the task container's default shell alongside the CLI, so it
    can reference any forwarded provider env var by name."""

    flags_pre_permission: tuple[str, ...] = ()
    """Flags inserted between the CLI executable and the model flag / argv
    suffix. Harbor invokes claude-code as
    ``claude --verbose --output-format=stream-json --permission-mode=… --print``;
    this field is where ``--verbose --output-format=stream-json`` land.
    Aligning the flag block aligns the CLI's internal reasoning mode — the
    principal source of the pipeline-vs-pipeline reward delta on
    ``fix-billing-holds`` before the parity work."""

    instruction_channel: Literal["argv", "stdin"] = "argv"
    """How the task instruction reaches the CLI. ``"argv"`` — the trailing
    positional argument (current default). ``"stdin"`` — piped in via
    ``printf "%s" '<instr>' | cli …`` so the shell never re-interprets any
    character in the prompt. Harbor uses stdin for claude-code; the
    positional form was TF's default and worked in practice, but stdin is
    the shape aligning-to-Harbor calls for."""

    env_model_vars: tuple[str, ...] = ()
    """Env variable names that carry the model name into the CLI. Non-empty
    for CLIs whose sub-agents (``Task``, ``Explore``) resolve their model
    independently of the top-level ``--model`` flag: without the env quartet,
    ``Task`` sub-agents fall back to the CLI's default model — a different
    provider mid-trial — even when ``--model`` is set on the outer CLI. When
    non-empty, ``harness_command`` chains ``export VAR=<model>`` for each
    into the pre-exec shell AND drops the redundant ``--model`` CLI arg.
    Claude Code's harbor invocation sets ``ANTHROPIC_MODEL`` and its
    ``_DEFAULT_SONNET_MODEL`` / ``_OPUS_MODEL`` / ``_HAIKU_MODEL`` /
    ``CLAUDE_CODE_SUBAGENT_MODEL`` siblings — a five-var quartet."""

    container_env: dict[str, str] = field(default_factory=dict)
    """Static env pairs the compose ``environment:`` block writes for the
    agent service. Zero-model, one-key-per-behaviour hardening — claude-code
    reads ``IS_SANDBOX=1`` (root-user override) and
    ``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1``. Values must be strings
    (compose interpolation is stringly typed) and must not overlap
    :data:`PROVIDER_ENV_KEYS` (a run-config would then shadow this)."""


HARNESSES: dict[str, HarnessSpec] = {
    "claude-code": HarnessSpec(
        npm_package="@anthropic-ai/claude-code",
        # Pinned to the version harbor's ``bootstrap.sh`` resolved when the
        # matrix comparison was baselined — three patch versions ahead of what
        # TF previously pinned. The pin closes the CLI-code delta with harbor
        # while keeping reproducibility (see :attr:`HarnessSpec.version`).
        version="2.1.231",
        argv_prefix=("claude",),
        # ``--verbose --output-format=stream-json`` matches harbor's invocation
        # (``harbor/agents/installed/claude_code.py``). Aligning the flag block
        # aligns the CLI's internal reasoning mode; without them the CLI ran a
        # different scaffold and made different fixes on the same task.
        flags_pre_permission=("--verbose", "--output-format=stream-json"),
        # ``--permission-mode=bypassPermissions`` is mandatory: without it the
        # CLI blocks at the first tool-permission prompt in ``--print`` mode
        # and burns the whole episode budget without ever calling the LLM.
        argv_suffix=(
            "--permission-mode=bypassPermissions",
            "--print",
        ),
        # Instruction on stdin sidesteps every shell-escape edge case a
        # positional-arg prompt would have to survive (a natural user request
        # can contain quotes, ``$``, backticks, newlines). Harbor pipes the
        # instruction the same way.
        instruction_channel="stdin",
        # The env quartet forces every sub-agent (``Task``, ``Explore``,
        # ``Plan``, ``general-purpose``) to use the declared model. With
        # ``--model`` on the CLI alone, sub-agents fall back to the CLI's
        # sonnet-default and may pick a different provider mid-trial — a
        # silent divergence from the declared benchmark model.
        env_model_vars=(
            "ANTHROPIC_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "CLAUDE_CODE_SUBAGENT_MODEL",
        ),
        # ``IS_SANDBOX=1`` is claude-code's documented root-user override:
        # without it, ``--permission-mode=bypassPermissions`` (which the CLI
        # rewrites internally to ``--dangerously-skip-permissions``) refuses
        # to run under UID 0 and exits before the model is called. The task
        # container is root by default. Harbor sets the same env for the
        # same reason.
        # ``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`` disables the CLI's
        # opt-in telemetry / analytics fetches — harbor sets it too.
        container_env={
            "IS_SANDBOX": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        },
    ),
    "codex": HarnessSpec(
        npm_package="@openai/codex",
        version="0.147.0",
        argv_prefix=("codex", "exec"),
        # Three mandatory flags. ``--dangerously-bypass-approvals-and-sandbox``
        # skips the approval prompt that ``codex exec`` otherwise blocks on when
        # it wants to write or run a command — the harness has no interactive
        # stdin to answer it. ``--skip-git-repo-check`` disables the "trusted
        # directory" gate that refuses to operate anywhere without a ``.git`` —
        # tbench task containers work under ``/app`` and are not git repos.
        # ``-c model_reasoning_effort=high`` is the OpenRouter-compat mandatory
        # config: gpt-5-mini via the Responses API rejects requests that omit
        # reasoning ("Reasoning is mandatory for this endpoint and cannot be
        # disabled"). Harbor's own invocation carries the same override
        # (``harbor/agents/installed/codex.py`` — its default codex command).
        argv_suffix=(
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "-c",
            "model_reasoning_effort=high",
        ),
        # Two on-disk files, one shell chain — codex reads both and honours
        # neither the env var they mirror:
        #
        # 1. ``$CODEX_HOME/config.toml`` carries ``openai_base_url``. The env
        #    var ``OPENAI_BASE_URL`` alone is ignored for the Responses API
        #    endpoint (codex hits ``api.openai.com`` regardless).
        # 2. ``$CODEX_HOME/auth.json`` carries the API key as JSON. The env
        #    var ``OPENAI_API_KEY`` alone earns "401 No cookie auth
        #    credentials found" from OpenRouter — the CLI sends no
        #    ``Authorization`` header without the file.
        #
        # Harbor writes both files (``harbor/agents/installed/codex.py:1391``
        # / ``:1406``). ``CODEX_HOME`` defaults to ``$HOME/.codex``.
        pre_exec_shell=(
            'CODEX_HOME_DIR="${CODEX_HOME:-$HOME/.codex}" && '
            'mkdir -p "$CODEX_HOME_DIR" && '
            'printf \'openai_base_url = "%s"\\n\' "$OPENAI_BASE_URL" '
            '> "$CODEX_HOME_DIR/config.toml" && '
            'printf \'{"OPENAI_API_KEY": "%s"}\\n\' "$OPENAI_API_KEY" '
            '> "$CODEX_HOME_DIR/auth.json"'
        ),
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

_HARNESSES_STRIPPING_VENDOR_NAMESPACE: frozenset[str] = frozenset({"codex", "gemini-cli"})
"""Harnesses whose CLI wants a bare model name (``gpt-5-mini``) rather than an
OpenRouter-style ``vendor/model`` (``openai/gpt-5-mini``).

Claude Code stays out of this set: with ``ANTHROPIC_BASE_URL`` pointing at
OpenRouter, its OpenRouter catalog entry IS ``anthropic/claude-sonnet-4-6``
and harbor forwards that whole string in ``ANTHROPIC_MODEL``. Codex asked
for ``openai/gpt-5-mini`` prints ``Model metadata for openai/gpt-5-mini not
found`` and drops OPENAI_BASE_URL, hitting hard-coded ``api.openai.com``
— harbor's fix is ``model.split('/')[-1]`` (``harbor/agents/installed/codex.py:1341``)."""

_VENDOR_NAMESPACE_PREFIXES: tuple[str, ...] = (
    "anthropic/",
    "openai/",
    "google/",
    "x-ai/",
    "meta-llama/",
    "moonshotai/",
    "qwen/",
    "deepseek/",
)
"""OpenRouter ``vendor/`` namespaces harnesses in
:data:`_HARNESSES_STRIPPING_VENDOR_NAMESPACE` drop from the model name. A new
namespace would surface as the same "metadata not found" warning."""

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


def harness_model(model: str, agent_harness: str | None = None) -> str:
    """Model name as *agent_harness*'s CLI must receive it.

    Always strips the ``openrouter/`` route marker (see
    :data:`OPENROUTER_PREFIX`); a vendor CLI does not go through litellm and
    would otherwise select its own direct-vendor handler and fail with 401.

    For harnesses in :data:`_HARNESSES_STRIPPING_VENDOR_NAMESPACE` (codex,
    gemini-cli), also strips a leading ``vendor/`` namespace so the model
    name is the CLI-catalog bare form (``gpt-5-mini`` from
    ``openrouter/openai/gpt-5-mini``). Claude Code stays out of this set;
    with ``ANTHROPIC_BASE_URL`` pointing at OpenRouter its catalog entry
    IS ``anthropic/claude-sonnet-4-6``.

    *agent_harness* defaults to ``None`` for older callers that only need the
    ``openrouter/`` strip; when unknown, no vendor-namespace stripping happens.
    """
    if model.startswith(OPENROUTER_PREFIX):
        model = model[len(OPENROUTER_PREFIX) :]
    if agent_harness in _HARNESSES_STRIPPING_VENDOR_NAMESPACE:
        for prefix in _VENDOR_NAMESPACE_PREFIXES:
            if model.startswith(prefix):
                return model[len(prefix) :]
    return model


def harness_command(agent_harness: str, instruction: str, model: str) -> str:
    """Shell command that runs *agent_harness* against *instruction*.

    Assembly order (blank pieces drop out):

        <pre_exec_shell> &&
        <export VAR=<model> && …>  # one per HarnessSpec.env_model_vars
        <printf "%s" '<instr>' |>  # only when instruction_channel == "stdin"
        <argv_prefix> <flags_pre_permission>
        <--model <model>>          # only when env_model_vars is empty
        <argv_suffix>
        <'<instr>'>                # only when instruction_channel == "argv"

    Every argv token is shell-quoted; the compose-exec tool wrapper hands the
    result to ``bash -c`` inside the task container, so an instruction
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
    resolved_model = harness_model(model, agent_harness)

    # Pre-exec preamble: on-disk config-file emission + env-quartet exports.
    preamble_parts: list[str] = []
    if spec.pre_exec_shell:
        preamble_parts.append(spec.pre_exec_shell)
    for var in spec.env_model_vars:
        preamble_parts.append(f"export {var}={shlex.quote(resolved_model)}")
    preamble = " && ".join(preamble_parts)

    # CLI argv (without the instruction when it's coming on stdin).
    cli_tokens: list[str] = [*spec.argv_prefix, *spec.flags_pre_permission]
    if not spec.env_model_vars and spec.model_flag:
        cli_tokens.extend([spec.model_flag, resolved_model])
    cli_tokens.extend(spec.argv_suffix)
    if spec.instruction_channel == "argv":
        cli_tokens.append(instruction)
    cli_command = " ".join(shlex.quote(part) for part in cli_tokens)

    if spec.instruction_channel == "stdin":
        cli_command = f"printf %s {shlex.quote(instruction)} | {cli_command}"

    if preamble:
        return f"{preamble} && {cli_command}"
    return cli_command
