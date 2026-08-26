"""ModelConfig wire type + provider routing knobs.

Holds the LLM invocation config that flows across the trial spec wire:
per-provider identity (name / provider), sampling parameters,
:class:`ReasoningConfig`, an :class:`OpenRouterConfig` when the model is
routed via OpenRouter, and an ordered ``fallbacks`` chain a client falls
through on hard failure.
"""

import warnings
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
    # Coding-harness selector. When set, the orchestrator selects a
    # CodingHarnessDriver (see tolokaforge.core.drivers.coding_harness)
    # that replaces the trial's LLM loop with a single invocation of the
    # named vendor CLI (``claude-code``, ``codex``, ``gemini-cli``,
    # ``kimi-code``, ``opencode``, ``grok-build`` — see the
    # ``tolokaforge_coding_harnesses`` package for the shipped registry)
    # inside the trial container. Adapter-agnostic: the driver applies
    # around whichever adapter's output, and its ``attach()`` call refuses
    # the combination when the resolved adapter does not stage a per-task
    # container the driver can layer onto.
    #
    # Named ``coding_harness`` (not just ``harness``) because tolokaforge is
    # itself a benchmarking harness — the qualifier disambiguates the CLI
    # scaffolding from the harness the CLI runs inside.
    #
    # Two shapes accepted:
    #   * ``"claude-code"`` — the shipped registry's pinned version is used
    #     (reproducibility-first; recommended for scored runs).
    #   * ``"claude-code@2.2.0"`` — the version segment overrides the shipped
    #     pin at install time. A pre-validator splits the slug at ``@`` and
    #     populates ``coding_harness_version``; the field itself carries only
    #     the name after parse. Meant for ad-hoc "try this CLI release" runs;
    #     the recorded trial artefact reflects the override so replay is
    #     honest.
    coding_harness: str | None = None
    # Pinned CLI version override, populated either by writing this field
    # directly or by using the ``name@version`` slug shape on ``coding_harness``.
    # ``None`` (the default) means the shipped registry's pin governs. A
    # non-None value passes through to ``install-harness.sh`` as the version
    # arg and lands verbatim on ``HarnessSpec.version`` for the resolved spec.
    coding_harness_version: str | None = None
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
    def _lift_and_split_coding_harness(cls, values: Any) -> Any:
        """Two transforms on the coding-harness fields at parse time.

        **Legacy-name lift.** ``models.agent.harness`` / ``harness_version``
        were the field names in the pre-rename shape. Callers who wrote them
        get a ``DeprecationWarning`` naming the new location and their value
        moves into ``coding_harness`` / ``coding_harness_version``. Collision
        policy: equal values warn once, differing values raise ``ValueError``
        naming both keys.

        **Slug split.** ``coding_harness: "<name>@<version>"`` splits into a
        bare name on ``coding_harness`` and a version on
        ``coding_harness_version``. Downstream code (adapters, orchestrator,
        registry lookup) never sees the ``@`` syntax. Empty name (``@2.2.0``)
        and empty version (``claude-code@``) raise. Setting the slug's
        ``@version`` and ``coding_harness_version`` to different values
        raises naming both.
        """
        if not isinstance(values, dict):
            return values
        # ---- Legacy-name lift: ``harness`` → ``coding_harness`` ----
        legacy_name = values.get("harness")
        if legacy_name is not None:
            canonical_name = values.get("coding_harness")
            if canonical_name is not None and canonical_name != legacy_name:
                raise ValueError(
                    f"models.agent.harness={legacy_name!r} conflicts with "
                    f"models.agent.coding_harness={canonical_name!r}; "
                    "drop `harness` (deprecated) and keep `coding_harness`."
                )
            values = dict(values)
            if canonical_name is None:
                values["coding_harness"] = legacy_name
            del values["harness"]
            warnings.warn(
                "models.agent.harness is deprecated; use "
                "models.agent.coding_harness instead. Tolokaforge is itself "
                "a benchmarking harness, so `coding_harness` disambiguates "
                "the CLI scaffolding from the harness the CLI runs inside. "
                "Legacy field will be removed in a future release.",
                DeprecationWarning,
                stacklevel=4,
            )
        legacy_version = values.get("harness_version")
        if legacy_version is not None:
            canonical_version = values.get("coding_harness_version")
            if canonical_version is not None and canonical_version != legacy_version:
                raise ValueError(
                    f"models.agent.harness_version={legacy_version!r} conflicts "
                    f"with models.agent.coding_harness_version="
                    f"{canonical_version!r}; drop `harness_version` "
                    "(deprecated) and keep `coding_harness_version`."
                )
            values = dict(values)
            if canonical_version is None:
                values["coding_harness_version"] = legacy_version
            del values["harness_version"]
            warnings.warn(
                "models.agent.harness_version is deprecated; use "
                "models.agent.coding_harness_version instead. Legacy field "
                "will be removed in a future release.",
                DeprecationWarning,
                stacklevel=4,
            )

        # ---- Slug split on the canonical field ----
        coding_harness = values.get("coding_harness")
        if not isinstance(coding_harness, str) or "@" not in coding_harness:
            return values
        name, _sep, version = coding_harness.partition("@")
        if not name:
            raise ValueError(
                f"models.agent.coding_harness={coding_harness!r}: empty name "
                "before '@'. Write the CLI name (e.g. 'claude-code@2.2.0') or "
                "drop the '@'."
            )
        if not version:
            raise ValueError(
                f"models.agent.coding_harness={coding_harness!r}: empty version "
                "after '@'. Write a concrete version (e.g. 'claude-code@2.2.0') "
                "or drop the '@' to use the shipped pin."
            )
        existing_version = values.get("coding_harness_version")
        if existing_version is not None and existing_version != version:
            raise ValueError(
                f"models.agent.coding_harness={coding_harness!r} version segment "
                f"({version!r}) conflicts with coding_harness_version="
                f"{existing_version!r}; pick one location."
            )
        values = dict(values)
        values["coding_harness"] = name
        values["coding_harness_version"] = version
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
