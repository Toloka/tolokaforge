"""Declarative preset loader — YAML → :class:`ModelCapabilities`.

Presets live in
[`tolokaforge/core/data/model_presets.yaml`](../../data/model_presets.yaml)
and are composed from a default block, per-model matches, and a per-provider
overlay. See [`docs/LLM_LAYER.md`](../../../docs/LLM_LAYER.md) for the full
resolution order.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from tolokaforge.core.llm.cache_policy import (
    AnthropicEphemeralCache,
    CachePolicy,
    NoCache,
)
from tolokaforge.core.llm.capabilities import ModelCapabilities
from tolokaforge.core.llm.content_policy import (
    AnthropicContent,
    NovaContent,
    OpenAIContent,
    ToolContentPolicy,
)
from tolokaforge.core.llm.params_policy import GenerationParams
from tolokaforge.core.llm.prompt_policy import (
    DictMapHints,
    NoPromptEnrichment,
    SystemPromptPolicy,
)
from tolokaforge.core.llm.reasoning_codec import (
    AnthropicReasoningCodec,
    GeminiReasoningCodec,
    NoReasoningCodec,
    OpenAIReasoningCodec,
    ReasoningCodec,
)
from tolokaforge.core.llm.response_policy import (
    ArrayDictMapResponse,
    JsonCoerceResponse,
    MinimaxM3TagRecoveryResponse,
    ResponsePolicy,
    StandardResponse,
    UnwrapInputResponse,
)
from tolokaforge.core.llm.schema_sanitizer import (
    GeminiSchema,
    PassthroughSchema,
    StrictSchema,
    ToolSchemaSanitizer,
)

__all__ = [
    "build_capabilities",
    "resolve_policy_names",
    "resolve_effective_preset",
]


# ---------------------------------------------------------------------------
# Policy-name → class registries
# ---------------------------------------------------------------------------

_SCHEMA_SANITIZERS: dict[str, type[ToolSchemaSanitizer]] = {
    "passthrough": PassthroughSchema,
    "strict": StrictSchema,
    "gemini": GeminiSchema,
}

_PROMPT_POLICIES: dict[str, type[SystemPromptPolicy]] = {
    "none": NoPromptEnrichment,
    "dict_map_hints": DictMapHints,
}

_CONTENT_POLICIES: dict[str, type[ToolContentPolicy]] = {
    "openai": OpenAIContent,
    "anthropic": AnthropicContent,
    "nova": NovaContent,
}

_RESPONSE_POLICIES: dict[str, type[ResponsePolicy]] = {
    "standard": StandardResponse,
    "unwrap_input": UnwrapInputResponse,
    "json_coerce": JsonCoerceResponse,
    "array_dict_map": ArrayDictMapResponse,
    "minimax_m3_tags": MinimaxM3TagRecoveryResponse,
}

_REASONING_CODECS: dict[str, type[ReasoningCodec]] = {
    "none": NoReasoningCodec,
    "anthropic": AnthropicReasoningCodec,
    "openai": OpenAIReasoningCodec,
    "gemini": GeminiReasoningCodec,
}

_CACHE_POLICIES: dict[str, type[CachePolicy]] = {
    "none": NoCache,
    "anthropic_ephemeral": AnthropicEphemeralCache,
}


_DEFAULT_PRESET_DATA: dict[str, Any] = {"default": {}, "presets": {}, "providers": {}}


@lru_cache(maxsize=1)
def _load_presets() -> dict[str, Any]:
    """Load model capability presets from YAML. Cached after first call."""
    preset_path = Path(__file__).parent.parent / "data" / "model_presets.yaml"
    if not preset_path.exists():
        return _DEFAULT_PRESET_DATA
    with open(preset_path) as f:
        return yaml.safe_load(f) or _DEFAULT_PRESET_DATA


def _iter_preset_matches(
    model_name: str,
    provider: str,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(preset_name, preset_dict)`` pairs whose match globs match.

    Iterates over every preset whose ``match`` / ``match_provider`` globs match
    ``(model_name, provider)``. Iteration order follows YAML declaration order
    — callers that want first-match-wins semantics should ``break`` / ``return``
    after the first yield.

    Shared by :func:`_match_preset` (merged-config lookup) and
    :func:`resolve_effective_preset` (name-only lookup) so that the two paths
    cannot drift.
    """
    data = _load_presets()
    name_lower = model_name.lower()
    provider_lower = (provider or "").lower()
    for preset_name, preset in data.get("presets", {}).items():
        patterns = preset.get("match", [])
        provider_patterns = preset.get("match_provider", [])
        matched = any(fnmatch.fnmatch(name_lower, p) for p in patterns)
        if not matched and provider_patterns:
            matched = any(fnmatch.fnmatch(provider_lower, p) for p in provider_patterns)
        if matched:
            yield preset_name, preset


