"""Spec, registry composition and command assembly for one harness CLI.

:class:`HarnessSpec` declares a CLI, :func:`load_harness_registry` and
:func:`resolve_effective_registry` compose the shipped data with an operator
overlay and installed plug-in bundles, and :func:`harness_command` turns the
result into the shell command a trial runs. Callers import from the package
root; the names here are re-exported there.
"""

from __future__ import annotations

import importlib.metadata
import importlib.resources
import json
import logging
import re
import shlex
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from jinja2 import Environment, StrictUndefined, TemplateSyntaxError
from jinja2.meta import find_undeclared_variables
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .path_resolvers import DEFAULT_PATH_RESOLVER
from .protocols import PATH_CONSTRUCT_PATTERN, PathResolver

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

Named for what actually runs. The trial goes through the calling runtime's own
tool-calling loop with the run config's model, its own system prompt, and the
adapter's ``bash`` tool — a different scaffold from terminal-bench's Terminus-2
agent, which this repo does not install. A trial recorded as ``terminus-2``
would be claiming a comparison it did not run.
"""


class RequestMiddleware(BaseModel):
    """Per-harness HTTP proxy that mutates outbound provider requests.

    Ships with :mod:`~.middleware_proxy` — a stdlib-only HTTP forwarder that
    lands in the image alongside ``install-harness.sh`` and starts
    on-demand in the harness_command preamble. Configured here as data:
    which env-var value gets redirected, what port to bind, what body /
    header fields to inject.

    Motivating case: ``moonshotai/kimi-k2.7-code`` on OpenRouter fans out to
    14 possible providers, mostly INT4/FP4 third-parties whose tool-call
    continuation returns empty completions. Forcing Moonshot AI first-party
    routing via ``{"provider": {"only": ["moonshotai"]}}`` fixes it, but the
    kimi-code CLI (through 0.36.1) has no user-facing body-passthrough. The
    same shape covers any vendor / model whose OpenRouter routing needs
    pinning, any custom-header injection a CLI does not surface, and any
    on-the-wire body repair a downstream provider needs. Every future user
    of this slot is a HarnessSpec YAML edit — no code change.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    upstream_env_key: str
    """Which :attr:`HarnessSpec.provider_env` key's value is the URL to be
    proxied. Non-empty. That variable's value in the container is rewritten
    to ``http://127.0.0.1:<port>`` for the CLI process, while the real URL
    reaches the middleware as its ``--upstream``. Naming the env key rather
    than the URL itself keeps the routing (LiteLLM overlay, direct-provider
    override, alternate gateway) as an operator overlay concern — the
    middleware wraps whatever the run's ``provider_env`` finally resolves
    to."""

    port: int = 8899
    """Local port :mod:`middleware_proxy` listens on inside the container.
    Fixed rather than dynamic so a caller inspecting the CLI's traffic can
    predict where it goes. Distinct across concurrent harnesses on the same
    host is not required — each trial runs in its own container namespace."""

    body_injections: dict[str, Any] = Field(default_factory=dict)
    """JSON object deep-merged into every request body (only on
    :attr:`path_filter`). Overlay values win on key conflict; nested dicts
    merge recursively; non-dict overlay values replace. Empty on default —
    the middleware becomes a passthrough forwarder, useful when only headers
    are being injected."""

    header_injections: dict[str, str] = Field(default_factory=dict)
    """Extra HTTP headers added to every forwarded request. Values are string
    literals — no template expansion, since the middleware runs inside the
    container after :attr:`HarnessSpec.provider_env` interpolation."""

    path_filter: str | None = None
    """Only inject on request paths starting with this prefix. ``None`` (the
    default) injects on every ``POST`` with a JSON body; set to
    ``"/chat/completions"`` when the provider serves both chat and
    non-chat endpoints and only chat needs a body override."""


class RuntimeGateway(BaseModel):
    """A gateway a harness can be routed through instead of its own provider.

    Data only — nothing in this package reads a gateway's fields. The catalog
    exists so a :class:`GatewayRoute` names one endpoint by key instead of
    repeating a URL, and so the runtime that *does* resolve the pair reads the
    same names the harness data shipped with. ADR-0037 is the contract.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url_env: str
    """Name of the variable holding the gateway's base URL — a name, never the
    URL. Where an operator's gateway lives is a deployment fact, so it reaches
    a run through the same secret / env seam a credential does and never enters
    shipped registry data."""

    credential_env: str
    """Name of the variable holding the gateway's credential."""

    supports: tuple[str, ...] = ()
    """Capability tags a consuming runtime can match a harness's needs against
    (``protocol_translation``, ``provider_pinning``). Free-form: this package
    defines no vocabulary and reads no tag."""

    @model_validator(mode="after")
    def _every_name_is_non_blank_and_no_tag_repeats(self) -> RuntimeGateway:
        for field, value in (
            ("base_url_env", self.base_url_env),
            ("credential_env", self.credential_env),
        ):
            if not value or value.strip() != value:
                raise ValueError(
                    f"RuntimeGateway.{field} {value!r} must be a non-blank variable name "
                    "without leading/trailing whitespace."
                )
        for tag in self.supports:
            if not tag or tag.strip() != tag:
                raise ValueError(
                    f"RuntimeGateway.supports entry {tag!r} must be a non-blank string "
                    "without leading/trailing whitespace."
                )
        if len(self.supports) != len(set(self.supports)):
            raise ValueError(
                f"RuntimeGateway.supports {list(self.supports)!r} contains duplicates."
            )
        return self


