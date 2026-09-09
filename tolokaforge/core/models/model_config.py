"""ModelConfig wire type + provider routing knobs.

Holds the LLM invocation config that flows across the trial spec wire:
per-provider identity (name / provider), sampling parameters,
:class:`ReasoningConfig`, an :class:`OpenRouterConfig` when the model is
routed via OpenRouter, and an ordered ``fallbacks`` chain a client falls
through on hard failure.
"""

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from tolokaforge.core.llm.reasoning import ReasoningConfig

__all__ = ["ModelConfig", "OpenRouterConfig"]


class OpenRouterConfig(BaseModel):
    """OpenRouter provider-routing knobs (https://openrouter.ai/docs/features/provider-routing).

    ``provider_order`` lists case-sensitive OpenRouter provider slugs in priority
    order, e.g. ``["Together"]`` or ``["DeepInfra", "Nebius"]``. With
    ``allow_fallbacks=False`` the request is restricted to those providers, which
    is how a model pins around a rate-limited default provider.
    """

    model_config = {"extra": "ignore"}

    provider_order: list[str] | None = None
    allow_fallbacks: bool = True


class ModelConfig(BaseModel):
    """LLM model configuration"""

    model_config = {"extra": "ignore"}

    provider: str
    name: str
    temperature: float = 0.0
    max_tokens: int | None = None
    seed: int | None = None
    # Coding-harness selector. When set, the trial's LLM loop is replaced by a
    # single invocation of the named vendor CLI (``claude-code``, ``codex``,
    # ``gemini-cli``, ``kimi-code``, ``opencode``, ``grok-build`` — see the
    # ``tolokaforge_coding_harnesses`` package for the shipped registry) inside
    # the trial container. Adapter-agnostic: any adapter whose
    # ``supports_coding_harness`` capability flag is ``True`` accepts this
    # field. Adapter identity is not checked here; the orchestrator's config
    # gate refuses the combination when the resolved adapter does not opt in.
    harness: str | None = None
    # Reasoning / thinking configuration. Must be a struct form —
    # bare strings (``reasoning: medium``) are rejected with a migration
    # pointer. See docs/CONFIG.md § reasoning for the schema.
    reasoning: ReasoningConfig = Field(default_factory=ReasoningConfig)
    top_p: float | None = None  # Nucleus sampling parameter (0.0-1.0)
    capabilities: dict[str, Any] | None = None  # Override auto-detected model capabilities
    # OpenRouter-only provider routing; rejected for other providers by the validator below.
    openrouter: OpenRouterConfig | None = None
    # Ordered fallback chain. When a hard failure hits the primary
    # model, subsequent turns for the affected trial use the next entry
    # in this list. Empty list (default) → no fallback wrapper. See
    # docs/CONFIG.md § Fallback models.
    fallbacks: list["ModelConfig"] = Field(default_factory=list)

    @model_validator(mode="after")
    def _reject_openrouter_on_other_providers(self) -> "ModelConfig":
        if self.openrouter is not None and not self.provider.startswith("openrouter"):
            raise ValueError(
                f"`openrouter:` routing is only valid for openrouter models, "
                f"but provider is {self.provider!r}."
            )
        return self

    @field_validator("reasoning", mode="before")
    @classmethod
    def _validate_reasoning(cls, value: Any) -> Any:
        if value is None:
            return ReasoningConfig()
        if isinstance(value, ReasoningConfig):
            return value
        if isinstance(value, str):
            raise ValueError(
                f"`reasoning:` must be a struct ({{mode: ..., budget_tokens: ...}}), "
                f"not the bare string {value!r}. See docs/CONFIG.md."
            )
        if isinstance(value, dict):
            return ReasoningConfig(**value)
        raise TypeError(
            f"`reasoning:` must be ReasoningConfig | dict | None, got {type(value).__name__}"
        )
