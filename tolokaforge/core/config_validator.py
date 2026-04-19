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
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from tolokaforge.core.models import RunConfig

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
    "deepseek/deepseek-reasoner",
    "google/gemini-2",
    "google/gemini-3",
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
    """Best-effort check via LiteLLM, returns None on failure."""
    try:
        import litellm

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
    reasoning = cfg.get("reasoning", "off")
    if reasoning and reasoning.lower() not in ("off", ""):
        supported = _model_supports_reasoning(name)
        if supported is False:
            issues.append(
                ValidationIssue(
                    severity=Severity.WARNING,
                    path=f"{base}.reasoning",
                    message=(
                        f"reasoning={reasoning!r} is set but model {name!r} "
                        f"is not known to support reasoning effort"
                    ),
                    hint="Remove 'reasoning' or set to 'off' for this model",
                )
            )
        elif supported is None:
            issues.append(
                ValidationIssue(
                    severity=Severity.INFO,
                    path=f"{base}.reasoning",
                    message=(
                        f"reasoning={reasoning!r} is set; cannot confirm model {name!r} supports it"
                    ),
                    hint="Verify with your provider that the model supports reasoning_effort",
                )
            )

    # --- temperature with reasoning ---
    temperature = cfg.get("temperature")
    if (
        reasoning
        and reasoning.lower() not in ("off", "")
        and temperature is not None
        and temperature > 0
    ):
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
        if fc_support is False:
            # For OpenRouter, LiteLLM may not recognise the model (future /
            # niche models).  Downgrade to WARNING so we don't block runs for
            # models that LiteLLM simply hasn't catalogued yet.
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

    return issues


def _validate_api_keys(raw: dict[str, Any]) -> list[ValidationIssue]:
    """Check that expected API keys are present in the environment."""
    from tolokaforge.secrets import get_default

    sm = get_default()
    issues: list[ValidationIssue] = []
    models = raw.get("models", {})
    seen_providers: set[str] = set()

    for role, model_cfg in models.items():
        provider = (model_cfg.get("provider") or "").lower()
        if provider and provider not in seen_providers:
            seen_providers.add(provider)
            env_keys = _PROVIDER_ENV_KEYS.get(provider, [])
            if env_keys and not any(sm.has_secret(k) for k in env_keys):
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