class GatewayRoute(BaseModel):
    """How ONE harness reaches ONE named gateway.

    Carries the same three shapes the default path uses — ``config_files``,
    ``container_env``, ``provider_env`` — so a runtime already provisioning a
    harness reuses its plumbing rather than growing a second one. It must not
    reuse the default path's *expansion order*: these values carry
    ``${gateway.*}`` and ``${secret:NAME}`` tokens this package never expands,
    and the adapter's provider-env resolution refuses any resolved value
    containing a ``$`` — which a value still carrying ``${gateway.base_url}``
    necessarily does. ADR-0037's token table is the ordering contract, and the
    consuming runtime is what honours it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    gateway: str
    """Key into :data:`ALTERNATIVE_GATEWAYS`, refused at load if undeclared.

    The catalog is shipped-only: ``registry_meta.yaml`` has no overlay and no
    plug-in layer, so an operator cannot register a gateway name — declaring
    one is a PR against that file. Their escape hatch is a layer down and
    already exists: an operator running their own gateway overlays the whole
    harness entry (``config_files`` / ``container_env`` / ``provider_env``
    directly, as ``examples/terminal_bench/gemini_litellm_overlay.yaml`` does)
    and names no gateway at all. The closed set buys a load-time typo check for
    the cross-repo runtime, which is the one consumer that can check nothing
    itself. The first operator needing a route against a self-hosted gateway is
    the trigger to make the catalog layerable — its own decision, recorded in
    ADR-0037 § Consequences."""

    passthrough_path: str = ""
    """Path segment the consuming runtime appends to the gateway's base URL to
    reach this CLI's wire protocol (``/gemini`` for LiteLLM's Gemini
    passthrough). Empty when the gateway serves the CLI's protocol at its
    root."""

    model_alias_pattern: str | None = None
    """Pattern the gateway knows this run's model under, with ``{model}``
    standing for the resolved model name. Opaque here — the consuming runtime
    renders it and delivers the result through the harness's own
    :attr:`HarnessSpec.env_model_vars`, the field that already answers how a
    CLI receives its model name."""

    config_files: dict[str, str] = Field(default_factory=dict)
    """``{container path: literal content}`` the gateway route needs on disk.

    Inverted from :attr:`HarnessSpec.config_files`, which holds Jinja
    templates: these values are literals, shipped into a container verbatim by
    a runtime that renders nothing, so a ``{{ … }}`` here is refused at load
    rather than delivered unexpanded. Keys follow the same rule as the default
    path's — absolute, or rooted at a construct the run's
    :class:`~.protocols.PathResolver` answers — and the consuming runtime
    resolves them before writing, since nothing downstream of it expands a
    leftover ``${HOME}``."""

    container_env: dict[str, str] = Field(default_factory=dict)
    """Static literals the gateway route needs in the CLI's environment. Same
    two narrowings :attr:`HarnessSpec.container_env` carries: no key on the
    :data:`PROVIDER_ENV_KEYS` allow-list, and no value containing a ``$``."""

    provider_env: dict[str, str] = Field(default_factory=dict)
    """Provider envelope for the gateway route, replacing the harness's default
    :attr:`HarnessSpec.provider_env` when a run takes this path. Values carry
    the ``${gateway.*}`` and ``${secret:NAME}`` tokens ADR-0037's table orders;
    keys are a subset of :data:`PROVIDER_ENV_KEYS`, checked here because no
    in-repo consumer will ever check them."""

    @model_validator(mode="after")
    def _provider_env_keys_are_forwardable(self) -> GatewayRoute:
        """Refuse a key no harness CLI may be given — at load or never.

        The default path is checked by the adapter as it resolves the effective
        envelope. Nothing in this package resolves a gateway route, so this is
        the only moment the check can happen.
        """
        try:
            validate_provider_env_keys(self.provider_env)
        except ValueError as exc:
            raise ValueError(f"gateway_route.provider_env: {exc}") from exc
        return self

    @model_validator(mode="after")
    def _container_env_carries_literals_that_shadow_nothing(self) -> GatewayRoute:
        """Refuse the two shapes :attr:`HarnessSpec.container_env` refuses."""
        overlapping = sorted(set(self.container_env) & PROVIDER_ENV_KEYS)
        if overlapping:
            raise ValueError(
                f"gateway_route.container_env key(s) {overlapping!r} are provider env keys; "
                "declare them under gateway_route.provider_env, the seam whose `${gateway.…}` "
                "and `${secret:NAME}` values the consuming runtime resolves. container_env "
                "carries literals written verbatim, so the same key here would reach the CLI as "
                "an unresolved endpoint or credential."
            )
        interpolated = sorted(key for key, value in self.container_env.items() if "$" in value)
        if interpolated:
            raise ValueError(
                f"gateway_route.container_env value(s) for {interpolated!r} contain a `$`; this "
                "map carries literals only, and no `${gateway.…}` or `${secret:NAME}` token is "
                "expanded on it. A value that must carry a credential or the gateway's own "
                "endpoint belongs in gateway_route.provider_env."
            )
        return self

    @model_validator(mode="after")
    def _config_files_are_literal_content_at_fixed_paths(self) -> GatewayRoute:
        """Refuse a relative path, and a template nothing on this path renders."""
        for path, content in self.config_files.items():
            if not path.startswith(("/", "$")):
                raise ValueError(
                    f"gateway_route.config_files path {path!r} is relative; the CLI reads it "
                    "from a fixed location, so give an absolute path or one rooted at a `$VAR`."
                )
            marker = next((m for m in ("{{", "{%") if m in content), None)
            if marker is not None:
                raise ValueError(
                    f"gateway_route.config_files[{path!r}] contains {marker!r}, but this map "
                    "holds literal file content shipped into a container verbatim — no renderer "
                    "in this package ever sees it, so the CLI would read the template "
                    "unexpanded. HarnessSpec.config_files is the templated map; a token dialect "
                    "on this path belongs to the runtime that provisions it."
                )
        return self

    @model_validator(mode="after")
    def _passthrough_path_is_rooted(self) -> GatewayRoute:
        """Refuse a segment that would concatenate onto the base URL wrong."""
        if self.passthrough_path and not self.passthrough_path.startswith("/"):
            raise ValueError(
                f"gateway_route.passthrough_path {self.passthrough_path!r} must be empty or "
                "start with `/`; the consuming runtime concatenates it onto the gateway's base "
                "URL, where a missing separator silently produces a different path."
            )
        return self


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
    """Static literals the compose ``environment:`` block writes for the agent
    service. Zero-model, one-key-per-behaviour hardening — claude-code reads
    ``IS_SANDBOX=1`` (root-user override) and
    ``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1``.

    Two narrowings are refused at registry-load time. A key in
    :data:`PROVIDER_ENV_KEYS`: this map is written into the agent service's
    ``environment:`` after the provider envelope, so a colliding key silently
    overwrites the ``${TBENCH_PROVIDER_*}`` indirection the per-trial ``.env``
    answers. A value containing a ``$``: docker interpolates that block, so
    ``${secret:NAME}`` is refused as ``invalid interpolation format`` and a bare
    ``$FOO`` resolves against the invoking shell.

    Env that must carry a secret or an operator-supplied endpoint goes in
    :attr:`provider_env`, which expands ``${secret:NAME}`` and keeps the value
    out of the compose file entirely."""

    skills_dir_target: str | None = None
    """Runtime directory a task pack's skills bundle is delivered to, or
    ``None`` for a CLI that reads no skills.

    Either an absolute path or one rooted at a ``${HOME}`` /
    ``${CONFIG_HOME}`` construct the run's
    :class:`~tolokaforge_coding_harnesses.protocols.PathResolver` answers before
    :class:`~tolokaforge_coding_harnesses.protocols.SkillDelivery` sees it.
    Unlike a :attr:`config_files` key it may *not* be rooted at a
    brace-less ``$VAR``: no shell reads this path, so nothing would expand it
    the way the resolver does.

    The parity policy refuses the operator's own ``~/.claude/skills``: what a
    benchmark agent can read has to be versioned with the task rather than with
    the laptop the eval ran on. A task pack declaring a skills directory gets it
    delivered here, and the bundle's content hash is recorded on the trial
    artifact. Left ``None``, the harness installs no skills and a pack shipping
    them still runs — without them."""

    strip_vendor_namespace: bool = False
    """Whether ``harness_model`` should strip a leading ``vendor/`` namespace
    from the model name before handing it to the CLI. Non-``False`` for CLIs
    whose model catalog uses bare names (``gpt-5-mini``, not
    ``openai/gpt-5-mini``) — a namespaced string prints "Model metadata for
    <name> not found" and the CLI silently drops OpenRouter routing to hit
    the vendor's default endpoint. The reference vendor-CLI invocation
    sidesteps the same trap by taking the last path segment."""

    request_middleware: RequestMiddleware | None = None
    """When set, ships a stdlib HTTP proxy inside the trial container and
    routes the CLI's provider calls through it, injecting the declared body /
    header fields. See :class:`RequestMiddleware` — the docstring names the
    motivating case (OpenRouter provider-preference pinning for
    ``moonshotai/kimi-k2.7-code``) and the future shape."""

    strip_openrouter_prefix: bool = True
    """Whether ``harness_model`` should strip a leading ``openrouter/`` route
    marker before handing the model name to the CLI. ``True`` for CLIs whose
    provider registry uses the ``openrouter/`` prefix to select a direct-vendor
    handler (claude-code, codex, grok-build, kimi-code, gemini-cli) — the
    prefix would land on ``api.openrouter.ai/openrouter/...`` and 404. ``False``
    for CLIs whose config template defines a provider *literally* named
    ``openrouter`` and expects the caller to route ``openrouter/<vendor>/<model>``
    to it (opencode). Stripping the prefix for those CLIs re-routes
    ``openrouter/meta/muse-glimmer-30b`` to a nonexistent ``meta`` provider
    and crashes with ``UnknownError`` in ~2s."""

    provider_env: dict[str, str] = Field(default_factory=dict)
    """Default provider-env envelope for this harness — the shape the CLI
    needs to reach its provider through OpenRouter (or wherever). Populated
    once per harness (``ANTHROPIC_API_KEY`` + ``ANTHROPIC_BASE_URL`` for
    claude-code, ``OPENAI_*`` for codex, ``GOOGLE_API_KEY`` for gemini-cli).
    Values may be literal (URLs pointing at OpenRouter) or ``${secret:NAME}``
    refs the calling runtime's secret manager resolves.

    The adapter's ``agent_provider_env`` run-config param overlays this
    map key-by-key (union with run-config keys winning on conflict), so a
    caller declaring nothing gets the shipped defaults, a caller
    declaring a different endpoint gets that, and one caller can add a
    key the harness didn't ship (e.g. ``ANTHROPIC_AUTH_TOKEN``) without
    losing the URL. Keys must be a subset of :data:`PROVIDER_ENV_KEYS`."""

    gateway_route: GatewayRoute | None = None
    """How this harness reaches a gateway named in :data:`ALTERNATIVE_GATEWAYS`,
    or ``None`` for a harness carrying no such recipe.

    Inert to :func:`harness_command`: the assembled command is byte-identical
    with and without it, and no caller in this package reads it. It is data for
    a second runtime, which provisions an already-running trial container from
    the same spec that drives compose synthesis here. See :class:`GatewayRoute`
    and ADR-0037."""

    @model_validator(mode="after")
    def _gateway_route_names_a_declared_gateway(self) -> HarnessSpec:
        """Refuse an undeclared gateway — at load, or nowhere.

        No in-repo caller reads :attr:`gateway_route`, so a typo would
        otherwise first surface as a cross-repo runtime's 404 against a URL
        nothing ever resolved.
        """
        if self.gateway_route is None or self.gateway_route.gateway in ALTERNATIVE_GATEWAYS:
            return self
        raise ValueError(
            f"gateway_route.gateway {self.gateway_route.gateway!r} is not a declared gateway; "
            f"accepted: {sorted(ALTERNATIVE_GATEWAYS)!r}. The catalog is shipped-only — "
            "registry_meta.yaml carries no overlay and no plug-in layer — so declaring a gateway "
            "is a PR against that file. An operator routing one harness through their own gateway "
            "overlays the harness entry's config_files / container_env / provider_env directly "
            "instead, and names no gateway at all."
        )

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
    def _skills_target_is_resolvable(self) -> HarnessSpec:
        """Refuse a target no resolver and no build step would place."""
        target = self.skills_dir_target
        if target is None or target.startswith("/") or PATH_CONSTRUCT_PATTERN.match(target):
            return self
        raise ValueError(
            f"skills_dir_target {target!r} is neither absolute nor rooted at a "
            "`${VAR}` construct the run's PathResolver answers. A Dockerfile `COPY` "
            "target resolves against the image's WORKDIR, and Docker expands neither "
            "`~` nor a brace-less `$VAR` the way a shell would — `$HOME/.claude/skills/` "
            "would be read off the image's own `ENV`, which is nobody's answer. Give the "
            "absolute path the CLI reads skills from, or `${HOME}/...`."
        )

    @model_validator(mode="after")
    def _config_files_and_request_middleware_do_not_coexist(self) -> HarnessSpec:
        """Refuse a spec that would silently bake the upstream URL into a config file.

        :attr:`config_files` templates render at Python assembly time from
        :attr:`provider_env` — the ``base_url`` variable interpolates the
        pre-rewrite ``*_BASE_URL`` value. :attr:`request_middleware`
        rewrites that env var at bash time, AFTER the config files have
        already been written. A CLI that reads its endpoint from an on-disk
        config file bakes in the upstream URL and bypasses the proxy —
        silently, since the CLI never touches the env var again after
        startup. Reject the combination at load rather than at trial time
        with a broken run to diagnose.
        """
        if self.request_middleware is not None and self.config_files:
            raise ValueError(
                "HarnessSpec: request_middleware and config_files cannot both be "
                "set. config_files render at assembly time with the upstream URL "
                "from provider_env; the middleware rewrite only reaches env-driven "
                "routing. A CLI that reads its endpoint from a config file would "
                "bake in the upstream and bypass the proxy. Route the CLI's "
                "endpoint through an env var, or land the config template "
                "referencing http://127.0.0.1:<port> directly."
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

    @model_validator(mode="after")
    def _container_env_does_not_shadow_the_provider_envelope(self) -> HarnessSpec:
        """Refuse a key whose literal would replace the provider indirection.

        The compose writer emits the provider envelope first and
        :attr:`container_env` second, and the later write wins — so a key on
        both sides reaches the container as this literal, and the
        ``${TBENCH_PROVIDER_*}`` input the per-trial ``.env`` answers is gone
        with nothing to read back that it ever existed.
        """
        overlapping = sorted(set(self.container_env) & PROVIDER_ENV_KEYS)
        if not overlapping:
            return self
        raise ValueError(
            f"container_env key(s) {overlapping!r} are provider env keys; declare them "
            "under provider_env, the seam that expands `${secret:NAME}` and supplies the "
            "value through the per-trial `.env`. container_env is written into the compose "
            "file verbatim, and last, so it would silently overwrite that indirection."
        )

    @model_validator(mode="after")
    def _container_env_values_are_compose_literals(self) -> HarnessSpec:
        """Refuse a value docker would interpolate rather than pass through."""
        interpolated = sorted(key for key, value in self.container_env.items() if "$" in value)
        if not interpolated:
            return self
        raise ValueError(
            f"container_env value(s) for {interpolated!r} contain a `$`; docker interpolates "
            "the compose `environment:` block, where `${secret:NAME}` is refused outright "
            "(`invalid interpolation format`) and a bare `$FOO` is replaced by whatever the "
            "invoking shell holds. container_env carries literals only — a value that must "
            "carry a secret or an operator-supplied endpoint belongs in provider_env."
        )


SHIPPED_REGISTRY_FILE = Path(__file__).resolve().parent / "data" / "harnesses.yaml"
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
        raise ValueError(f"coding harness: harness registry file {path} does not exist.")
    try:
        document = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ValueError(
            f"coding harness: harness registry file {path} is not valid YAML: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise ValueError(
            f"coding harness: harness registry file {path} must be a YAML mapping "
            f"with a `harnesses:` key; got {type(document).__name__}."
        )
    unknown = sorted(set(document) - {"harnesses"})
    if unknown:
        raise ValueError(
            f"coding harness: harness registry file {path} declares unknown top-level "
            f"key(s) {unknown!r}; the only accepted key is `harnesses`."
        )
    entries = document.get("harnesses")
    if not isinstance(entries, dict) or not entries:
        raise ValueError(
            f"coding harness: harness registry file {path} must declare a non-empty "
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
                f"coding harness: harness registry file {path}, harness {name!r}: "
                f"invalid field(s) {fields!r} — {exc}"
            ) from exc
    return registry


HARNESS_REGISTRY_ENTRY_POINT_GROUP = "tolokaforge_adapter_terminal_bench.harness_registries"
"""Entry-point group a pip-installable harness bundle registers itself in.

