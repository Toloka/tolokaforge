"""Vendor coding-CLI :class:`~tolokaforge.core.agent_driver.AgentDriver`.

Replaces the engine's own LLM turn loop with a single invocation of a
vendor coding-agent CLI inside the trial container. Owns command assembly,
metadata emission, tool-schema payload, and compose-layer synthesis in one
object the orchestrator holds and applies around adapter output. Adapters
carry no mode state — they expose
:meth:`~tolokaforge.adapters.base.BaseAdapter.stage_task` for this driver to
layer onto.

The driver:

- resolves the vendor CLI from
  :data:`~tolokaforge_coding_harnesses.HARNESSES` (with operator overlay
  + plugin discovery) once per run;
- reads the resolved :class:`~tolokaforge_coding_harnesses.HarnessSpec` +
  the run's provider envelope out of construction args (both live on
  the driver instance — not the adapter);
- decorates the wire :class:`~tolokaforge.runner.models.TaskDescription`
  with the four-key ``agent_harness_*`` metadata handshake the conductor
  branches on, the ``bash`` docker-exec tool schema the runner installs,
  and ``test_execution`` grading defaults;
- layers a CLI-install Dockerfile snippet + runner + db-service sidecars
  onto the adapter's staged compose file.

The driver depends on :mod:`tolokaforge_coding_harnesses` — one-way — so
the boundary invariant (harness package must not import the engine)
still holds.
"""

from __future__ import annotations

import hashlib
import logging
import shlex
import shutil
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import yaml

from tolokaforge.adapters.base import ComposeImageBuild
from tolokaforge.core.agent_driver import StagedTask, StagedTaskLayers
from tolokaforge.runner.llm_gateway import (
    GATEWAY_HOSTNAME,
    GatewayHandle,
)
from tolokaforge.secrets import expand_secret_refs
from tolokaforge.secrets import get_default as _get_default_secrets
from tolokaforge_coding_harnesses import (
    ALTERNATIVE_GATEWAYS,
    INSTALL_SCRIPT,
    MIDDLEWARE_PROXY_CONTAINER_PATH,
    MIDDLEWARE_PROXY_SCRIPT,
    GatewayRoute,
    HarnessSpec,
    ResolvedHarnessRegistry,
    harness_command,
    resolve_effective_registry,
    validate_harness,
    validate_provider_env_keys,
)

if TYPE_CHECKING:
    from tolokaforge.runner.models import TaskDescription

logger = logging.getLogger(__name__)


_HARNESS_INSTALL_CONTAINER_PATH = "/opt/tolokaforge/install-harness.sh"
"""Where the shipped installer lands inside the layered image."""

_HARNESS_DOCKERFILE_NAME = "harness.Dockerfile"
"""Filename the driver writes under the staging dir; compose's
``build.dockerfile:`` reads this."""

_DEFAULT_BASH_TIMEOUT_S = 900.0
"""Bash-tool timeout the driver installs on the ``bash`` schema.

The CLI runs to completion inside a single ``exec`` — one bash-tool call
covers the whole trial's agent budget, so the timeout is measured in
minutes, not seconds. Matches the engine's default ``episode_s`` so runs
that keep the default do not surface the "harness needs Xs but the run
allows Ys" refusal. A pack that needs more raises both
``orchestrator.timeouts.episode_s`` and the driver's default (or the
task's own ``trial_seconds``) together."""

_DEFAULT_RUNNER_IMAGE = "tolokaforge-runner:local"
_DEFAULT_DB_SERVICE_IMAGE = "tolokaforge-db-service:local"

_SIDECAR_GATEWAY_PORT = 8080
"""Fixed TCP port the credential-gateway sidecar binds inside its
compose service. Trial containers reach it as
``http://{GATEWAY_HOSTNAME}:{_SIDECAR_GATEWAY_PORT}``; docker's compose
DNS resolves the hostname on the shared internal network, so no
``extra_hosts`` alias is required and the shield works under every
network policy the runtime enforces."""

_HARNESS_BUILD_PROFILE = "_harness_build_only"
"""Compose profile that keeps ``<agent_service>_base`` out of
``docker compose up`` — the build-only base is used at pre-build only,
never brought up as part of the trial stack."""

RUNNER_SERVICE = "runner"
DB_SERVICE = "db-service"


