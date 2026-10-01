"""Run-configuration validator.

Validates ``RunConfig`` YAML files *before* a benchmark run starts,
catching common mistakes such as unsupported model parameters,
missing API keys, or schema violations.

Usage::

    from tolokaforge.core.config_validator import validate_run_config
    issues = validate_run_config(config_data)
    for issue in issues:
        print(issue)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from tolokaforge.core.llm.litellm_params import (
    OverlayKeyMismatchError,
    lookup_overlay,
    overlay_key_mismatches,
    overlay_stray_entries,
)
from tolokaforge.core.llm.openrouter_headers import is_openrouter_provider
from tolokaforge.core.llm.presets import (
    IGNORED_SAMPLING_PARAM,
    capability_override_errors,
    ignored_sampling_params,
    unclaimed_route_families,
)
from tolokaforge.core.llm.providers import litellm_model_id
from tolokaforge.core.llm.proxy import ProxyConfigError
from tolokaforge.core.llm.session_header import session_header_conflicts
from tolokaforge.core.models import (
    DOCKER_RUNTIME_ALIAS_TARGET,
    LEGACY_DOCKER_RUNTIME_ALIAS,
    RunConfig,
)
from tolokaforge.core.models.run_config import USER_TEMPERATURE_IGNORED
from tolokaforge.core.plugin_registry import available_agent_loops, available_runtime_backends

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Issue types
# ---------------------------------------------------------------------------


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass
class ValidationIssue:
    """A single validation finding."""

    severity: Severity
    path: str  # dotted config path, e.g. "models.agent.reasoning"
    message: str
    hint: str = ""

    def __str__(self) -> str:
        prefix = self.severity.value.upper()
        text = f"[{prefix}] {self.path}: {self.message}"
        if self.hint:
            text += f" (hint: {self.hint})"
        return text


@dataclass
class ValidationResult:
    """Aggregate validation outcome."""

    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.severity == Severity.ERROR for i in self.issues)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == Severity.WARNING]


# ---------------------------------------------------------------------------
# Knowledge about provider / model capabilities
# ---------------------------------------------------------------------------

# Providers whose OpenRouter-proxied models are *known* to accept the
# ``reasoning`` / ``reasoning_effort`` parameter.
_REASONING_SUPPORTED_PREFIXES: set[str] = {
    "anthropic/claude",
    "openai/o1",
    "openai/o3",
    "openai/o4",
    "openai/gpt-5",
    "deepseek/deepseek-reasoner",
    "google/gemini-2",
    "google/gemini-3",
    "qwen/qwen3",
    "moonshotai/kimi-k2",
}

# Provider keys expected in the environment per provider name.
_PROVIDER_ENV_KEYS: dict[str, list[str]] = {
    "openrouter": ["OPENROUTER_API_KEY", "OPENROUTER_API_KEYS"],
    "openai": ["OPENAI_API_KEY"],
    "anthropic": ["ANTHROPIC_API_KEY"],
    "nova": ["NOVA_API_KEY"],
}


def _model_supports_reasoning(model_name: str) -> bool | None:
    """Return True / False / None (unknown) for reasoning support."""
    lower = model_name.lower()
    for prefix in _REASONING_SUPPORTED_PREFIXES:
        if lower.startswith(prefix):
            return True
    # Explicitly unsupported families
    unsupported_patterns = [
        "minimax/",
        "meta-llama/",
        "mistral/",
        "cohere/",
    ]
    for pat in unsupported_patterns:
        if lower.startswith(pat):
            return False
    return None  # unknown – let the caller decide


def _model_supports_function_calling(model_name: str) -> bool | None:
    """Answer function-calling support for *model_name* from litellm's map.

    Returns ``True``/``False`` when litellm carries an entry for the key, and
    ``None`` when the key is absent from the map. ``litellm.get_model_info``
    is the seam that distinguishes the two: it raises for unmapped keys where
    ``litellm.supports_function_calling`` alone would return ``False`` for
    both "map has an entry that says False" and "map has no entry".
    """
    try:
        import litellm

        litellm.get_model_info(model=model_name)
    except Exception:
        return None
    try:
        return litellm.supports_function_calling(model=model_name)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Individual validators
# ---------------------------------------------------------------------------


def _function_calling_issues(base: str, provider: str, name: str) -> list[ValidationIssue]:
    """The agent's function-calling verdict, from litellm's map and the overlay."""
    try:
        overlay = lookup_overlay(provider, name)
    except OverlayKeyMismatchError:
        # Reported as an ERROR by the walk over every model config; a second
        # issue for the same entry would only restate it.
        return []
    fc_support = _model_supports_function_calling(litellm_model_id(provider, name))
    if fc_support is not True and "tools" in overlay.params:
        # An overlay entry answers the same question litellm's map cannot,
        # and this command already loads and schema-validates that block.
        # Reporting the model unable to call functions while the run works
        # is a preflight that contradicts the thing it is checking.
        #
        # `is not True` rather than `is False`: today an unmapped model
        # reads False, but the premise of this whole feature is that
        # litellm's answers move between patch releases, and a future
        # `None` would quietly stop consulting the declaration.
        fc_support = True
    if fc_support is False:
        severity = Severity.WARNING if is_openrouter_provider(provider) else Severity.ERROR
        return [
            ValidationIssue(
                severity=severity,
                path=f"{base}.name",
                message=f"Model {name!r} does not appear to support function calling (required for agent)",
                hint="Verify with your provider that the model supports tool use / function calling",
            )
        ]
    if fc_support is None:
        # Unmapped in litellm and undeclared in the overlay: the check
        # cannot answer either way. Surface an INFO with the exact overlay
        # entry to declare — silence would look like approval.
        return [
            ValidationIssue(
                severity=Severity.INFO,
                path=f"{base}.name",
                message=(
                    f"Model {name!r} is not in litellm's model map; "
                    "cannot confirm function-calling support"
                ),
                hint=(
                    "If the run needs tools, declare it in the presets overlay: "
                    f"litellm_models.{overlay.key} with supports_function_calling: true"
                ),
            )
        ]
    return []


def _overlay_key_issues(run_config: RunConfig) -> list[ValidationIssue]:
    """An ERROR per model config whose overlay entry sits under its raw name, and
    an INFO per model config whose raw name keys another config's entry."""
    refused = [
        ValidationIssue(severity=Severity.ERROR, path=f"{path}.name", message=str(err))
        for path, err in overlay_key_mismatches(run_config.models)
    ]
    stray = [
        ValidationIssue(
            severity=Severity.INFO,
            path=f"{path}.name",
            message=(
                f"litellm_models entry `{lookup.stray_key}` applies to provider "
                f"`{lookup.stray_provider}`, not to this config, which resolves `{lookup.key}`"
            ),
            hint=f"To admit parameters for this config, declare litellm_models.{lookup.key}",
        )
        for path, lookup in overlay_stray_entries(run_config.models)
    ]
    return refused + stray