Each entry point names the plugin's Python package; the package ships its
registry as a :data:`PLUGIN_REGISTRY_RESOURCE` resource beside its
``__init__.py``::

    [project.entry-points."tolokaforge_adapter_terminal_bench.harness_registries"]
    my_org = "my_org.tolokaforge_harnesses"

Not ``tolokaforge.*``: the engine core never learns these names, so that group
would imply it consumes the registry. The adapter-shaped string is the published
name every installed bundle registers under; renaming it needs its own
migration (ADR-0036).
"""

PLUGIN_REGISTRY_RESOURCE = "harnesses.yaml"
"""File name a plugin package ships its registry under.

Convention rather than a second declaration: the plugin's whole contract is the
``pyproject.toml`` entry point plus a file named like the shipped registry it
extends, so there is no module attribute that could disagree with the file on
disk. Same shape as :data:`SHIPPED_REGISTRY_FILE`'s ``harnesses:`` mapping.
"""


class PluginBundle(BaseModel):
    """One installed registry plugin and the harness names it declared.

    Pydantic rather than a dataclass: this value is written verbatim into the
    run bundle, so it crosses a serialisation boundary.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    distribution: str
    """Installing distribution's name, or the entry-point name when the entry
    point carries no distribution."""

    version: str | None
    """The distribution's version. ``None`` for a programmatically registered
    entry point, which has no distribution to read one off."""

    harnesses: tuple[str, ...]
    """Harness names this bundle declared, sorted."""


