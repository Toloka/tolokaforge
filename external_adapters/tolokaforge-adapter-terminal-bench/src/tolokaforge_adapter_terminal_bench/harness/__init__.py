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
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

__all__ = [
    "ENGINE_LOOP",
    "HARNESSES",
    "INSTALL_SCRIPT",
    "OPENROUTER_PREFIX",
    "PROVIDER_ENV_INPUT_PREFIX",
    "PROVIDER_ENV_KEYS",
    "SHIPPED_REGISTRY_FILE",
    "HarnessSpec",
    "accepted_harnesses",
    "harness_command",
    "harness_model",
    "load_harness_registry",
    "provider_env_input",
    "validate_harness",
    "validate_provider_env_keys",
]

_URL_INSTALL_METHODS: frozenset[str] = frozenset({"curl-bash", "binary"})
"""Install methods whose ``install_source`` is downloaded rather than named."""


def _is_package_name(value: str) -> bool:
    """Whether *value* is a bare npm / PyPI installable rather than a path or URL."""
    if value.split() != [value]:
        return False
    scope, _, rest = value.partition("/")
    return "/" not in rest and (not rest or scope.startswith("@"))


ENGINE_LOOP = "engine-loop"
"""The default: tolokaforge's own turn loop drives the trial.

Named for what actually runs. The trial goes through
:class:`~tolokaforge.core.loop.ToolCallingLoop` with the run config's model,
its own system prompt, and the adapter's ``bash`` tool — a different scaffold
from terminal-bench's Terminus-2 agent, which this repo does not install. A
trial recorded as ``terminus-2`` would be claiming a comparison it did not run.
"""


class HarnessSpec(BaseModel):
    """One coding-harness CLI: how to install it, how to drive it.

    Every per-harness parity knob lives on this model so the answer to "how
    do I add / fix a harness" has one address. Pydantic + ``extra="forbid"``
    + ``frozen=True`` per ADR 0011 Pattern B: adding a field requires an ADR
    update and a snapshot regen, and mutation on an instance is refused at
    runtime (the registry is data, not state)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    install_method: Literal["npm", "pip", "curl-bash", "binary"] = "npm"
    """How ``install-harness.sh`` puts the CLI in the image.

    ``npm`` / ``pip`` install :attr:`install_source` by name from the public
    registry; ``curl-bash`` downloads it as an installer script and runs it
    with ``--version <version>``; ``binary`` downloads it as a ``.tar.gz`` /
    ``.tgz`` unpacked onto ``PATH``, or as a bare executable placed there
    under the URL's basename. The URL methods refuse ``version: "latest"``:
    neither can report back what it installed, and an unrecorded agent
    version is not a benchmark result."""

    install_source: str
    """What :attr:`install_method` installs — a package name for ``npm`` /
    ``pip`` (``@scope/`` allowed, nothing else path-shaped), a download URL
    for ``curl-bash`` / ``binary``. Mismatches are refused at registry-load
    time rather than at ``docker build`` time."""

    version: str
    """Exact version installed, or the literal ``"latest"``.

    Pinned by default because the agent is the largest variable in a coding
    benchmark: the layered image tag is stable, and the orchestrator skips a
    build whose tag already resolves locally, so a floating version would
    freeze per-machine on first build and differ between contributors.

    ``"latest"`` is available for exploration runs of the registry methods
    (``npm`` / ``pip``): ``install-harness.sh`` resolves it during the image
    build and records what it actually installed at
    ``/opt/tolokaforge/installed-version.txt`` inside the layer, so the
    container carries the evidence the pin would otherwise have provided."""

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

    container_env: dict[str, str] = Field(default_factory=dict)
    """Static env pairs the compose ``environment:`` block writes for the
    agent service. Zero-model, one-key-per-behaviour hardening — claude-code
    reads ``IS_SANDBOX=1`` (root-user override) and
    ``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1``. Values must be strings
    (compose interpolation is stringly typed) and must not overlap
    :data:`PROVIDER_ENV_KEYS` (a run-config would then shadow this)."""

    strip_vendor_namespace: bool = False
    """Whether ``harness_model`` should strip a leading ``vendor/`` namespace
    from the model name before handing it to the CLI. Non-``False`` for CLIs
    whose model catalog uses bare names (``gpt-5-mini``, not
    ``openai/gpt-5-mini``) — a namespaced string prints "Model metadata for
    <name> not found" and the CLI silently drops OpenRouter routing to hit
    the vendor's default endpoint. Harbor sidesteps the same trap by taking
    the last path segment (``harbor/agents/installed/codex.py:1341``)."""

    provider_env: dict[str, str] = Field(default_factory=dict)
    """Default provider-env envelope for this harness — the shape the CLI
    needs to reach its provider through OpenRouter (or wherever). Populated
    once per harness (``ANTHROPIC_API_KEY`` + ``ANTHROPIC_BASE_URL`` for
    claude-code, ``OPENAI_*`` for codex, ``GOOGLE_API_KEY`` for gemini-cli).
    Values may be literal (URLs pointing at OpenRouter) or reference
    :data:`SecretManager`-resolvable ``${secret:NAME}`` refs.

    The adapter's ``agent_provider_env`` run-config param overlays this
    map key-by-key (union with run-config keys winning on conflict), so a
    caller declaring nothing gets the shipped defaults, a caller
    declaring a different endpoint gets that, and one caller can add a
    key the harness didn't ship (e.g. ``ANTHROPIC_AUTH_TOKEN``) without
    losing the URL. Keys must be a subset of :data:`PROVIDER_ENV_KEYS`."""

    @model_validator(mode="after")
    def _install_source_fits_the_method(self) -> HarnessSpec:
        """Refuse a source the method cannot consume, at load rather than build."""
        if self.install_method in _URL_INSTALL_METHODS:
            if not self.install_source.startswith(("http://", "https://")):
                raise ValueError(
                    f"install_method {self.install_method!r} downloads its source, but "
                    f"install_source {self.install_source!r} is not one "
                    "(expected an http:// or https:// URL)."
                )
        elif not _is_package_name(self.install_source):
            raise ValueError(
                f"install_method {self.install_method!r} installs a named package, but "
                f"install_source {self.install_source!r} is not a bare package name "
                "(no whitespace, and no `/` outside a leading `@scope/`)."
            )
        return self


SHIPPED_REGISTRY_FILE = Path(__file__).resolve().parent.parent / "data" / "harnesses.yaml"
"""Packaged registry data — the source of truth for the shipped harnesses.