def _session_header_issues(run_config: RunConfig) -> list[ValidationIssue]:
    """An ERROR per model config whose session header another header source also
    sets, judged against this environment's gateway variables."""
    try:
        conflicts = session_header_conflicts(run_config.models)
    except ProxyConfigError as err:
        return [ValidationIssue(severity=Severity.ERROR, path="(environment)", message=str(err))]
    return [
        ValidationIssue(severity=Severity.ERROR, path=err.path, message=err.reason)
        for _, err in conflicts
    ]


def _route_family_issues(run_config: RunConfig) -> list[ValidationIssue]:
    """A WARNING per model config whose route-prefixed name misses the preset its
    last segment matches."""
    return [
        ValidationIssue(
            severity=Severity.WARNING,
            path=f"{path}.name",
            message=(
                f"{finding.model_name!r} (provider {finding.provider!r}) resolves to the "
                f"'default' preset, but its last segment {finding.last_segment!r} matches "
                f"preset {finding.family!r}"
            ),
            hint=finding.remedy,
        )
        for path, finding in unclaimed_route_families(run_config.models)
    ]


def _capability_override_issues(run_config: RunConfig) -> list[ValidationIssue]:
    """An ERROR per model config, fallbacks included, whose ``capabilities`` block
    carries an unrecognised key."""
    return [
        ValidationIssue(severity=Severity.ERROR, path=err.path, message=err.reason)
        for _, err in capability_override_errors(run_config.models)
    ]