def _match_preset(model_name: str, provider: str) -> dict[str, Any]:
    """Find the first matching model preset.

    Returns merged config: default → matched preset → provider override.
    """
    data = _load_presets()
    result: dict[str, Any] = dict(data.get("default", {}))

    # Match model preset — first match wins
    for _preset_name, preset in _iter_preset_matches(model_name, provider):
        for key, value in preset.items():
            if key in ("match", "match_provider"):
                continue
            if key == "params" and "params" in result:
                result["params"] = {**result["params"], **value}
            else:
                result[key] = value
        break

    # Apply provider overlay
    prov_lower = provider.lower().split("/")[0]  # "openrouter/google" → "openrouter"
    provider_cfg = data.get("providers", {}).get(prov_lower, {})
    for key, value in provider_cfg.items():
        if key == "params" and "params" in result:
            result["params"] = {**result["params"], **value}
        else:
            result[key] = value

    return result


#: Every override key recognised by :func:`_apply_config_overrides`.
#: Keep synchronised with the body below — the unit-test
#: ``test_recognised_keys_are_the_documented_set`` asserts every key in this
#: set appears as a literal inside the function body. Adding a new override
#: key means (a) updating this set, (b) extending the conditional logic.
_RECOGNISED_OVERRIDE_KEYS: frozenset[str] = frozenset(
    {
        # Prompt policy
        "dict_map_prompt_hints",
        # Schema sanitizer
        "supports_schema_extras",
        "supports_typed_dict_maps",
        # Response policy
        "unwrap_input_key",
        # Content policy
        "tool_content_format",
        "supports_tool_images",
        # Reasoning codec
        "gemini_drop_placeholder_signature",
        # Params policy
        "fixed_temperature",
        "supports_seed",
        "reasoning_via_extra_body",
        "reasoning_via_thinking_kwarg",
        "drop_sampling_when_thinking",
        "reasoning_budget_default",
    }
)