Data, not code: adding a harness or bumping a pinned CLI version is a YAML
edit. An operator ships their own entries by pointing the adapter's
``harness_presets_file`` param at a second file of the same shape.
"""


def load_harness_registry(path: Path) -> dict[str, HarnessSpec]:
    """Registry declared by the YAML file at *path*.

    Expected shape::

        harnesses:
          <name>:
            <HarnessSpec field>: <value>

    Every entry is validated by :class:`HarnessSpec` on its own, so an unknown
    field, a missing required one, or a wrong type is refused naming the file,
    the harness key, and the offending field — an operator's typo reads as the
    config error it is instead of surfacing as a trial-time failure.

    Raises:
        ValueError: *path* does not exist, is not valid YAML, carries a
            top-level key other than ``harnesses``, declares no harness, or
            declares an entry :class:`HarnessSpec` rejects.
    """
    if not path.is_file():
        raise ValueError(f"terminal-bench adapter: harness registry file {path} does not exist.")
    try:
        document = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ValueError(
            f"terminal-bench adapter: harness registry file {path} is not valid YAML: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise ValueError(
            f"terminal-bench adapter: harness registry file {path} must be a YAML mapping "
            f"with a `harnesses:` key; got {type(document).__name__}."
        )
    unknown = sorted(set(document) - {"harnesses"})
    if unknown:
        raise ValueError(
            f"terminal-bench adapter: harness registry file {path} declares unknown top-level "
            f"key(s) {unknown!r}; the only accepted key is `harnesses`."
        )
    entries = document.get("harnesses")
    if not isinstance(entries, dict) or not entries:
        raise ValueError(
            f"terminal-bench adapter: harness registry file {path} must declare a non-empty "
            "`harnesses:` mapping."
        )
    registry: dict[str, HarnessSpec] = {}
    for name, entry in entries.items():
        try:
            registry[name] = HarnessSpec.model_validate(entry)
        except ValidationError as exc:
            fields = sorted(
                ".".join(str(part) for part in err["loc"]) or "<entry>" for err in exc.errors()
            )
            raise ValueError(
                f"terminal-bench adapter: harness registry file {path}, harness {name!r}: "
                f"invalid field(s) {fields!r} — {exc}"
            ) from exc
    return registry


HARNESSES: dict[str, HarnessSpec] = load_harness_registry(SHIPPED_REGISTRY_FILE)
"""The shipped registry, loaded from :data:`SHIPPED_REGISTRY_FILE` at import."""


INSTALL_SCRIPT = Path(__file__).parent / "install-harness.sh"

OPENROUTER_PREFIX = "openrouter/"
"""Route marker litellm reads to select its OpenRouter handler.