def _ignored_sampling_issues(run_config: RunConfig) -> list[ValidationIssue]:
    """A WARNING per explicit sampling value, fallbacks included, that the model's
    capabilities would not send, or the ERROR a preset or overlay conflict gives."""
    try:
        findings = ignored_sampling_params(run_config.models)
    except ValueError as err:
        return [ValidationIssue(severity=Severity.ERROR, path="(presets)", message=str(err))]
    return [
        ValidationIssue(
            severity=Severity.WARNING,
            path=path,
            message=(
                f"{IGNORED_SAMPLING_PARAM}: {finding.field} on {finding.model_name!r} "
                f"(provider {finding.provider!r})"
            ),
            hint=finding.remedy,
        )
        for path, finding in findings
    ]


def _validate_schema(raw: dict[str, Any]) -> RunConfig | ValidationIssue:
    """Parse *raw* into a ``RunConfig``, or the ERROR saying why it does not parse."""
    try:
        return RunConfig(**raw)
    except Exception as exc:
        return ValidationIssue(
            severity=Severity.ERROR,
            path="(root)",
            message=f"Schema validation failed: {exc}",
            hint="Check YAML structure against docs/CONFIG.md",
        )


def _validate_model(
    role: str,
    cfg: dict[str, Any],
) -> list[ValidationIssue]:
    """Validate a single model entry (``agent`` or ``user``)."""
    issues: list[ValidationIssue] = []
    base = f"models.{role}"

    provider = cfg.get("provider", "")
    name = cfg.get("name", "")

    # --- name format ---
    if not name:
        issues.append(
            ValidationIssue(
                severity=Severity.ERROR,
                path=f"{base}.name",
                message="Model name is empty",
            )
        )
        return issues

    # --- reasoning compatibility ---
    # ReasoningConfig must be a struct ({mode: ..., effort_hint: ..., ...}).
    # Legacy bare strings are rejected by ModelConfig validation, but we
    # produce a helpful INFO here for dict-sourced configs too.
    reasoning_raw = cfg.get("reasoning")
    reasoning_mode: str = "off"
    if isinstance(reasoning_raw, dict):
        reasoning_mode = str(reasoning_raw.get("mode", "off") or "off").lower()
    elif isinstance(reasoning_raw, str):
        issues.append(
            ValidationIssue(
                severity=Severity.ERROR,
                path=f"{base}.reasoning",
                message=(
                    f"reasoning must be a struct ({{mode, effort_hint, ...}}), "
                    f"got bare string {reasoning_raw!r}"
                ),
                hint="Migrate to {mode: adaptive, effort_hint: medium} — see docs/CONFIG.md",
            )
        )
    reasoning_enabled = reasoning_mode not in ("off", "")

    if reasoning_enabled:
        supported = _model_supports_reasoning(name)
        if supported is False:
            issues.append(
                ValidationIssue(
                    severity=Severity.WARNING,
                    path=f"{base}.reasoning",
                    message=(
                        f"reasoning mode={reasoning_mode!r} is set but model {name!r} "
                        f"is not known to support reasoning effort"
                    ),
                    hint="Set reasoning.mode=off for this model",
                )
            )
        elif supported is None:
            issues.append(
                ValidationIssue(
                    severity=Severity.INFO,
                    path=f"{base}.reasoning",
                    message=(
                        f"reasoning mode={reasoning_mode!r} is set; cannot confirm "
                        f"model {name!r} supports it"
                    ),
                    hint="Verify with your provider that the model supports reasoning_effort",
                )
            )

    # --- a user temperature nothing reads ---
    if role == "user" and "temperature" in cfg:
        issues.append(
            ValidationIssue(
                severity=Severity.WARNING,
                path=f"{base}.temperature",
                message=USER_TEMPERATURE_IGNORED,
                hint="Drop the key; a registered simulator takes its temperature from "
                "actors.user.simulator_config",
            )
        )

    # --- max_tokens sanity ---
    max_tokens = cfg.get("max_tokens")
    if max_tokens is not None and max_tokens > 128_000:
        issues.append(
            ValidationIssue(
                severity=Severity.WARNING,
                path=f"{base}.max_tokens",
                message=f"max_tokens={max_tokens} is unusually large",
                hint="Most models cap output at 4096-16384 tokens",
            )
        )

    if role == "agent" and provider:
        issues.extend(_function_calling_issues(base, provider, name))

    return issues


