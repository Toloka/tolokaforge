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
    #
    # Two shapes accepted:
    #   * ``"claude-code"`` — the shipped registry's pinned version is used
    #     (reproducibility-first; recommended for scored runs).
    #   * ``"claude-code@2.2.0"`` — the version segment overrides the shipped
    #     pin at install time. A pre-validator splits the slug at ``@`` and
    #     populates ``harness_version``; the field itself carries only the
    #     name after parse. Meant for ad-hoc "try this CLI release" runs; the
    #     recorded trial artefact reflects the override so replay is honest.
    harness: str | None = None
    # Pinned CLI version override, populated either by writing this field
    # directly or by using the ``name@version`` slug shape on ``harness``.
    # ``None`` (the default) means the shipped registry's pin governs. A
    # non-None value passes through to ``install-harness.sh`` as the version
    # arg and lands verbatim on ``HarnessSpec.version`` for the resolved spec.
    harness_version: str | None = None
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

    @model_validator(mode="before")
    @classmethod
    def _split_harness_slug(cls, values: Any) -> Any:
        """Split ``harness: "<name>@<version>"`` into name + version.

        Accepts the two documented shapes on ``harness``: a bare name that
        defers to the shipped registry's pin, or a ``name@version`` slug that
        overrides the pin at install time. When a slug is provided the pre-
        validator populates ``harness_version`` and rewrites ``harness`` to
        just the name so downstream code (adapters, orchestrator, registry
        lookup) never has to know about the syntax.

        Collision policy: writing the version through the slug AND through
        the field is a hard error naming both values. Empty name (``@2.2``)
        and empty version (``claude-code@``) are hard errors — an empty
        segment is almost always a typo.
        """
        if not isinstance(values, dict):
            return values
        harness = values.get("harness")
        if not isinstance(harness, str) or "@" not in harness:
            return values
        name, sep, version = harness.partition("@")
        if not name:
            raise ValueError(
                f"models.agent.harness={harness!r}: empty name before '@'. "
                "Write the CLI name (e.g. 'claude-code@2.2.0') or drop the '@'."
            )
        if not version:
            raise ValueError(
                f"models.agent.harness={harness!r}: empty version after '@'. "
                "Write a concrete version (e.g. 'claude-code@2.2.0') or drop "
                "the '@' to use the shipped pin."
            )
        existing_version = values.get("harness_version")
        if existing_version is not None and existing_version != version:
            raise ValueError(
                f"models.agent.harness={harness!r} version segment "
                f"({version!r}) conflicts with harness_version="
                f"{existing_version!r}; pick one location."
            )
        values = dict(values)
        values["harness"] = name
        values["harness_version"] = version
        return values

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