A vendor CLI does not go through litellm — it reaches OpenRouter through the
``*_BASE_URL`` variables :data:`PROVIDER_ENV_KEYS` forwards. Left on the model
name, the CLI instead selects its own direct-vendor handler, reads the
deliberately blank vendor key, and fails with a 401. So a vendor harness gets
the prefix stripped, while the engine loop keeps it (litellm needs it).
"""

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
"""OpenRouter ``vendor/`` namespaces that :func:`harness_model` drops from the
model name for harnesses declaring ``strip_vendor_namespace=True``. A new
OpenRouter namespace would surface as the same "metadata not found" warning
the field's docstring cites."""

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
            f"terminal-bench adapter: provider env key(s) {rejected!r} are not "
            f"forwardable; accepted: {sorted(PROVIDER_ENV_KEYS)!r}."
        )


def accepted_harnesses(registry: Mapping[str, HarnessSpec] = HARNESSES) -> tuple[str, ...]:
    """Values ``agent_harness`` accepts: the engine loop plus every registry key.

    *registry* is the shipped one unless an operator overlay added or replaced
    entries, in which case the adapter passes its own.
    """
    return (ENGINE_LOOP, *registry)


def validate_harness(agent_harness: str, registry: Mapping[str, HarnessSpec] = HARNESSES) -> str:
    """Return *agent_harness* unchanged, or raise naming the accepted set."""
    accepted = accepted_harnesses(registry)
    if agent_harness not in accepted:
        raise ValueError(
            f"terminal-bench adapter: agent_harness {agent_harness!r} is not supported; "
            f"accepted: {list(accepted)!r}."
        )
    return agent_harness


def harness_model(
    model: str,
    agent_harness: str | None = None,
    registry: Mapping[str, HarnessSpec] = HARNESSES,
) -> str:
    """Model name as *agent_harness*'s CLI must receive it.

    Always strips the ``openrouter/`` route marker (see
    :data:`OPENROUTER_PREFIX`); a vendor CLI does not go through litellm and
    would otherwise select its own direct-vendor handler and fail with 401.

    When *agent_harness* names a harness whose spec declares
    :attr:`HarnessSpec.strip_vendor_namespace`, also strips a leading
    ``vendor/`` namespace so the model name is the CLI-catalog bare form
    (``gpt-5-mini`` from ``openrouter/openai/gpt-5-mini``).

    *agent_harness* defaults to ``None`` for callers that only need the
    ``openrouter/`` strip (or for the engine loop, which keeps everything).
    """
    if model.startswith(OPENROUTER_PREFIX):
        model = model[len(OPENROUTER_PREFIX) :]
    if agent_harness is not None:
        spec = registry.get(agent_harness)
        if spec is not None and spec.strip_vendor_namespace:
            for prefix in _VENDOR_NAMESPACE_PREFIXES:
                if model.startswith(prefix):
                    return model[len(prefix) :]
    return model


def harness_command(
    agent_harness: str,
    instruction: str,
    model: str,
    registry: Mapping[str, HarnessSpec] = HARNESSES,
) -> str:
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
    validate_harness(agent_harness, registry)
    spec = registry.get(agent_harness)
    if spec is None:
        raise ValueError(
            f"terminal-bench adapter: agent_harness {agent_harness!r} runs no CLI; "
            "the trial goes through the engine's LLM turn loop instead."
        )
    resolved_model = harness_model(model, agent_harness, registry)

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
