"""Coding-harness CLI support for benchmark trials.

A harness trial replaces an engine's LLM turn loop with a single invocation of a
vendor coding-harness CLI inside the task container. Several ends have to agree
on the same small set of facts — the image layer that installs the CLI, the
trial that invokes it, and the artifact that records what drove it — so all of
them read :data:`HARNESSES`.

The registry is data: :mod:`~tolokaforge_coding_harnesses._registry` loads
``data/harnesses.yaml`` at import, an operator overlay and any installed
:data:`HARNESS_REGISTRY_ENTRY_POINT_GROUP` plug-in bundle compose over it, and
:func:`harness_command` turns the result into a shell command. Nothing here
imports an engine: the adapter that consumes this package publishes the
assembled command on its own task description, and whoever runs the trial runs
whatever command it finds there.
"""

from __future__ import annotations

from ._registry import (
    ALTERNATIVE_GATEWAYS,
    CONFIG_TEMPLATE_VARIABLES,
    ENGINE_LOOP,
    HARNESS_REGISTRY_ENTRY_POINT_GROUP,
    HARNESSES,
    INSTALL_SCRIPT,
    MIDDLEWARE_PROXY_CONTAINER_PATH,
    MIDDLEWARE_PROXY_SCRIPT,
    OPENROUTER_PREFIX,
    PLUGIN_REGISTRY_RESOURCE,
    PROVIDER_ENV_INPUT_PREFIX,
    PROVIDER_ENV_KEYS,
    SHIPPED_REGISTRY_FILE,
    SHIPPED_REGISTRY_META_FILE,
    CredentialGateway,
    DuplicateRegistrationError,
    GatewayRoute,
    HarnessSpec,
    PluginBundle,
    PluginDiscovery,
    RequestMiddleware,
    ResolvedHarnessRegistry,
    RuntimeGateway,
    accepted_harnesses,
    discover_plugin_harness_registries,
    harness_command,
    harness_model,
    load_harness_registry,
    provider_env_input,
    resolve_effective_registry,
    validate_harness,
    validate_provider_env_keys,
)
from .container_injection import (
    ContainerFileInjector,
    ContainerInjectionError,
    DockerExecInjector,
    FileSpec,
)
from .fingerprint import HarnessFingerprint, compute_harness_fingerprint
from .path_resolvers import DEFAULT_PATH_RESOLVER, LinuxRootResolver
from .protocols import PATH_CONSTRUCT_PATTERN, PathResolver, SkillDelivery, SkillsBundle

__all__ = [
    "ALTERNATIVE_GATEWAYS",
    "CONFIG_TEMPLATE_VARIABLES",
    "DEFAULT_PATH_RESOLVER",
    "ENGINE_LOOP",
    "HARNESSES",
    "HARNESS_REGISTRY_ENTRY_POINT_GROUP",
    "INSTALL_SCRIPT",
    "MIDDLEWARE_PROXY_CONTAINER_PATH",
    "MIDDLEWARE_PROXY_SCRIPT",
    "OPENROUTER_PREFIX",
    "PATH_CONSTRUCT_PATTERN",
    "PLUGIN_REGISTRY_RESOURCE",
    "PROVIDER_ENV_INPUT_PREFIX",
    "PROVIDER_ENV_KEYS",
    "SHIPPED_REGISTRY_FILE",
    "SHIPPED_REGISTRY_META_FILE",
    "ContainerFileInjector",
    "ContainerInjectionError",
    "CredentialGateway",
    "DockerExecInjector",
    "DuplicateRegistrationError",
    "FileSpec",
    "GatewayRoute",
    "HarnessFingerprint",
    "HarnessSpec",
    "LinuxRootResolver",
    "PathResolver",
    "PluginBundle",
    "PluginDiscovery",
    "RequestMiddleware",
    "ResolvedHarnessRegistry",
    "RuntimeGateway",
    "SkillDelivery",
    "SkillsBundle",
    "accepted_harnesses",
    "compute_harness_fingerprint",
    "discover_plugin_harness_registries",
    "harness_command",
    "harness_model",
    "load_harness_registry",
    "provider_env_input",
    "resolve_effective_registry",
    "validate_harness",
    "validate_provider_env_keys",
]