def _validate_api_keys(raw: dict[str, Any]) -> list[ValidationIssue]:
    """Check that expected API keys are present in the environment."""
    issues: list[ValidationIssue] = []
    models = raw.get("models", {})
    seen_providers: set[str] = set()

    for role, model_cfg in models.items():
        provider = (model_cfg.get("provider") or "").lower()
        if provider and provider not in seen_providers:
            seen_providers.add(provider)
            env_keys = _PROVIDER_ENV_KEYS.get(provider, [])
            if env_keys and not any(os.environ.get(k) for k in env_keys):
                issues.append(
                    ValidationIssue(
                        severity=Severity.WARNING,
                        path=f"models.{role}.provider",
                        message=(
                            f"Provider {provider!r} expects API key in "
                            f"{' or '.join(env_keys)}, but none is set"
                        ),
                        hint="Set the required environment variable or use scripts/with_env.sh",
                    )
                )

    return issues


def _validate_orchestrator(raw: dict[str, Any]) -> list[ValidationIssue]:
    """Validate orchestrator-level settings."""
    issues: list[ValidationIssue] = []
    orch = raw.get("orchestrator", {})

    workers = orch.get("workers", 8)
    if workers > 50:
        issues.append(
            ValidationIssue(
                severity=Severity.WARNING,
                path="orchestrator.workers",
                message=f"workers={workers} is very high; consider rate-limit implications",
            )
        )

    max_turns = orch.get("max_turns")
    if max_turns is not None and max_turns > 100:
        issues.append(
            ValidationIssue(
                severity=Severity.WARNING,
                path="orchestrator.max_turns",
                message=f"max_turns={max_turns} is very high; episodes may be expensive",
            )
        )

    runtime = orch.get("runtime")
    if runtime is not None:
        # ``docker`` is a legacy alias for ``shared`` resolved before any
        # registry lookup (the registry has no ``docker`` name); coerce it
        # here so a still-supported ``runtime: docker`` config validates.
        if runtime == LEGACY_DOCKER_RUNTIME_ALIAS:
            runtime = DOCKER_RUNTIME_ALIAS_TARGET
        known = available_runtime_backends()
        if runtime not in known:
            issues.append(
                ValidationIssue(
                    severity=Severity.ERROR,
                    path="orchestrator.runtime",
                    message=(
                        f"Unknown runtime backend {orch['runtime']!r}. "
                        f"Registered backends: {', '.join(known)}."
                    ),
                )
            )

    agent_loop = orch.get("agent_loop")
    if agent_loop is not None:
        registered = available_agent_loops()
        if agent_loop not in registered:
            issues.append(
                ValidationIssue(
                    severity=Severity.ERROR,
                    path="orchestrator.agent_loop",
                    message=(
                        f"Unknown agent loop {agent_loop!r}. "
                        f"Registered loops: {', '.join(registered)}."
                    ),
                    hint=(
                        "Install the package that registers the loop under the "
                        "tolokaforge.agent_loops entry-point group, or use the "
                        "built-in 'engine-loop'"
                    ),
                )
            )

    return issues


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_run_config(raw: dict[str, Any]) -> ValidationResult:
    """Validate a raw (parsed-YAML) run configuration dict.

    Returns a ``ValidationResult`` with all findings.
    """
    result = ValidationResult()

    # 1. Schema validation (must pass for further checks)
    run_config = _validate_schema(raw)
    if isinstance(run_config, ValidationIssue):
        result.issues.append(run_config)
        return result

    # 2. Overlay entries stored under a raw name, and route-prefixed names that
    #    miss their last segment's preset, for every model and fallback
    result.issues.extend(_overlay_key_issues(run_config))
    result.issues.extend(_route_family_issues(run_config))
    result.issues.extend(_capability_override_issues(run_config))
    result.issues.extend(_ignored_sampling_issues(run_config))
    result.issues.extend(_session_header_issues(run_config))

    # 3. Per-model checks
    models = raw.get("models", {})
    for role, model_cfg in models.items():
        if isinstance(model_cfg, dict):
            result.issues.extend(_validate_model(role, model_cfg))

    # 4. API key presence
    result.issues.extend(_validate_api_keys(raw))

    # 5. Orchestrator checks
    result.issues.extend(_validate_orchestrator(raw))

    return result