def _resolve_gateway_route(
    route: GatewayRoute, agent_model: str
) -> tuple[dict[str, str], dict[str, str], str]:
    """Resolve a :class:`GatewayRoute` into (provider_env, config_files, model).

    Turns the ``${gateway.base_url}``, ``${gateway.passthrough_path}``, and
    ``${secret:NAME}`` tokens ADR-0037 documents into literal strings the
    driver hands downstream — the harness registry package ships
    :class:`GatewayRoute` as pure data, so this expansion is the coding-
    harness driver's job as the consuming runtime. Reads secrets via
    :class:`SecretManager` (never ``os.environ``); refuses a route naming
    an :data:`~tolokaforge_coding_harnesses.ALTERNATIVE_GATEWAYS` entry
    that is not declared, and raises on any leftover ``${…}`` token so a
    typo cannot ship to the CLI unresolved.

    Returns:
        A 3-tuple ``(provider_env, config_files, effective_model)`` where
        ``provider_env`` is the literal env map to seed the container
        with, ``config_files`` is the literal on-disk map to write
        (``GatewayRoute.config_files`` values with tokens resolved), and
        ``effective_model`` is the model string after
        ``model_alias_pattern`` has been applied (unchanged when the
        pattern is absent).
    """
    gateway = ALTERNATIVE_GATEWAYS.get(route.gateway)
    if gateway is None:  # pragma: no cover — HarnessSpec validator rejects at load
        raise ValueError(
            f"gateway_route.gateway {route.gateway!r} is not in ALTERNATIVE_GATEWAYS; "
            "the shipped registry validator normally catches this at load."
        )
    secret_manager = _get_default_secrets()
    base_url = secret_manager.get_secret_or_raise(gateway.base_url_env)
    tokens = {
        "gateway.base_url": base_url,
        "gateway.passthrough_path": route.passthrough_path,
    }

    def _resolve(value: str, *, where: str) -> str:
        expanded = expand_secret_refs(value, secret_manager, where=where)
        for name, replacement in tokens.items():
            expanded = expanded.replace("${" + name + "}", replacement)
        if "${" in expanded:
            raise ValueError(
                f"gateway_route {where}: value carries unresolved token(s) after "
                f"substitution: {expanded!r}. Accepted tokens are "
                f"${{gateway.base_url}}, ${{gateway.passthrough_path}}, and "
                f"${{secret:NAME}} — anything else is a typo."
            )
        return expanded

    provider_env = {
        key: _resolve(value, where=f"gateway_route.provider_env[{key!r}]")
        for key, value in route.provider_env.items()
    }
    config_files = {
        path: _resolve(content, where=f"gateway_route.config_files[{path!r}]")
        for path, content in route.config_files.items()
    }
    effective_model = (
        route.model_alias_pattern.format(model=agent_model)
        if route.model_alias_pattern is not None
        else agent_model
    )
    return provider_env, config_files, effective_model


@dataclass(frozen=True)
class HarnessSelection:
    """Inputs the orchestrator hands the driver at construction time.

    Attributes:
        agent_harness: Registry name — one of the shipped vendor CLIs
            or an operator-overlay entry.
        agent_model: Fully-qualified model id the CLI is pinned to
            (``models.agent.name`` verbatim). Must be non-empty; the
            CLI would otherwise pick its own default and the model
            under measurement would drift.
        version_override: Optional CLI version pin from
            ``models.agent.coding_harness_version`` (or the slug's
            ``@version`` suffix). ``None`` means "use the shipped
            registry pin".
        provider_env_declared: Operator-declared overrides for the
            provider envelope the CLI reaches its provider with. Rare;
            defaults to empty.
        presets_file: Operator-overlay YAML path; passed through to
            :func:`resolve_effective_registry`.
        plugin_discovery: Whether to discover installed plug-in
            registries. Default ``True`` matches the current behaviour.
        disable_credential_gateway: Escape hatch from
            ``models.agent.disable_credential_gateway``. ``True`` strips
            the resolved spec's ``credential_gateway`` before any other
            constructor logic runs, so the driver takes the pre-shield
            path throughout — same as a harness that ships no
            ``credential_gateway`` at all. Logs a warning naming the
            harness. No effect when the harness has no
            ``credential_gateway`` to begin with.
        gateway_route: Name of an
            :data:`~tolokaforge_coding_harnesses.ALTERNATIVE_GATEWAYS`
            entry to route this harness through instead of its default
            provider. When ``None`` (default) the driver takes the
            shielded ``credential_gateway`` path (or the unshielded path
            for harnesses that ship none). When set, the harness's
            ``gateway_route`` block for the same gateway name is picked:
            ``provider_env`` tokens (``${gateway.base_url}``,
            ``${gateway.passthrough_path}``, ``${secret:NAME}``) resolve
            via :class:`SecretManager`, ``model_alias_pattern`` renders
            with the run's model, ``config_files`` lands in the trial
            container verbatim, and the credential-shield sidecar is
            skipped (ADR-0037 § "The two paths are alternatives, not
            layers"). Refused at construction if the resolved spec has
            no ``gateway_route`` block or if it names a different
            gateway. Skips ``request_middleware`` too — a gateway route
            replaces the per-provider pin the middleware exists to
            inject.
    """

    agent_harness: str
    agent_model: str
    version_override: str | None = None
    provider_env_declared: Mapping[str, str] = field(default_factory=dict)
    presets_file: str | None = None
    plugin_discovery: bool = True
    disable_credential_gateway: bool = False
    gateway_route: str | None = None


