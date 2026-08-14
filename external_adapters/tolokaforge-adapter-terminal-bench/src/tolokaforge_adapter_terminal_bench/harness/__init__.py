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

import importlib.resources
import logging
import re
import shlex
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Literal

import yaml
from jinja2 import Environment, StrictUndefined, TemplateSyntaxError
from jinja2.meta import find_undeclared_variables
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from tolokaforge.core.plugin_registry import discover_entry_points

from .path_resolvers import DEFAULT_PATH_RESOLVER, LinuxRootResolver
from .protocols import PathResolver

__all__ = [
    "CONFIG_TEMPLATE_VARIABLES",
    "DEFAULT_PATH_RESOLVER",
    "ENGINE_LOOP",
    "HARNESSES",
    "HARNESS_REGISTRY_ENTRY_POINT_GROUP",
    "INSTALL_SCRIPT",
    "OPENROUTER_PREFIX",
    "PLUGIN_REGISTRY_RESOURCE",
    "PROVIDER_ENV_INPUT_PREFIX",
    "PROVIDER_ENV_KEYS",
    "SHIPPED_REGISTRY_FILE",
    "HarnessSpec",
    "LinuxRootResolver",
    "PathResolver",
    "accepted_harnesses",
    "discover_plugin_harness_registries",
    "harness_command",
    "harness_model",
    "load_harness_registry",
    "provider_env_input",
    "resolve_effective_registry",
    "validate_harness",
    "validate_provider_env_keys",
]

logger = logging.getLogger(__name__)

_URL_INSTALL_METHODS: frozenset[str] = frozenset({"curl-bash", "binary"})
"""Install methods whose ``install_source`` is downloaded rather than named."""

CONFIG_TEMPLATE_VARIABLES: frozenset[str] = frozenset(
    {"model", "provider", "base_url", "api_key_env"}
)
"""Every name a :attr:`HarnessSpec.config_files` template may reference.

``model`` is the slug as the CLI must receive it (see :func:`harness_model`),
``provider`` the routing prefix the run config's model named (``openrouter``),
``base_url`` the value of the provider envelope's ``*_BASE_URL`` entry, and
``api_key_env`` the *name* of its ``*_API_KEY`` entry — a template writes
``$``-prefixed to let the container's own environment supply the credential.
Deliberately closed: a template that could read arbitrary state would put that
state in the assembled command, which trial metadata records.
"""