def _apply_config_overrides(cfg: dict[str, Any], overrides: dict[str, Any]) -> None:
    """Translate run-config override keys into preset-format fields.

    Mutates *cfg* in place.

    Raises
    ------
    ValueError
        If any key in *overrides* is not listed in
        :data:`_RECOGNISED_OVERRIDE_KEYS`. Typos in run-config YAML must
        surface loudly rather than silently no-op, per AGENTS.md rule #1
        ("Surface failures explicitly"). See ``docs/CONFIG.md`` §
        ``ModelConfig.capabilities`` for the current contract.
    """
    unknown = set(overrides) - _RECOGNISED_OVERRIDE_KEYS
    if unknown:
        raise ValueError(
            f"Unknown capability override keys: {sorted(unknown)}. "
            f"Recognised keys: {sorted(_RECOGNISED_OVERRIDE_KEYS)}. "
            f"See docs/CONFIG.md § ModelConfig.capabilities for the contract."
        )

    # dict_map_prompt_hints → prompt_policy
    if overrides.get("dict_map_prompt_hints"):
        cfg["prompt_policy"] = "dict_map_hints"

    # Schema strictness overrides
    if (
        overrides.get("supports_schema_extras") is False
        or overrides.get("supports_typed_dict_maps") is False
    ):
        if cfg.get("schema_sanitizer", "passthrough") == "passthrough":
            cfg["schema_sanitizer"] = "strict"

    # Response wrapping
    if overrides.get("unwrap_input_key"):
        cfg["response_policy"] = "unwrap_input"

    # Content format overrides
    fmt = overrides.get("tool_content_format")
    if fmt in ("anthropic", "openai"):
        cfg["content_policy"] = fmt

    if "supports_tool_images" in overrides:
        current = cfg.get("content_policy", "openai")
        if overrides["supports_tool_images"] and current != "anthropic":
            cfg["content_policy"] = "anthropic"
        elif not overrides["supports_tool_images"] and current != "openai":
            cfg["content_policy"] = "openai"

    # Reasoning-codec-level overrides land at the top of *cfg* so
    # ``build_capabilities`` can hand them to the codec constructor
    # (``_REASONING_CODECS[name](**codec_kwargs)``). Currently only the
    # Gemini codec consumes a kwarg here; future codecs can extend the
    # pattern without touching the dispatch table.
    if "gemini_drop_placeholder_signature" in overrides:
        cfg["gemini_drop_placeholder_signature"] = overrides["gemini_drop_placeholder_signature"]

    # Params-level overrides — every key below lands on ``GenerationParams``
    # (see tolokaforge/core/llm/params_policy.py).
    params = cfg.setdefault("params", {})
    if "fixed_temperature" in overrides:
        params["fixed_temperature"] = overrides["fixed_temperature"]
    if "supports_seed" in overrides:
        params["supports_seed"] = overrides["supports_seed"]
    if "reasoning_via_extra_body" in overrides:
        params["reasoning_via_extra_body"] = overrides["reasoning_via_extra_body"]
    if "reasoning_via_thinking_kwarg" in overrides:
        params["reasoning_via_thinking_kwarg"] = overrides["reasoning_via_thinking_kwarg"]
    if "drop_sampling_when_thinking" in overrides:
        params["drop_sampling_when_thinking"] = overrides["drop_sampling_when_thinking"]
    if "reasoning_budget_default" in overrides:
        params["reasoning_budget_default"] = overrides["reasoning_budget_default"]


def build_capabilities(
    model_name: str,
    provider: str = "",
    overrides: dict[str, Any] | None = None,
) -> ModelCapabilities:
    """Build :class:`ModelCapabilities` from presets + config overrides.

    Resolution order: default → matched preset → provider overlay → overrides.
    """
    cfg = _match_preset(model_name, provider)

    if overrides:
        _apply_config_overrides(cfg, overrides)

    schema = _SCHEMA_SANITIZERS.get(cfg.get("schema_sanitizer", "passthrough"), PassthroughSchema)()
    prompt = _PROMPT_POLICIES.get(cfg.get("prompt_policy", "none"), NoPromptEnrichment)()
    content = _CONTENT_POLICIES.get(cfg.get("content_policy", "openai"), OpenAIContent)()
    response = _RESPONSE_POLICIES.get(cfg.get("response_policy", "standard"), StandardResponse)()
    reasoning_name = cfg.get("reasoning_codec", "none")
    reasoning_cls = _REASONING_CODECS.get(reasoning_name, NoReasoningCodec)
    reasoning_kwargs: dict[str, Any] = {}
    if reasoning_name == "gemini" and "gemini_drop_placeholder_signature" in cfg:
        reasoning_kwargs["drop_placeholder_signature"] = bool(
            cfg["gemini_drop_placeholder_signature"]
        )
    reasoning = reasoning_cls(**reasoning_kwargs)
    cache = _CACHE_POLICIES.get(cfg.get("cache_policy", "none"), NoCache)()
    params = GenerationParams(**cfg.get("params", {}))

    api_call_timeout_s = cfg.get("api_call_timeout_s")
    api_call_retries = cfg.get("api_call_retries")

    return ModelCapabilities(
        schema_sanitizer=schema,
        prompt_policy=prompt,
        content_policy=content,
        params_policy=params,
        response_policy=response,
        reasoning_codec=reasoning,
        cache_policy=cache,
        api_call_timeout_s=float(api_call_timeout_s) if api_call_timeout_s is not None else None,
        api_call_retries=int(api_call_retries) if api_call_retries is not None else None,
    )


