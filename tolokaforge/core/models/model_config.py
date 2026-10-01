"""ModelConfig wire type + provider routing knobs.

Holds the LLM invocation config that flows across the trial spec wire:
per-provider identity (name / provider), sampling parameters,
:class:`ReasoningConfig`, an :class:`OpenRouterConfig` when the model is
routed via OpenRouter, a :class:`ModelSessionConfig` naming a per-conversation
session header, and an ordered ``fallbacks`` chain a client falls through on
hard failure.
"""

import dataclasses
import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tolokaforge.core.llm.reasoning import ReasoningConfig
from tolokaforge.core.unknown_keys import refuse_undeclared_keys

__all__ = ["RESOLVED_RECORD_KEY", "ModelConfig", "ModelSessionConfig", "OpenRouterConfig"]

#: The key the conductor adds to each ``task.yaml`` ``model_config.<role>`` block for the
#: preset fingerprint. It is the record, not a field: a reader rebuilding a
#: :class:`ModelConfig` from that block drops it.
RESOLVED_RECORD_KEY = "resolved"

_REASONING_FIELDS = tuple(field.name for field in dataclasses.fields(ReasoningConfig))

#: RFC 9110 ``field-name`` (a ``token``).
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")

#: Transport headers litellm or the engine sets; a session value must not replace them.
_RESERVED_SESSION_HEADERS = frozenset({"authorization", "content-type", "content-length", "host"})


class _RefusesUndeclaredKeys(BaseModel):
    """Answers an undeclared key with :func:`refuse_undeclared_keys` before ``extra="forbid"`` can."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _refuse_undeclared_keys(cls, data: Any) -> Any:
        if isinstance(data, Mapping):
            refuse_undeclared_keys(data, tuple(cls.model_fields), owner=cls.__name__)
        return data


class ModelSessionConfig(_RefusesUndeclaredKeys):
    """The request header that carries this model's conversation id (docs/CONFIG.md)."""

    model_config = ConfigDict(frozen=True)

    header: str

    @field_validator("header")
    @classmethod
    def _validate_header(cls, value: str) -> str:
        if not _HEADER_NAME.fullmatch(value):
            raise ValueError(
                f"session.header {value!r} is not an HTTP header name: use letters, digits "
                f"and !#$%&'*+.^_`|~- only, no spaces or colons."
            )
        if value.lower() in _RESERVED_SESSION_HEADERS:
            raise ValueError(
                f"session.header {value!r} names a transport header the engine or litellm "
                f"sets ({', '.join(sorted(_RESERVED_SESSION_HEADERS))}); pick another name."
            )
        return value


class OpenRouterConfig(_RefusesUndeclaredKeys):
    """OpenRouter provider-routing knobs (https://openrouter.ai/docs/features/provider-routing).

    ``provider_order`` lists case-sensitive OpenRouter provider slugs in priority
    order, e.g. ``["Together"]`` or ``["DeepInfra", "Nebius"]``. With
    ``allow_fallbacks=False`` the request is restricted to those providers, which
    is how a model pins around a rate-limited default provider.
    """

    provider_order: list[str] | None = None
    allow_fallbacks: bool = True


class ModelConfig(_RefusesUndeclaredKeys):
    """LLM model configuration"""

    provider: str
    name: str
    # ``None`` sends no ``temperature``, so the provider's default applies; a
    # preset's ``fixed_temperature`` still overrides either. The built-in user
    # simulator does not read ``models.user.temperature`` (see
    # ``tolokaforge.core.models.run_config.sets_user_temperature``).
    temperature: float | None = 0.0
    max_tokens: int | None = None
    seed: int | None = None
    # Suppress the provider's parallel tool-call behavior: when ``False``
    # the engine sends ``parallel_tool_calls=False`` on requests that also
    # carry ``tools``, requesting a single tool call per turn (some
    # analysis-agent workflows depend on serial tool calls). ``None`` is
    # the default and omits the parameter, letting the provider's default
    # apply. The overlay's ``supports_function_calling`` capability admits
    # this parameter alongside ``tools`` and ``tool_choice``.
    #
    # No-op on tools-less requests: the parameter is only meaningful when
    # the same request carries ``tools``, so a rubric-judge or
    # completions-only call omits ``parallel_tool_calls`` regardless of
    # what this field is set to.
    parallel_tool_calls: bool | None = None
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
    # Not inherited by ``fallbacks`` entries: each declares its own or sends none.
    session: ModelSessionConfig | None = None
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
        if isinstance(value, Mapping):
            refuse_undeclared_keys(value, _REASONING_FIELDS, owner=ReasoningConfig.__name__)
            return ReasoningConfig(**value)
        raise TypeError(
            f"`reasoning:` must be ReasoningConfig | dict | None, got {type(value).__name__}"
        )