_TEMPLATES = Environment(undefined=StrictUndefined)
"""Renderer for :attr:`HarnessSpec.config_files`. Strict-undefined so a typo
raises instead of writing a config file with a silently empty value."""


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

    model_flag_style: Literal["space", "equals"] = "space"
    """Whether :attr:`model_flag` and the model are two argv words
    (``--model gpt-5``) or one (``--model=gpt-5``). A CLI parsing its flags
    strictly accepts only one of the two."""

    config_files: dict[str, str] = Field(default_factory=dict)
    """Files the CLI reads its runtime configuration from, as
    ``{container path: Jinja template}``.

    For CLIs the compose ``environment:`` block alone cannot configure: codex
    reads ``openai_base_url`` from ``$CODEX_HOME/config.toml`` and drops
    ``$OPENAI_BASE_URL`` on the floor otherwise. Each template renders against
    :data:`CONFIG_TEMPLATE_VARIABLES` and nothing else — an unknown name is a
    load-time error, since a silently empty substitution would surface as a
    provider auth failure many layers from the typo.

    A path is an absolute one, a
    :data:`~.protocols.PATH_CONSTRUCT_PATTERN` construct over the vocabulary
    the run's :class:`~.protocols.PathResolver` knows (``${HOME}`` /
    ``${CONFIG_HOME}`` under the shipped
    :class:`~.path_resolvers.LinuxRootResolver`), or any other ``$``-rooted
    reference — which reaches the container verbatim, so a harness need not
    assume the container's user. Both path and content are written through a
    double-quoted ``printf``, so those references expand inside the container:
    that is how a credential reaches the file without the assembled command —
    which is recorded on ``TaskDescription.metadata`` — ever carrying its
    value. A template must therefore not carry a literal ``$`` it does not want
    expanded."""

    flags_pre_permission: tuple[str, ...] = ()
    """Flags inserted between the CLI executable and the model flag / argv
    suffix. The reference vendor-CLI invocation runs claude-code as
    ``claude --verbose --output-format=stream-json --permission-mode=… --print``;
    this field is where ``--verbose --output-format=stream-json`` land.
    Aligning the flag block aligns the CLI's internal reasoning mode, which
    drives how the agent reasons about the task and so what reward it earns."""

    instruction_channel: Literal["argv", "stdin"] = "argv"
    """How the task instruction reaches the CLI. ``"argv"`` — the trailing
    positional argument. ``"stdin"`` — piped in via ``printf "%s" '<instr>' |
    cli …`` so the shell never re-interprets any character in the prompt."""

    env_model_vars: tuple[str, ...] = ()
    """Env variable names that carry the model name into the CLI. Non-empty
    for CLIs whose sub-agents (``Task``, ``Explore``) resolve their model
    independently of the top-level ``--model`` flag: without the env quartet,
    ``Task`` sub-agents fall back to the CLI's default model — a different
    provider mid-trial — even when ``--model`` is set on the outer CLI. When
    non-empty, ``harness_command`` chains ``export VAR=<model>`` for each
    into the preamble AND drops the redundant ``--model`` CLI arg.
    The reference vendor-CLI invocation of Claude Code sets ``ANTHROPIC_MODEL``
    and its ``_DEFAULT_SONNET_MODEL`` / ``_OPUS_MODEL`` / ``_HAIKU_MODEL`` /
    ``CLAUDE_CODE_SUBAGENT_MODEL`` siblings — a five-var quartet."""

    container_env: dict[str, str] = Field(default_factory=dict)
    """Static env pairs the compose ``environment:`` block writes for the
    agent service. Zero-model, one-key-per-behaviour hardening — claude-code
    reads ``IS_SANDBOX=1`` (root-user override) and
    ``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1``. Values must be strings
    (compose interpolation is stringly typed) and must not overlap
    :data:`PROVIDER_ENV_KEYS` (a run-config would then shadow this)."""

    skills_dir_target: str | None = None
    """Absolute container directory a task pack's skills bundle is copied into
    during the harness image build, or ``None`` for a CLI that reads no skills.

    The parity policy refuses the operator's own ``~/.claude/skills``: what a
    benchmark agent can read has to be versioned with the task rather than with
    the laptop the eval ran on. A task declaring
    :attr:`~tolokaforge_adapter_terminal_bench.task_parser.TerminalBenchTask.harness_skills_dir`
    gets that directory copied here, and the bundle's content hash is recorded
    on the trial artifact. Left ``None``, the harness installs no skills and a
    pack shipping them still runs — without them."""

    strip_vendor_namespace: bool = False
    """Whether ``harness_model`` should strip a leading ``vendor/`` namespace
    from the model name before handing it to the CLI. Non-``False`` for CLIs
    whose model catalog uses bare names (``gpt-5-mini``, not
    ``openai/gpt-5-mini``) — a namespaced string prints "Model metadata for
    <name> not found" and the CLI silently drops OpenRouter routing to hit
    the vendor's default endpoint. The reference vendor-CLI invocation
    sidesteps the same trap by taking the last path segment."""

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
    def _config_templates_read_only_declared_variables(self) -> HarnessSpec:
        """Refuse a template a trial could not render, at load rather than run."""
        for path, template in self.config_files.items():
            if not path.startswith(("/", "$")):
                raise ValueError(
                    f"config_files path {path!r} is relative; the CLI reads it from a "
                    "fixed location, so give an absolute path or one rooted at a `$VAR`."
                )
            try:
                parsed = _TEMPLATES.parse(template)
            except TemplateSyntaxError as exc:
                raise ValueError(f"config_files[{path!r}] is not a valid template: {exc}") from exc
            unknown = sorted(find_undeclared_variables(parsed) - CONFIG_TEMPLATE_VARIABLES)
            if unknown:
                raise ValueError(
                    f"config_files[{path!r}] reads undeclared variable(s) {unknown!r}; "
                    f"available: {sorted(CONFIG_TEMPLATE_VARIABLES)!r}."
                )
        return self

    @model_validator(mode="after")
    def _skills_target_is_an_absolute_path(self) -> HarnessSpec:
        """Refuse a target the image build would resolve somewhere unintended."""
        if self.skills_dir_target is not None and not self.skills_dir_target.startswith("/"):
            raise ValueError(
                f"skills_dir_target {self.skills_dir_target!r} is relative; a Dockerfile "
                "`COPY` target resolves against the image's WORKDIR, so give the absolute "
                "path the CLI reads skills from. `~` is not expanded either."
            )
        return self

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
``harness_presets_file`` param at a second file of the same shape, or installs
a bundle that registers itself in
:data:`HARNESS_REGISTRY_ENTRY_POINT_GROUP`; :func:`resolve_effective_registry`
composes the three.
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