@dataclass(frozen=True)
class PluginDiscovery:
    """What the installed registry plugins contributed, and who contributed it."""

    harnesses: Mapping[str, HarnessSpec]
    bundles: tuple[PluginBundle, ...]


@dataclass(frozen=True)
class ResolvedHarnessRegistry:
    """The registry one adapter runs on, and which layers composed it."""

    harnesses: Mapping[str, HarnessSpec]
    plugin_bundles: tuple[PluginBundle, ...]
    overlay_file: Path | None


class DuplicateRegistrationError(ValueError):
    """Two installed entry points share ``name`` within ``group``.

    An unresolvable ambiguity: no pick is safe for anyone, so discovery fails
    every lookup into the group and names both providing distributions.
    ``ValueError`` rather than a bespoke hierarchy — a caller already refusing
    malformed registry input catches this the same way.
    """

    def __init__(self, name: str, group: str, distributions: tuple[str, str]) -> None:
        self.name = name
        self.group = group
        self.distributions = distributions
        first, second = distributions
        super().__init__(
            f"Duplicate registration of {name!r} in entry-point group {group!r}: "
            f"provided by both {first!r} and {second!r}. "
            "Uninstall or rename one to resolve the ambiguity."
        )


_discovery_cache: dict[str, dict[str, importlib.metadata.EntryPoint]] = {}