class CodingHarnessDriver:
    """Drives a trial via a vendor coding-agent CLI in-container.

    Constructed once per run. Owns the resolved
    :class:`~tolokaforge_coding_harnesses.HarnessSpec`, the model pin,
    and the resolved provider envelope. Layers all of that onto the
    adapter's staged compose file at trial start; decorates the wire
    :class:`~tolokaforge.runner.models.TaskDescription` at every
    ``load_tasks`` call so the conductor + runner see the four-key
    handshake and the ``bash`` docker-exec tool schema.
    """

    name: ClassVar[str] = "coding_harness"

    def __init__(self, selection: HarnessSelection) -> None:
        self.selection = selection
        self._resolved_registry: ResolvedHarnessRegistry = resolve_effective_registry(
            selection.presets_file, discover_plugins=selection.plugin_discovery
        )
        validate_harness(selection.agent_harness, self._resolved_registry.harnesses)
        base_spec = self._resolved_registry.harnesses[selection.agent_harness]
        if not selection.agent_model:
            raise ValueError(
                f"coding_harness driver: agent_harness {selection.agent_harness!r} "
                "requires a non-empty agent_model — the CLI selects its own default "
                "otherwise, so the run config's model would not be the one measured."
            )
        if selection.version_override is not None:
            if not selection.version_override:
                raise ValueError(
                    f"coding_harness driver: agent_harness {selection.agent_harness!r} "
                    "version_override must be non-empty when set; drop the field to "
                    "use the shipped pin."
                )
            base_spec = base_spec.model_copy(update={"version": selection.version_override})
        escape_hatch_active = (
            selection.disable_credential_gateway and base_spec.credential_gateway is not None
        )
        if escape_hatch_active:
            logger.warning(
                "coding_harness driver: harness %r credential gateway disabled — "
                "provider credentials will be baked into the trial container's "
                "environment; the model can read them via printenv.",
                selection.agent_harness,
            )
            base_spec = base_spec.model_copy(update={"credential_gateway": None})
        self._gateway_handle: GatewayHandle | None = None
        self._gateway_container_url: str | None = None
        self._gateway_upstream_token: str | None = None
        self._gateway_route_config_files: dict[str, str] = {}
        self._effective_model: str = selection.agent_model
        if selection.gateway_route is not None:
            # Gateway-route path (ADR-0037). Mutually exclusive with the
            # credential-shield sidecar: the operator's own gateway (LiteLLM
            # today) has its own auth boundary, so the shielded upstream
            # rewriting has nothing to do here. ``request_middleware`` is
            # also skipped — the gateway route replaces the per-provider pin
            # the middleware injects, so running both would double-book the
            # provider decision.
            route = base_spec.gateway_route
            if route is None:
                raise ValueError(
                    f"coding_harness driver: run config selected "
                    f"gateway_route={selection.gateway_route!r}, but harness "
                    f"{selection.agent_harness!r} ships no gateway_route block."
                )
            if route.gateway != selection.gateway_route:
                raise ValueError(
                    f"coding_harness driver: run config selected "
                    f"gateway_route={selection.gateway_route!r}, but harness "
                    f"{selection.agent_harness!r}'s gateway_route names "
                    f"{route.gateway!r}. The registry pin decides which gateway "
                    "this CLI knows how to reach; matching in the run config is "
                    "the operator's opt-in signal, not a rename."
                )
            resolved_provider_env, resolved_config_files, effective_model = _resolve_gateway_route(
                route, selection.agent_model
            )
            base_spec = base_spec.model_copy(
                update={
                    "credential_gateway": None,
                    "request_middleware": None,
                    # The alias ``_resolve_gateway_route`` produced already
                    # names the model the gateway serves it as — kimi's
                    # ``{model}-moonshotai-pinned`` renders
                    # ``moonshotai/kimi-k2.7-code-moonshotai-pinned``, the
                    # exact ``model_name`` LiteLLM's proxy config
                    # registers. Letting ``harness_command`` re-run
                    # ``harness_model``'s vendor-namespace strip on top of
                    # that would drop the ``moonshotai/`` prefix and turn
                    # the request into ``kimi-k2.7-code-moonshotai-pinned``
                    # — an unregistered alias, and the proxy answers
                    # ``400 Invalid model name``. ``strip_vendor_namespace``
                    # was for the default OpenRouter path where the CLI
                    # wants the raw vendor slug; the alias path already
                    # produced its final form and must land verbatim.
                    "strip_vendor_namespace": False,
                }
            )
            self.spec: HarnessSpec = base_spec
            declared = _resolve_provider_env(
                shipped={},
                declared=dict(selection.provider_env_declared),
                agent_harness=selection.agent_harness,
            )
            self.container_env: dict[str, str] = {
                **declared,
                **resolved_provider_env,
                **dict(route.container_env),
            }
            self._gateway_route_config_files = resolved_config_files
            self._effective_model = effective_model
            logger.info(
                "coding_harness driver: harness %r routed through gateway %r; "
                "credential shield + request_middleware bypassed. Effective model: %r.",
                selection.agent_harness,
                route.gateway,
                effective_model,
            )
            return
        self.spec: HarnessSpec = base_spec
        if self.spec.credential_gateway is None:
            # Pre-shield path: the real credential is resolved straight into
            # container_env, same as every harness did before this driver
            # grew a gateway. Reachable either because the harness
            # intentionally ships no credential_gateway (see
            # UNSHIELDED_HARNESSES in tests/unit/test_credential_gateway_schema.py)
            # or because escape_hatch_active just stripped one — every other
            # shipped harness takes the `else` branch below.
            self.container_env: dict[str, str] = _resolve_provider_env(
                shipped=dict(self.spec.provider_env),
                declared=dict(selection.provider_env_declared),
                agent_harness=selection.agent_harness,
            )
            if not escape_hatch_active:
                logger.warning(
                    "coding_harness driver: harness %r ships no credential_gateway; "
                    "its real provider credential reaches the trial container's "
                    "environment unshielded. See "
                    "https://github.com/Toloka/tolokaforge/issues/1311.",
                    selection.agent_harness,
                )
        else:
            # Shielded path: never resolve the upstream token here — that is
            # the gateway's job, done from its own SecretManager call at
            # attach() below. container_env carries only the dummy value the
            # CLI is allowed to see; the gateway's own container-reachable
            # URL is added once attach() has actually launched it.
            gateway_spec = self.spec.credential_gateway
            extra_env = _resolve_provider_env(
                shipped={},
                declared=dict(selection.provider_env_declared),
                agent_harness=selection.agent_harness,
            )
            self.container_env = {
                **extra_env,
                gateway_spec.dummy_token_env_var: gateway_spec.dummy_token_value,
                # Under LIMITED_INTERNET, netpolicy points every service's
                # HTTP(S)_PROXY at squid. Squid would then intercept the
                # CLI's gateway hop too — and squid's allowlist (empty by
                # default for a shielded run, since the CLI needs no
                # external destinations) would 403 it. NO_PROXY on the
                # CLI tells its HTTP client to skip squid for the sidecar
                # hostname; the CLI reaches the sidecar directly over the
                # netpolicy internal network the sidecar is bridged onto.
                # ``enforce_network_policy._merge_proxy_env`` unions this
                # with squid's own no-proxy list, so both survive: the
                # sidecar hop is direct, everything else transits squid.
                # Under NO_INTERNET and FULL_INTERNET there is no squid;
                # the variable is inert then.
                "NO_PROXY": GATEWAY_HOSTNAME,
                "no_proxy": GATEWAY_HOSTNAME,
            }

    # -- driver protocol ----------------------------------------------------

    def needs_container_stage(self) -> bool:
        return True

    def needs_docker_cli(self) -> bool:
        return True

    def attach(self, adapter_name: str, staged_ok: bool) -> None:
        if not staged_ok:
            raise ValueError(
                f"coding_harness driver: adapter {adapter_name!r} does not stage a "
                "per-trial container (its stage_task() returned None). The driver "
                "layers the CLI install onto a staged compose file; adapters that "
                "run without a container cannot host this driver. Compatible "
                "adapters today: 'native', 'terminal_bench'."
            )
        gateway_spec = self.spec.credential_gateway
        if gateway_spec is not None:
            # Sidecar-mode credential shield. The gateway is a compose service
            # in the trial stack (added in ``_rewrite_compose``), reachable
            # from the CLI's container at a fixed name and port over docker's
            # DNS — no host-gateway hop, no ``extra_hosts`` alias, no
            # dependence on the pack's declared ``network_policy``. The real
            # provider credential is resolved via :class:`SecretManager` here
            # and stashed into the sidecar's compose env at ``_rewrite_compose``
            # time; it never lands in the CLI's own ``environment:`` block.
            secret_manager = _get_default_secrets()
            self._gateway_upstream_token = secret_manager.get_secret_or_raise(
                gateway_spec.upstream_token_env_var
            )
            self._gateway_handle = GatewayHandle(
                port=_SIDECAR_GATEWAY_PORT, hostname=GATEWAY_HOSTNAME
            )
            self._gateway_container_url = (
                f"http://{self._gateway_handle.hostname}:{self._gateway_handle.port}"
            )
            self.container_env[gateway_spec.base_url_env_var] = self._gateway_container_url

    def close(self) -> None:
        """No-op under sidecar mode: the compose stack teardown stops the
        gateway service alongside every other trial container. Method kept
        on the :class:`AgentDriver` protocol so runtimes may call it
        unconditionally. Idempotent — safe without a prior ``attach()``."""
        self._gateway_handle = None
        self._gateway_upstream_token = None

    def decorate_task_description(
        self,
        base: TaskDescription,
        *,
        staged: StagedTask | None,
    ) -> TaskDescription:
        """Rewrite tools + grading + metadata for CLI-mode.

        The runner installs the returned ``bash`` schema and the
        conductor branches on ``metadata['agent_harness_command']``.
        """
        if staged is None:
            raise ValueError(
                "coding_harness driver: decorate_task_description requires a "
                "StagedTask; the orchestrator must call adapter.stage_task first."
            )
        from tolokaforge.runner.models import (
            InvocationStyle,
            RunnerGradingConfig,
            ToolSchema,
            ToolSource,
        )

        bash_schema = ToolSchema(
            name="bash",
            description="Execute a bash command inside the task container",
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to run",
                    }
                },
                "required": ["command"],
            },
            category="compute",
            timeout_s=_DEFAULT_BASH_TIMEOUT_S,
            source=ToolSource(
                toolset="coding_harness",
                module_path="",
                class_name="bash",
                invocation_style=InvocationStyle.DOCKER_COMPOSE_EXEC,
                extra={
                    "service": staged.agent_service,
                    "compose_project_prefix": staged.compose_project_prefix,
                },
            ),
        )
        grading = RunnerGradingConfig(
            combine_method="weighted",
            weights={"custom_checks": 1.0},
            pass_threshold=0.5,
            grading_method="test_execution",
        )
        instruction = base.metadata.get("initial_user_message") or base.description or ""
        command = harness_command(
            self.selection.agent_harness,
            instruction=instruction,
            model=self._effective_model,
            registry={self.selection.agent_harness: self.spec},
            provider_env=self.container_env,
            extra_config_files=(self._gateway_route_config_files or None),
        )
        metadata: dict[str, Any] = dict(base.metadata)
        metadata.update(
            {
                "agent_harness": self.selection.agent_harness,
                "agent_harness_version": self.spec.version,
                "agent_harness_model": self._effective_model,
                "agent_harness_command": command,
                "agent_visible_dir": metadata.get("agent_visible_dir", "/work"),
            }
        )
        # Redirect the wire environment_manifest at the *staged* compose
        # file (with the harness layer + runner/db-service sidecars) and
        # at the injected ``runner`` sidecar as the trial's runner service
        # — the pack's own compose file has neither the sidecars nor the
        # layered image, and its runner_service is the pack's agent
        # container which does not expose the grader RPC port.
        env_manifest = base.environment_manifest
        if env_manifest is not None:
            manifest_update: dict[str, Any] = {
                "compose_file": staged.compose_file,
                "runner_service": RUNNER_SERVICE,
            }
            # Shield sidecar bridge. The gateway sidecar the driver adds in
            # ``_rewrite_compose`` must be reachable from the CLI's service
            # (over the netpolicy internal network) AND able to reach the
            # LLM provider on the internet (over the edge network). Marking
            # its compose service name as ``bridged_services`` gives it
            # both attachments under ``no_internet`` and
            # ``limited_internet`` — the same treatment
            # :attr:`~EnvironmentManifest.runner_service` gets. Under
            # ``full_internet`` it has no effect (netpolicy is identity).
            # The pack's own declared ``network_policy`` is left untouched;
            # no elevation, no downgrade — the sidecar keeps working
            # whichever posture the pack picked.
            if self._gateway_handle is not None:
                manifest_update["bridged_services"] = frozenset(env_manifest.bridged_services) | {
                    GATEWAY_HOSTNAME
                }
                # The gateway sidecar already carries the real upstream
                # token. Strip it from the runner container's
                # ``TOLOKAFORGE_SECRETS_JSON`` payload so the trial's
                # compose file holds only one copy of the credential —
                # the sidecar's — instead of duplicating it into every
                # container that shares the trial network. The rest of
                # the runner's payload (judge keys, litellm endpoints,
                # tokenizer cache paths) is untouched.
                gateway_spec = self.spec.credential_gateway
                assert (
                    gateway_spec is not None
                )  # attach() sets _gateway_handle iff spec is not None
                manifest_update["stripped_container_secrets"] = frozenset(
                    env_manifest.stripped_container_secrets
                ) | {gateway_spec.upstream_token_env_var}
            elif self.selection.gateway_route is not None:
                # Gateway-route mode. There is no shield sidecar to bridge
                # (skipped in ``__init__``): the CLI's own container hits
                # the external gateway (LiteLLM today) directly. Under a
                # pack that declared ``no_internet`` / ``limited_internet``
                # the CLI's service otherwise lives on
                # ``netpolicy_internal`` alone and cannot reach the
                # gateway (empirically: the CLI's own retry loop dies after
                # 10 ``APIConnectionError`` attempts inside the isolated
                # network). Marking the CLI's compose service as
                # ``bridged_services`` gives it BOTH internal-net (grader
                # RPC to the runner) AND edge-net (egress to the gateway)
                # attachments, same treatment the shield sidecar gets. The
                # operator's choice of ``gateway_route`` is the opt-in
                # signal that this run's CLI is expected to reach an
                # external endpoint.
                manifest_update["bridged_services"] = frozenset(env_manifest.bridged_services) | {
                    staged.agent_service
                }
            env_manifest = env_manifest.model_copy(update=manifest_update)
        return base.model_copy(
            update={
                "agent_tools": [bash_schema],
                "grading": grading,
                "metadata": metadata,
                "environment_manifest": env_manifest,
            }
        )

    def apply_container_layers(self, *, staged: StagedTask) -> StagedTaskLayers:
        """Write the harness Dockerfile + rewrite compose in place.

        Adds runner + db-service sidecars to the compose file, bakes
        the resolved provider envelope into the agent-service
        ``environment:`` block, and returns the extra
        :class:`ComposeImageBuild` the orchestrator pre-builds.
        """
        self._write_install_dockerfile(
            context_dir=staged.staging_dir,
            base_image=staged.base_image,
        )
        layered_image = _layered_image_tag(
            staged.task_id, self.selection.agent_harness, self.spec.version
        )
        doc = yaml.safe_load(staged.compose_file.read_text()) or {}
        doc = self._rewrite_compose(
            doc=doc,
            agent_service=staged.agent_service,
            base_build_service=staged.base_build_service,
            base_image=staged.base_image,
            layered_image=layered_image,
        )
        staged.compose_file.write_text(yaml.safe_dump(doc, sort_keys=False))
        return StagedTaskLayers(
            stack_requirements=[
                ComposeImageBuild(
                    compose_file=staged.compose_file, service=staged.base_build_service
                ),
                ComposeImageBuild(compose_file=staged.compose_file, service=staged.agent_service),
            ],
            provider_env_snapshot=dict(self.container_env),
        )

    # -- internals ----------------------------------------------------------

    def _write_install_dockerfile(self, *, context_dir: Path, base_image: str) -> None:
        install_source = INSTALL_SCRIPT.name
        shutil.copy2(INSTALL_SCRIPT, context_dir / install_source)
        lines = [
            f"FROM {base_image}",
            f"COPY {install_source} {_HARNESS_INSTALL_CONTAINER_PATH}",
            f"RUN sh {_HARNESS_INSTALL_CONTAINER_PATH} {self.spec.install_method} "
            f"{shlex.quote(self.spec.install_source)} {shlex.quote(self.spec.version)}",
        ]
        if self.spec.request_middleware is not None:
            middleware_source = MIDDLEWARE_PROXY_SCRIPT.name
            shutil.copy2(MIDDLEWARE_PROXY_SCRIPT, context_dir / middleware_source)
            lines.append(f"COPY {middleware_source} {MIDDLEWARE_PROXY_CONTAINER_PATH}")
        (context_dir / _HARNESS_DOCKERFILE_NAME).write_text("\n".join(lines) + "\n")

    def _rewrite_compose(
        self,
        *,
        doc: dict[str, Any],
        agent_service: str,
        base_build_service: str,
        base_image: str,
        layered_image: str,
    ) -> dict[str, Any]:
        doc = deepcopy(doc)
        services: dict[str, Any] = doc.setdefault("services", {})
        agent_body: dict[str, Any] = services.get(agent_service, {})
        task_build = agent_body.get("build")
        if task_build is None:
            raise ValueError(
                f"coding_harness driver: staged compose service {agent_service!r} "
                "must declare a `build:` entry the harness layer FROMs; got no build."
            )
        agent_body["image"] = layered_image
        agent_body["build"] = {"context": ".", "dockerfile": _HARNESS_DOCKERFILE_NAME}
        agent_env = _set_env(agent_body.get("environment"), "TEST_DIR", "/tests")
        for key, value in sorted(self.spec.container_env.items()):
            agent_env = _set_env(agent_env, key, value)
        for key, value in sorted(self.container_env.items()):
            agent_env = _set_env(agent_env, key, value)
        agent_body["environment"] = agent_env
        if self._gateway_handle is not None:
            # Gate the CLI's container on the gateway sidecar's healthcheck so
            # the CLI never issues its first request against a
            # not-yet-listening ``tolokaforge-llm-gateway:8080``.
            depends_on = agent_body.get("depends_on")
            if not isinstance(depends_on, dict):
                depends_on = {}
            depends_on[GATEWAY_HOSTNAME] = {"condition": "service_healthy"}
            agent_body["depends_on"] = depends_on
        services[agent_service] = agent_body

        # The base build service tags the pack's own build with the exact
        # image the harness Dockerfile ``FROM``s (``staged.base_image``,
        # baked into the Dockerfile by ``_write_install_dockerfile``). The
        # adapter picks the tag; the driver honours it — the two sides must
        # not derive it independently or the harness layer refuses to pull.
        services[base_build_service] = {
            "image": base_image,
            "build": deepcopy(task_build),
            "profiles": [_HARNESS_BUILD_PROFILE],
        }
        services[RUNNER_SERVICE] = _runner_service_body(_DEFAULT_RUNNER_IMAGE)
        services[DB_SERVICE] = _db_service_body(_DEFAULT_DB_SERVICE_IMAGE)
        if self._gateway_handle is not None:
            services[GATEWAY_HOSTNAME] = self._gateway_service_body()
        return doc

    def _gateway_service_body(self) -> dict[str, Any]:
        """Compose service definition for the credential-shield gateway
        sidecar. Runs the shipped :mod:`tolokaforge.core.drivers.\
        llm_gateway_serve` entrypoint against the runner image (already
        carries every tolokaforge dep). The real upstream token lives on
        this service's ``environment:`` only — the CLI's own service sees
        the ``dummy_token_value`` and the container-DNS URL.

        Preconditions (guaranteed by ``attach()``):

        * ``self.spec.credential_gateway`` is not ``None`` — a
          precondition of adding this sidecar at all.
        * ``self._gateway_upstream_token`` is set — resolved via
          :class:`SecretManager` in ``attach()``.

        The sidecar joins the netpolicy internal + edge networks via
        :attr:`~tolokaforge.runner.models.EnvironmentManifest\
        .bridged_services`, so the CLI reaches it over the internal
        network and it reaches the upstream over the edge network,
        regardless of the pack's declared ``network_policy``.
        """
        gateway = self.spec.credential_gateway
        assert gateway is not None  # narrowed by attach() preconditions
        assert self._gateway_upstream_token is not None  # attach() resolved it
        return {
            "image": _DEFAULT_RUNNER_IMAGE,
            "command": [
                "python",
                "-m",
                "tolokaforge.runner.llm_gateway_serve",
            ],
            "environment": {
                "TF_GATEWAY_UPSTREAM_URL": gateway.upstream_url,
                "TF_GATEWAY_UPSTREAM_TOKEN": self._gateway_upstream_token,
                "TF_GATEWAY_UPSTREAM_AUTH_HEADER": gateway.upstream_auth_header,
                "TF_GATEWAY_UPSTREAM_AUTH_TEMPLATE": gateway.upstream_auth_template,
                "TF_GATEWAY_PATH_ALLOWLIST": ",".join(gateway.path_allowlist),
                "TF_GATEWAY_PORT": str(_SIDECAR_GATEWAY_PORT),
            },
            "healthcheck": {
                "test": [
                    "CMD",
                    "bash",
                    "-c",
                    f"echo > /dev/tcp/127.0.0.1/{_SIDECAR_GATEWAY_PORT}",
                ],
                "interval": "2s",
                "timeout": "3s",
                "retries": 30,
                "start_period": "3s",
            },
        }