HARNESS_REGISTRY_ENTRY_POINT_GROUP = "tolokaforge_adapter_terminal_bench.harness_registries"
"""Entry-point group a pip-installable harness bundle registers itself in.

Each entry point names the plugin's Python package; the package ships its
registry as a :data:`PLUGIN_REGISTRY_RESOURCE` resource beside its
``__init__.py``::

    [project.entry-points."tolokaforge_adapter_terminal_bench.harness_registries"]
    my_org = "my_org.tolokaforge_harnesses"

Adapter-namespaced rather than ``tolokaforge.*``: the harness registry is the
terminal-bench adapter's surface, and the engine core never learns these names.
"""

PLUGIN_REGISTRY_RESOURCE = "harnesses.yaml"
"""File name a plugin package ships its registry under.

Convention rather than a second declaration: the plugin's whole contract is the
``pyproject.toml`` entry point plus a file named like the shipped registry it
extends, so there is no module attribute that could disagree with the file on
disk. Same shape as :data:`SHIPPED_REGISTRY_FILE`'s ``harnesses:`` mapping.
"""


def discover_plugin_harness_registries() -> dict[str, HarnessSpec]:
    """Registry union of every installed :data:`HARNESS_REGISTRY_ENTRY_POINT_GROUP` plugin.

    Each entry point is loaded to its package, whose
    :data:`PLUGIN_REGISTRY_RESOURCE` is read through
    :func:`load_harness_registry` — so a plugin's typo is refused with the same
    message an operator overlay's would be, naming the file and the harness key.

    Returns an empty mapping when nothing is installed, which is the common
    case and the one that must stay free of surprises: no plugin, no change.

    Raises:
        ValueError: two installed plugins declare the same harness name. There
            is no safe pick — the two bundles disagree about what that name
            installs and how it is invoked — so the ambiguity is refused naming
            both distributions rather than resolved by install order.
    """
    registry: dict[str, HarnessSpec] = {}
    provenance: dict[str, str] = {}
    installed = discover_entry_points(HARNESS_REGISTRY_ENTRY_POINT_GROUP)
    for name, entry_point in sorted(installed.items()):
        distribution = entry_point.dist.name if entry_point.dist is not None else name
        resource = importlib.resources.files(entry_point.load()) / PLUGIN_REGISTRY_RESOURCE
        with importlib.resources.as_file(resource) as path:
            bundle = load_harness_registry(path)
        for harness_name in sorted(bundle):
            owner = provenance.get(harness_name)
            if owner is not None:
                raise ValueError(
                    f"terminal-bench adapter: harness {harness_name!r} is declared by two "
                    f"installed registry plugins, {owner!r} and {distribution!r}. Uninstall "
                    "one, or rename the harness in one of the bundles."
                )
            provenance[harness_name] = distribution
        registry.update(bundle)
        logger.info(
            "terminal-bench adapter: harness registry plugin %s contributed %s",
            distribution,
            sorted(bundle),
        )
    return registry


def resolve_effective_registry(
    presets_file: str | None = None, *, discover_plugins: bool = True
) -> dict[str, HarnessSpec]:
    """The registry one adapter runs on, composed from all three sources.

    Precedence, lowest to highest, whole-entry replacement at each transition::

        shipped SHIPPED_REGISTRY_FILE
          ← discover_plugin_harness_registries()   (logs a warning when it
                                                    shadows a shipped entry)
          ← the *presets_file* operator overlay    (shadows silently: naming
                                                    the file is the intent)

    Replacement is whole-entry, never field-wise, at every layer: a merge would
    let a bundle silently inherit a default it never meant to keep — a pinned
    CLI version, a mandatory permission flag — and produce an invocation no
    layer declared.

    Args:
        presets_file: Path to an overlay registry YAML, absolute or relative to
            the working directory. ``None`` adds no overlay layer.
        discover_plugins: Whether installed plugins contribute. ``False`` pins
            the effective registry to what this adapter ships plus the named
            overlay, for runs that must reproduce independently of what is
            installed alongside them.

    Raises:
        ValueError: *presets_file* names a file that does not exist, is not
            valid YAML, or declares an entry :class:`HarnessSpec` rejects; or
            two installed plugins collide.
    """
    registry = dict(HARNESSES)
    if discover_plugins:
        plugins = discover_plugin_harness_registries()
        shadowed = sorted(set(plugins) & set(HARNESSES))
        if shadowed:
            logger.warning(
                "terminal-bench adapter: installed registry plugin(s) replace shipped "
                "harness spec(s) %s; the shipped install source, pinned version and argv "
                "for those names are not what runs.",
                shadowed,
            )
        registry.update(plugins)
    if presets_file:
        registry.update(load_harness_registry(Path(presets_file).expanduser().resolve()))
    return registry