def _distribution_name(entry_point: importlib.metadata.EntryPoint) -> str:
    dist = entry_point.dist
    return dist.name if dist is not None else "<unknown distribution>"


def _discover_entry_points(group: str) -> Mapping[str, importlib.metadata.EntryPoint]:
    """Cached ``name → EntryPoint`` mapping for *group*, duplicates refused.

    Enumerates names and distributions without importing any target — ``load()``
    stays with the caller, so a broken plug-in fails only when its own name is
    used while a duplicate name fails every lookup into the group. The raise
    happens before the cache is written, so a group carrying a duplicate
    re-raises instead of serving a partial map.

    ``entry_points`` is read off :mod:`importlib.metadata` at call time rather
    than bound at import: that is the seam
    :func:`tolokaforge_coding_harnesses.testing.install_plugins` patches to make
    a fabricated bundle the installed set.
    """
    cached = _discovery_cache.get(group)
    if cached is not None:
        return cached

    mapping: dict[str, importlib.metadata.EntryPoint] = {}
    for entry_point in importlib.metadata.entry_points(group=group):
        existing = mapping.get(entry_point.name)
        if existing is not None:
            raise DuplicateRegistrationError(
                entry_point.name,
                group,
                (_distribution_name(existing), _distribution_name(entry_point)),
            )
        mapping[entry_point.name] = entry_point

    _discovery_cache[group] = mapping
    return mapping