def _resolve_provider_env(
    *, shipped: dict[str, str], declared: dict[str, str], agent_harness: str
) -> dict[str, str]:
    """Merge shipped + declared, expand ``${secret:...}`` refs, guard against
    values that would corrupt a compose ``environment:`` entry."""
    effective = shipped | declared
    if not effective:
        return {}
    validate_provider_env_keys(effective)
    secrets = _get_default_secrets()
    resolved = {
        key: expand_secret_refs(
            value,
            secrets,
            where=(
                f"coding_harness driver provider_env_declared[{key!r}]"
                if key in declared
                else f"coding_harness driver harness {agent_harness!r} provider_env[{key!r}]"
            ),
        )
        for key, value in effective.items()
    }
    unrepresentable = sorted(
        key for key, value in resolved.items() if any(char in value for char in ("\n", "\r", "$"))
    )
    if unrepresentable:
        raise ValueError(
            f"coding_harness driver: provider env value(s) for {unrepresentable!r} "
            "contain a newline or a `$`; each value becomes one compose "
            "`environment:` entry and docker compose either splits the line or "
            "truncates at the `$`. Store the secret under a different name or "
            "rewrite the value."
        )
    return resolved


def _set_env(existing: Any, key: str, value: str) -> Any:
    """Set ``key=value`` on a compose service's ``environment:`` value.

    Preserves the shape the pack declared (list of ``KEY=value`` strings or
    mapping); replaces any prior entry for the same key rather than
    duplicating it.
    """
    if isinstance(existing, dict):
        return {**existing, key: value}
    if isinstance(existing, list):
        filtered = [
            entry
            for entry in existing
            if not (isinstance(entry, str) and entry.split("=", 1)[0] == key)
        ]
        filtered.append(f"{key}={value}")
        return filtered
    return {key: value}


