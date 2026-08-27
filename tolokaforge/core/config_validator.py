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

from tolokaforge.core.models import (
    DOCKER_RUNTIME_ALIAS_TARGET,
    LEGACY_DOCKER_RUNTIME_ALIAS,
    RunConfig,
)
from tolokaforge.core.plugin_registry import available_runtime_backends

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


def _declared_function_calling(name: str, provider: str) -> bool:
    """Whether an operator overlay admits tool calls for this model.

    Asked through the same function the RUN asks, so the preflight cannot
    disagree with it about which entry applies - a second lookup here would
    have its own idea of how to build the key.
    """
    from tolokaforge.core.llm.litellm_params import allowed_openai_params

    try:
        return "tools" in allowed_openai_params(name, provider)
    except (OSError, ValueError):
        # A broken overlay has its own, louder error path at load; this check
        # must not turn it into a confusing function-calling verdict.
        return False


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


def _validate_schema(raw: dict[str, Any]) -> list[ValidationIssue]:
    """Validate that *raw* parses into a valid ``RunConfig``."""
    issues: list[ValidationIssue] = []
    try:
        RunConfig(**raw)
    except Exception as exc:
        issues.append(
            ValidationIssue(
                severity=Severity.ERROR,
                path="(root)",
                message=f"Schema validation failed: {exc}",
                hint="Check YAML structure against docs/CONFIG.md",
            )
        )
    return issues


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

    # --- temperature with reasoning ---
    temperature = cfg.get("temperature")
    if reasoning_enabled and temperature is not None and temperature > 0:
        # Some reasoning models ignore or reject non-zero temperature
        lower_name = name.lower()
        if any(lower_name.startswith(p) for p in ("openai/o1", "openai/o3")):
            issues.append(
                ValidationIssue(
                    severity=Severity.WARNING,
                    path=f"{base}.temperature",
                    message=(
                        f"temperature={temperature} with reasoning model {name!r}; "
                        "OpenAI o-series models may ignore or reject non-zero temperature"
                    ),
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

    # --- function calling (agent only) ---
    if role == "agent" and provider:
        litellm_name = f"{provider}/{name}" if not name.startswith(f"{provider}/") else name
        fc_support = _model_supports_function_calling(litellm_name)
        if fc_support is not True and _declared_function_calling(name, provider):
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
            severity = (
                Severity.WARNING if provider.lower().startswith("openrouter") else Severity.ERROR
            )
            issues.append(
                ValidationIssue(
                    severity=severity,
                    path=f"{base}.name",
                    message=f"Model {name!r} does not appear to support function calling (required for agent)",
                    hint="Verify with your provider that the model supports tool use / function calling",
                )
            )
        elif fc_support is None:
            # Unmapped in litellm and undeclared in the overlay: the check
            # cannot answer either way. Surface an INFO with the exact overlay
            # entry to declare — silence would look like approval.
            issues.append(
                ValidationIssue(
                    severity=Severity.INFO,
                    path=f"{base}.name",
                    message=(
                        f"Model {name!r} is not in litellm's model map; "
                        "cannot confirm function-calling support"
                    ),
                    hint=(
                        "If the run needs tools, declare it in the presets overlay: "
                        f"litellm_models.{provider}/{name} with supports_function_calling: true"
                    ),
                )
            )

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
    schema_issues = _validate_schema(raw)
    result.issues.extend(schema_issues)
    if any(i.severity == Severity.ERROR for i in schema_issues):
        return result

    # 2. Per-model checks
    models = raw.get("models", {})
    for role, model_cfg in models.items():
        if isinstance(model_cfg, dict):
            result.issues.extend(_validate_model(role, model_cfg))

    # 3. API key presence
    result.issues.extend(_validate_api_keys(raw))

    # 4. Orchestrator checks
    result.issues.extend(_validate_orchestrator(raw))

    return result