def discover_plugin_harness_registries() -> PluginDiscovery:
    """Registry union of every installed :data:`HARNESS_REGISTRY_ENTRY_POINT_GROUP` plugin.

    Each entry point is loaded to its package, whose
    :data:`PLUGIN_REGISTRY_RESOURCE` is read through
    :func:`load_harness_registry` — so a plugin's typo is refused with the same
    message an operator overlay's would be, naming the file and the harness key.

    Returns an empty registry and no bundles when nothing is installed, which is
    the common case and the one that must stay free of surprises: no plugin, no
    change.

    Raises:
        ValueError: two installed plugins declare the same harness name. There
            is no safe pick — the two bundles disagree about what that name
            installs and how it is invoked — so the ambiguity is refused naming
            both distributions rather than resolved by install order.
            :class:`DuplicateRegistrationError` for the narrower case of two
            entry points claiming one *entry-point* name.
    """
    registry: dict[str, HarnessSpec] = {}
    declared_by: dict[str, str] = {}
    bundles: list[PluginBundle] = []
    installed = _discover_entry_points(HARNESS_REGISTRY_ENTRY_POINT_GROUP)
    for name, entry_point in sorted(installed.items()):
        distribution = entry_point.dist.name if entry_point.dist is not None else name
        version = entry_point.dist.version if entry_point.dist is not None else None
        resource = importlib.resources.files(entry_point.load()) / PLUGIN_REGISTRY_RESOURCE
        with importlib.resources.as_file(resource) as path:
            bundle = load_harness_registry(path)
        for harness_name in sorted(bundle):
            owner = declared_by.get(harness_name)
            if owner is not None:
                raise ValueError(
                    f"coding harness: harness {harness_name!r} is declared by two "
                    f"installed registry plugins, {owner!r} and {distribution!r}. Uninstall "
                    "one, or rename the harness in one of the bundles."
                )
            declared_by[harness_name] = distribution
        registry.update(bundle)
        bundles.append(
            PluginBundle(
                distribution=distribution, version=version, harnesses=tuple(sorted(bundle))
            )
        )
        logger.info(
            "coding harness: harness registry plugin %s contributed %s",
            distribution,
            sorted(bundle),
        )
    return PluginDiscovery(
        harnesses=registry,
        bundles=tuple(sorted(bundles, key=lambda entry: entry.distribution)),
    )