def _runner_service_body(runner_image: str) -> dict[str, Any]:
    return {
        "image": runner_image,
        "ports": ["50051"],
        "environment": {"DB_SERVICE_URL": "http://db-service:8000"},
        "healthcheck": {
            "test": ["CMD", "bash", "-c", "echo > /dev/tcp/127.0.0.1/50051"],
            "interval": "2s",
            "timeout": "3s",
            "retries": 30,
            "start_period": "3s",
        },
        "depends_on": {DB_SERVICE: {"condition": "service_healthy"}},
    }


def _db_service_body(db_service_image: str) -> dict[str, Any]:
    return {
        "image": db_service_image,
        "ports": ["8000"],
        "healthcheck": {
            "test": ["CMD-SHELL", "curl -fs http://localhost:8000/health || exit 1"],
            "interval": "2s",
            "timeout": "3s",
            "retries": 30,
            "start_period": "3s",
        },
    }


def _layered_image_tag(task_id: str, agent_harness: str, harness_version: str) -> str:
    safe_task = _slugify(task_id)
    safe_version = _slugify(harness_version)
    return f"tolokaforge-{safe_task}-{agent_harness}-{safe_version}:local"


def _slugify(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in value)


def content_digest_for(task_dir: Path, *, agent_harness: str, spec: HarnessSpec) -> str:
    """Deterministic per-task staging tag the adapter uses to name the
    staging directory. Kept here (not on the adapter) so the driver owns
    the hash inputs it depends on — an adapter that stages without a
    driver would not need to hash the harness fields."""
    hasher = hashlib.sha256()
    for path in sorted(task_dir.rglob("*")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(task_dir).as_posix()
        hasher.update(b"P|" + rel.encode() + b"\n")
        if path.is_file():
            hasher.update(b"C|")
            hasher.update(path.read_bytes())
            hasher.update(b"\n")
    hasher.update(b"|harness|\n")
    hasher.update(f"agent_harness={agent_harness}\n".encode())
    hasher.update(f"harness_version={spec.version}\n".encode())
    hasher.update(f"harness_install_source={spec.install_source}\n".encode())
    return hasher.hexdigest()[:16]


__all__ = ["CodingHarnessDriver", "HarnessSelection"]
