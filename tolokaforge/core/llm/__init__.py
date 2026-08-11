"""LLM abstractions — reasoning, schema, cache, usage, client.

This package is the sole boundary between Tolokaforge and provider-specific
details (``thinking_blocks``, ``cache_control``, ``reasoning_content``, …).
Raw provider shapes must never leak above it; callers work with the curated
public types exported here.

See [`docs/LLM_LAYER.md`](../../../docs/LLM_LAYER.md) for module-by-module
purpose and the plan at
[`plans/llm_reasoning_and_observability_fix.md`](../../../plans/llm_reasoning_and_observability_fix.md)
for design rationale.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

# Safe eager imports — these modules do not depend on tolokaforge.core.models.
from tolokaforge.core.llm.cache_policy import CachePolicy, NoCache
from tolokaforge.core.llm.content_policy import (
    AnthropicContent,
    OpenAIContent,
    ToolContentPolicy,
)
from tolokaforge.core.llm.dict_maps import find_additional_properties
from tolokaforge.core.llm.message_assembly_policy import (
    MessageAssemblyPolicy,
    NovaMessageAssembly,
    NullMessageAssembly,
)
from tolokaforge.core.llm.params_policy import GenerationParams, ParamPolicy, ParamsPolicy
from tolokaforge.core.llm.prompt_policy import (
    DictMapHints,
    NoPromptEnrichment,
    RefResolvingDictMapHints,
    SystemPromptPolicy,
)
from tolokaforge.core.llm.reasoning import (
    ReasoningBlock,
    ReasoningConfig,
    ReasoningMode,
    StructuredReasoning,
)
from tolokaforge.core.llm.reasoning_codec import (
    AnthropicReasoningCodec,
    NoReasoningCodec,
    OpenAIReasoningCodec,
    OpenAISummaryReplayReasoningCodec,
    ReasoningCodec,
)
from tolokaforge.core.llm.response_policy import (
    ArrayDictMapResponse,
    ItemRecursiveUnwrapResponse,
    JsonCoerceResponse,
    JsonRecursiveCoerceResponse,
    MinimaxM3TagRecoveryResponse,
    ResponsePolicy,
    ScalarArrayDictMapResponse,
    StandardResponse,
    UnwrapInputResponse,
    coerce_empty_containers,
    coerce_json_strings,
)
from tolokaforge.core.llm.schema_sanitizer import (
    GeminiRecursiveSchema,
    GeminiSchema,
    PassthroughSchema,
    SchemaCapability,
    StrictSchema,
    ToolSchemaSanitizer,
)
from tolokaforge.core.llm.usage import Usage, UsageExtractor

if TYPE_CHECKING:  # pragma: no cover - type-only re-exports
    from tolokaforge.core.llm.capabilities import ModelCapabilities
    from tolokaforge.core.llm.client import (
        SIMULATOR_GREETING,
        GenerationResult,
        LLMClient,
        UserSimulator,
    )
    from tolokaforge.core.llm.presets import build_capabilities


# ``ModelCapabilities``, ``LLMClient``, ``UserSimulator``, ``GenerationResult``
# and ``build_capabilities`` all import :mod:`tolokaforge.core.models`, which
# itself imports :mod:`tolokaforge.core.llm.reasoning`. To break the import
# cycle we expose them via module-level ``__getattr__`` (PEP 562).
_LAZY: dict[str, tuple[str, str]] = {
    "ModelCapabilities": ("tolokaforge.core.llm.capabilities", "ModelCapabilities"),
    "LLMClient": ("tolokaforge.core.llm.client", "LLMClient"),
    "UserSimulator": ("tolokaforge.core.llm.client", "UserSimulator"),
    "GenerationResult": ("tolokaforge.core.llm.client", "GenerationResult"),
    "SIMULATOR_GREETING": ("tolokaforge.core.llm.client", "SIMULATOR_GREETING"),
    "build_capabilities": ("tolokaforge.core.llm.presets", "build_capabilities"),
}


def __getattr__(name: str) -> Any:  # pragma: no cover - simple delegator
    if name in _LAZY:
        module_name, attr_name = _LAZY[name]
        import importlib

        module = importlib.import_module(module_name)
        value = getattr(module, attr_name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Reasoning
    "ReasoningMode",
    "ReasoningConfig",
    "ReasoningBlock",
    "StructuredReasoning",
    "ReasoningCodec",
    "NoReasoningCodec",
    "AnthropicReasoningCodec",
    "OpenAIReasoningCodec",
    "OpenAISummaryReplayReasoningCodec",
    # Usage
    "Usage",
    "UsageExtractor",
    # Schema sanitizer
    "SchemaCapability",
    "ToolSchemaSanitizer",
    "PassthroughSchema",
    "StrictSchema",
    "GeminiSchema",
    "GeminiRecursiveSchema",
    # Cache
    "CachePolicy",
    "NoCache",
    # Prompt / content / response / params policies
    "SystemPromptPolicy",
    "NoPromptEnrichment",
    "DictMapHints",
    "RefResolvingDictMapHints",
    "ToolContentPolicy",
    "OpenAIContent",
    "AnthropicContent",
    "ResponsePolicy",
    "StandardResponse",
    "UnwrapInputResponse",
    "JsonCoerceResponse",
    "ArrayDictMapResponse",
    "ScalarArrayDictMapResponse",
    "JsonRecursiveCoerceResponse",
    "ItemRecursiveUnwrapResponse",
    "MinimaxM3TagRecoveryResponse",
    "coerce_empty_containers",
    "coerce_json_strings",
    "find_additional_properties",
    "ParamsPolicy",
    "ParamPolicy",
    "GenerationParams",
    "MessageAssemblyPolicy",
    "NullMessageAssembly",
    "NovaMessageAssembly",
    # Capabilities + presets (lazy)
    "ModelCapabilities",
    "build_capabilities",
    # Client (lazy)
    "LLMClient",
    "GenerationResult",
    "UserSimulator",
    "SIMULATOR_GREETING",
]