INSTALL_SCRIPT = Path(__file__).parent / "install-harness.sh"

OPENROUTER_PREFIX = "openrouter/"
"""Route marker litellm reads to select its OpenRouter handler.

A vendor CLI does not go through litellm — it reaches OpenRouter through the
``*_BASE_URL`` variables :data:`PROVIDER_ENV_KEYS` forwards. Left on the model
name, the CLI instead selects its own direct-vendor handler, reads the
deliberately blank vendor key, and fails with a 401. So a vendor harness gets
the prefix stripped, while the engine loop keeps it (litellm needs it).
"""

SHIPPED_REGISTRY_META_FILE: Path = (
    Path(__file__).resolve().parent.parent / "data" / "registry_meta.yaml"
)
"""Registry-wide catalog file: OpenRouter vendor namespaces + the closed
allow-list of provider env-var names. Ships as data so a new namespace or
env-var name is a YAML edit rather than a Python constant edit."""


class _RegistryMeta(BaseModel):
    """Loader-shape for ``data/registry_meta.yaml``.

    Kept private: no caller outside this module needs the model itself,
    only :data:`_VENDOR_NAMESPACE_PREFIXES` and :data:`PROVIDER_ENV_KEYS`
    populated from it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    openrouter_vendor_namespaces: tuple[str, ...]
    provider_env_keys: frozenset[str]

    @model_validator(mode="after")
    def _every_entry_is_a_non_blank_string(self) -> _RegistryMeta:
        for value in self.openrouter_vendor_namespaces:
            if not value or value.strip() != value:
                raise ValueError(
                    f"registry_meta.yaml: openrouter_vendor_namespaces entry "
                    f"{value!r} must be a non-blank string without leading/trailing whitespace."
                )
        for value in self.provider_env_keys:
            if not value or value.strip() != value:
                raise ValueError(
                    f"registry_meta.yaml: provider_env_keys entry {value!r} "
                    "must be a non-blank string without leading/trailing whitespace."
                )
        if len(self.openrouter_vendor_namespaces) != len(set(self.openrouter_vendor_namespaces)):
            raise ValueError(
                "registry_meta.yaml: openrouter_vendor_namespaces contains duplicates."
            )
        if not self.openrouter_vendor_namespaces:
            raise ValueError("registry_meta.yaml: openrouter_vendor_namespaces must not be empty.")
        if not self.provider_env_keys:
            raise ValueError("registry_meta.yaml: provider_env_keys must not be empty.")
        return self


def _load_registry_meta(path: Path) -> _RegistryMeta:
    """Read the registry-meta YAML at *path*, fail loud on missing / malformed input."""
    if not path.exists():
        raise FileNotFoundError(
            f"registry_meta.yaml not found at {path}; the shipped file ships with "
            "the tbench adapter wheel and its absence is a packaging error."
        )
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, Mapping):
        raise ValueError(
            f"registry_meta.yaml at {path} must be a YAML mapping; got {type(data).__name__}."
        )
    try:
        return _RegistryMeta.model_validate(dict(data))
    except ValidationError as exc:
        raise ValueError(f"registry_meta.yaml at {path}: {exc}") from exc


_REGISTRY_META = _load_registry_meta(SHIPPED_REGISTRY_META_FILE)

_VENDOR_NAMESPACE_PREFIXES: tuple[str, ...] = _REGISTRY_META.openrouter_vendor_namespaces
"""OpenRouter ``vendor/`` namespaces that :func:`harness_model` drops from the
model name for harnesses declaring ``strip_vendor_namespace=True``. Loaded from
``data/registry_meta.yaml`` at import; adding a namespace is a YAML edit."""

PROVIDER_ENV_KEYS: frozenset[str] = _REGISTRY_META.provider_env_keys
"""Environment variables a harness CLI may be given inside the task container.