# ---------------------------------------------------------------------------
# Preset fingerprint helpers
# ---------------------------------------------------------------------------
#
# Downstream consumers (analytics tools, the per-trial ``task.yaml`` writer)
# need a stable, JSON-serialisable view of WHICH preset + WHICH policies
# landed on a given :class:`ModelCapabilities`. These helpers perform the
# reverse lookup: "given a capabilities instance, what preset names does the
# registry reveal?"
#
# ``params_policy`` is intentionally omitted from the fingerprint — it is a
# stateful :class:`GenerationParams` dataclass, not a single-named policy, and
# round-trips independently via ``ModelConfig`` serialisation.


def _reverse_lookup(
    instance: Any,
    registry: dict[str, type[Any]],
    slot_name: str,
) -> str:
    """Find the registry name whose class matches ``type(instance)``.

    Raises ``ValueError`` if no match — surfaces configuration drift rather
    than silently emitting a stub (AGENTS.md rule #1).
    """
    instance_cls = type(instance)
    for name, cls in registry.items():
        if cls is instance_cls:
            return name
    raise ValueError(
        f"Unknown policy instance for slot {slot_name!r}: "
        f"{instance_cls.__name__} not in registry "
        f"{sorted(registry.keys())!r}"
    )


def resolve_policy_names(capabilities: ModelCapabilities) -> dict[str, str]:
    """Reverse-lookup from policy instances on *capabilities* to registry names.

    Returns a dict shaped for ``task.yaml.model_config.<role>.resolved.*``
    consumption::

        {
            "schema_sanitizer": "strict" | "passthrough",
            "prompt_policy":   "dict_map_hints" | "none",
            "content_policy":  "anthropic" | "openai" | "nova",
            "response_policy": "standard" | "array_dict_map" | "unwrap_input",
            "reasoning_codec": "anthropic" | "openai" | "none",
            "cache_policy":    "anthropic_ephemeral" | "none",
        }

    ``params_policy`` is omitted — it is a stateful dataclass, not a single
    named policy. Callers that need the full parameter block should serialise
    ``ModelConfig`` alongside this fingerprint.

    Raises
    ------
    ValueError
        If any slot holds a policy instance not registered in the
        corresponding name → class map. This is an explicit failure signal
        rather than a silent pass-through, per AGENTS.md rule #1.
    """
    return {
        "schema_sanitizer": _reverse_lookup(
            capabilities.schema_sanitizer, _SCHEMA_SANITIZERS, "schema_sanitizer"
        ),
        "prompt_policy": _reverse_lookup(
            capabilities.prompt_policy, _PROMPT_POLICIES, "prompt_policy"
        ),
        "content_policy": _reverse_lookup(
            capabilities.content_policy, _CONTENT_POLICIES, "content_policy"
        ),
        "response_policy": _reverse_lookup(
            capabilities.response_policy, _RESPONSE_POLICIES, "response_policy"
        ),
        "reasoning_codec": _reverse_lookup(
            capabilities.reasoning_codec, _REASONING_CODECS, "reasoning_codec"
        ),
        "cache_policy": _reverse_lookup(capabilities.cache_policy, _CACHE_POLICIES, "cache_policy"),
    }


def resolve_effective_preset(model_name: str, provider: str = "") -> str:
    """Return the preset name that matched *model_name* / *provider*.

    Mirrors the first-match-wins routing in :func:`_match_preset` but returns
    only the preset identifier, never the merged config. If no ``presets:``
    entry matched, returns ``"default"`` — the same label the orchestrator
    records in ``task.yaml.model_config.<role>.resolved.effective_preset``
    when a model falls through to the base-case defaults.

    Examples
    --------
    >>> resolve_effective_preset("anthropic/claude-opus-4.7")
    'anthropic_claude_4_7'
    >>> resolve_effective_preset("openai/gpt-5.5")
    'openai_gpt5'
    >>> resolve_effective_preset("some-unknown-model")
    'default'
    """
    for preset_name, _preset in _iter_preset_matches(model_name, provider):
        return preset_name  # first match wins
    return "default"