def resolve_effective_registry(
    presets_file: str | None = None, *, discover_plugins: bool = True
) -> ResolvedHarnessRegistry:
    """The registry one adapter runs on, composed from all three sources.

    The result carries both the composed registry and the layers that composed
    it, from the one resolution pass that runs: a second pass could disagree
    with the registry the adapter is actually using.

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
    bundles: tuple[PluginBundle, ...] = ()
    if discover_plugins:
        discovery = discover_plugin_harness_registries()
        shadowed = sorted(set(discovery.harnesses) & set(HARNESSES))
        if shadowed:
            logger.warning(
                "coding harness: installed registry plugin(s) replace shipped "
                "harness spec(s) %s; the shipped install source, pinned version and argv "
                "for those names are not what runs.",
                shadowed,
            )
        registry.update(discovery.harnesses)
        bundles = discovery.bundles
    overlay_file: Path | None = None
    if presets_file:
        overlay_file = Path(presets_file).expanduser().resolve()
        registry.update(load_harness_registry(overlay_file))
    return ResolvedHarnessRegistry(
        harnesses=registry, plugin_bundles=bundles, overlay_file=overlay_file
    )


INSTALL_SCRIPT = Path(__file__).parent / "install-harness.sh"

MIDDLEWARE_PROXY_SCRIPT = Path(__file__).parent / "middleware_proxy.py"
"""Path to the stdlib HTTP proxy that ships alongside :data:`INSTALL_SCRIPT`.

Copied into every image whose harness declares
:attr:`HarnessSpec.request_middleware`; started on-demand from the
:func:`harness_command` preamble. See :mod:`~.middleware_proxy` for the
proxy itself and :class:`RequestMiddleware` for the per-harness config
shape."""

OPENROUTER_PREFIX = "openrouter/"
"""Route marker litellm reads to select its OpenRouter handler.

A vendor CLI does not go through litellm — it reaches OpenRouter through the
``*_BASE_URL`` variables :data:`PROVIDER_ENV_KEYS` forwards. Left on the model
name, the CLI instead selects its own direct-vendor handler, reads the
deliberately blank vendor key, and fails with a 401. So a vendor harness gets
the prefix stripped, while the engine loop keeps it (litellm needs it).
"""

SHIPPED_REGISTRY_META_FILE: Path = Path(__file__).resolve().parent / "data" / "registry_meta.yaml"
"""Registry-wide catalog file: OpenRouter vendor namespaces, the closed
allow-list of provider env-var names, and the alternative-gateway catalog.
Ships as data so a new namespace, env-var name or gateway is a YAML edit
rather than a Python constant edit."""


class _RegistryMeta(BaseModel):
    """Loader-shape for ``data/registry_meta.yaml``.

    Kept private: no caller outside this module needs the model itself, only
    :data:`_VENDOR_NAMESPACE_PREFIXES`, :data:`PROVIDER_ENV_KEYS` and
    :data:`ALTERNATIVE_GATEWAYS` populated from it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    openrouter_vendor_namespaces: tuple[str, ...]
    provider_env_keys: frozenset[str]
    alternative_gateways: dict[str, RuntimeGateway] = Field(default_factory=dict)

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
            f"registry_meta.yaml not found at {path}; the shipped file ships inside "
            "the tolokaforge-coding-harnesses wheel and its absence is a packaging error."
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

ALTERNATIVE_GATEWAYS: Mapping[str, RuntimeGateway] = _REGISTRY_META.alternative_gateways
"""Gateways a :attr:`HarnessSpec.gateway_route` may name, keyed by the name it
names them by.

Shipped-only, and deliberately so: ``registry_meta.yaml`` sits outside both
fingerprint digests because it carries no overlay and no plug-in layer, and
making this catalog layerable would move the file inside that contract. An
operator routing a harness through their own gateway overlays the harness entry
instead. See :attr:`GatewayRoute.gateway` and ADR-0037 § Consequences."""

HARNESSES: dict[str, HarnessSpec] = load_harness_registry(SHIPPED_REGISTRY_FILE)
"""The shipped registry, loaded from :data:`SHIPPED_REGISTRY_FILE` at import.

Bound after :data:`PROVIDER_ENV_KEYS` and :data:`ALTERNATIVE_GATEWAYS`, which
:meth:`HarnessSpec._container_env_does_not_shadow_the_provider_envelope` and
:meth:`HarnessSpec._gateway_route_names_a_declared_gateway` read while
validating each entry.
"""

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
            f"coding harness: provider env key(s) {rejected!r} are not "
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
            f"coding harness: agent_harness {agent_harness!r} is not supported; "
            f"accepted: {list(accepted)!r}."
        )
    return agent_harness