An allow-list, not an open surface: every forwarded value lands in the
per-trial compose ``.env`` and then in the container the agent works in, so an
open surface would let a run config shadow the task's own environment with
arbitrary values. Loaded from ``data/registry_meta.yaml`` at import; adding a
key is a YAML edit."""

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


def _shell_string(value: str) -> str:
    """*value* as a double-quoted shell word, with ``$`` left expandable."""
    return '"' + re.sub(r'([\\"`])', r"\\\1", value) + '"'


def _config_file_write(path: str, content: str) -> str:
    """Command writing *content* to *path*, creating the parent directory.

    One ``printf`` argument per line, so the assembled command stays on a
    single line whatever the file holds.
    """
    lines = " ".join(_shell_string(line) for line in content.rstrip("\n").split("\n"))
    target = _shell_string(path)
    return f"mkdir -p \"$(dirname {target})\" && printf '%s\\n' {lines} > {target}"


def _config_template_variables(
    resolved_model: str, model: str, provider_env: Mapping[str, str]
) -> dict[str, str]:
    """The :data:`CONFIG_TEMPLATE_VARIABLES` values for one trial.

    Raises:
        ValueError: *provider_env* carries more than one ``*_BASE_URL`` or
            ``*_API_KEY`` entry, leaving no single answer for a template that
            asks for "the" endpoint or key.
    """
    base_urls = sorted(key for key in provider_env if key.endswith("_BASE_URL"))
    api_keys = sorted(key for key in provider_env if key.endswith("_API_KEY"))
    ambiguous = sorted(key for keys in (base_urls, api_keys) if len(keys) > 1 for key in keys)
    if ambiguous:
        raise ValueError(
            "terminal-bench adapter: the provider envelope carries several entries a "
            f"config_files template would have to choose between ({ambiguous!r}); declare "
            "one endpoint and one key per harness."
        )
    return {
        "model": resolved_model,
        "provider": model.partition("/")[0] if "/" in model else "",
        "base_url": provider_env[base_urls[0]] if base_urls else "",
        "api_key_env": api_keys[0] if api_keys else "",
    }


def harness_command(
    agent_harness: str,
    instruction: str,
    model: str,
    registry: Mapping[str, HarnessSpec] = HARNESSES,
    provider_env: Mapping[str, str] | None = None,
    *,
    path_resolver: PathResolver | None = None,
) -> str:
    """Shell command that runs *agent_harness* against *instruction*.

    *provider_env* is the envelope the trial's container will carry, which the
    adapter resolves from the harness default and the run config; it supplies
    the ``base_url`` / ``api_key_env`` template variables. It defaults to the
    harness's own :attr:`HarnessSpec.provider_env`, and only its ``*_BASE_URL``
    value is ever read — a credential reaches a config file by name, through
    the container's environment, never through this command.

    *path_resolver* answers where each :attr:`HarnessSpec.config_files` key
    lands in the runtime this command will run in, defaulting to
    :data:`~.path_resolvers.DEFAULT_PATH_RESOLVER`. File *contents* never go
    through it: their template vocabulary is closed and already
    runtime-neutral.

    Assembly order (blank pieces drop out):

        <config_files write> &&    # one per HarnessSpec.config_files entry
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
    if spec.config_files:
        variables = _config_template_variables(
            resolved_model, model, spec.provider_env if provider_env is None else provider_env
        )
        resolver = DEFAULT_PATH_RESOLVER if path_resolver is None else path_resolver
        preamble_parts.extend(
            _config_file_write(
                resolver.resolve(path), _TEMPLATES.from_string(template).render(variables)
            )
            for path, template in spec.config_files.items()
        )
    for var in spec.env_model_vars:
        preamble_parts.append(f"export {var}={shlex.quote(resolved_model)}")

    # CLI argv (without the instruction when it's coming on stdin).
    cli_tokens: list[str] = [*spec.argv_prefix, *spec.flags_pre_permission]
    if not spec.env_model_vars and spec.model_flag:
        if spec.model_flag_style == "equals":
            cli_tokens.append(f"{spec.model_flag}={resolved_model}")
        else:
            cli_tokens.extend([spec.model_flag, resolved_model])
    cli_tokens.extend(spec.argv_suffix)
    if spec.instruction_channel == "argv":
        cli_tokens.append(instruction)
    cli_command = " ".join(shlex.quote(part) for part in cli_tokens)

    if spec.instruction_channel == "stdin":
        cli_command = f"printf %s {shlex.quote(instruction)} | {cli_command}"

    return " && ".join([*preamble_parts, cli_command])