def harness_model(
    model: str,
    agent_harness: str | None = None,
    registry: Mapping[str, HarnessSpec] = HARNESSES,
) -> str:
    """Model name as *agent_harness*'s CLI must receive it.

    Strips the ``openrouter/`` route marker (see :data:`OPENROUTER_PREFIX`)
    by default; a vendor CLI does not go through litellm and would otherwise
    select its own direct-vendor handler and fail with 401. Gated by
    :attr:`HarnessSpec.strip_openrouter_prefix` — a harness whose config
    template defines a provider literally named ``openrouter`` (opencode)
    sets it to ``False`` so the caller's ``openrouter/<vendor>/<model>``
    slug reaches the config's ``openrouter`` provider block; stripping the
    prefix for those CLIs routes the trial to a nonexistent vendor provider
    and crashes.

    When *agent_harness* names a harness whose spec declares
    :attr:`HarnessSpec.strip_vendor_namespace`, also strips a leading
    ``vendor/`` namespace so the model name is the CLI-catalog bare form
    (``gpt-5-mini`` from ``openrouter/openai/gpt-5-mini``).

    *agent_harness* defaults to ``None`` for callers that only need the
    ``openrouter/`` strip (or for the engine loop, which keeps everything);
    with ``None``, the default of ``strip_openrouter_prefix=True`` applies.
    """
    spec = registry.get(agent_harness) if agent_harness is not None else None
    strip_prefix = spec.strip_openrouter_prefix if spec is not None else True
    if strip_prefix and model.startswith(OPENROUTER_PREFIX):
        model = model[len(OPENROUTER_PREFIX) :]
    if spec is not None and spec.strip_vendor_namespace:
        for prefix in _VENDOR_NAMESPACE_PREFIXES:
            if model.startswith(prefix):
                return model[len(prefix) :]
    return model


MIDDLEWARE_PROXY_CONTAINER_PATH = "/opt/tolokaforge/middleware_proxy.py"
"""Where ``install-harness.sh`` writes the middleware proxy script inside
every image whose harness declares :attr:`HarnessSpec.request_middleware`."""


def _middleware_preamble(middleware: RequestMiddleware) -> list[str]:
    """Preamble steps that boot the middleware proxy and redirect the CLI to it.

    Emitted BEFORE ``config_files`` / env-model-vars exports so the CLI's
    template renders and its environment inherit the redirected base-URL.
    Two steps: start the proxy in daemon mode against the current value of
    :attr:`RequestMiddleware.upstream_env_key`; then rewrite that env var to
    ``http://127.0.0.1:<port>`` for everything downstream.

    The proxy's ``--daemon`` mode double-forks and only returns when the
    listener is bound, so the CLI's first request cannot race the proxy's
    startup.
    """
    # The upstream URL is an ``${ENV_VAR}`` reference that MUST expand at
    # shell time — the CLI process's provider_env supplies its value; the
    # adapter never sees the resolved URL, so binding it here would freeze
    # a stale one. Quote the fixed tokens (path, JSON payloads) but pass
    # the reference itself inside a double-quoted string that bash expands
    # before ``python3`` sees it. ``shlex.quote`` uses single quotes, which
    # would suppress the expansion.
    upstream_ref = f'"${{{middleware.upstream_env_key}}}"'
    boot_args: list[str] = [
        "python3",
        shlex.quote(MIDDLEWARE_PROXY_CONTAINER_PATH),
        "--port",
        str(middleware.port),
        "--upstream",
        upstream_ref,
        "--body-inject",
        shlex.quote(json.dumps(middleware.body_injections)),
        "--header-inject",
        shlex.quote(json.dumps(middleware.header_injections)),
    ]
    if middleware.path_filter is not None:
        boot_args += ["--path-filter", shlex.quote(middleware.path_filter)]
    boot_args.append("--daemon")
    boot = " ".join(boot_args)
    rewrite = f"export {middleware.upstream_env_key}=http://127.0.0.1:{middleware.port}"
    return [boot, rewrite]


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
            "coding harness: the provider envelope carries several entries a "
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
            f"coding harness: agent_harness {agent_harness!r} runs no CLI; "
            "the trial goes through the engine's LLM turn loop instead."
        )
    resolved_model = harness_model(model, agent_harness, registry)

    # Pre-exec preamble: on-disk config-file emission + env-quartet exports.
    preamble_parts: list[str] = []
    if spec.request_middleware is not None:
        preamble_parts.extend(_middleware_preamble(spec.request_middleware))
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
