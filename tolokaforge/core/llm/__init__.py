"""LLM abstractions — reasoning, schema, cache, usage, client.

This package is the sole boundary between Tolokaforge and provider-specific
details (``thinking_blocks``, ``cache_control``, ``reasoning_content``, …).
Raw provider shapes must never leak above it; callers work with the curated
public types exported here. Per-model policy subclasses ship in
:mod:`tolokaforge_models.policies` and register with the engine via the
``tolokaforge.policies`` entry-point group.

See [`docs/LLM_LAYER.md`](../../../docs/LLM_LAYER.md) for module-by-module
purpose and the plan at
[`plans/llm_reasoning_and_observability_fix.md`](../../../plans/llm_reasoning_and_observability_fix.md)
for design rationale.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

# Safe eager imports — these modules do not depend on tolokaforge.core.models.
from tolokaforge.core.llm.cache_policy import CachePolicy, NoCache
from tolokaforge.core.llm.content_policy import (
    AnthropicContent,
    OpenAIContent,
    ToolContentPolicy,
)
from tolokaforge.core.llm.dict_maps import find_additional_properties
from tolokaforge.core.llm.message_assembly_policy import (
    FillEmptyAssistantAssembly,
    MessageAssemblyPolicy,
    NullMessageAssembly,
)
from tolokaforge.core.llm.params_policy import GenerationParams, ParamsPolicy
from tolokaforge.core.llm.prompt_policy import (
    DictMapHints,
    NoPromptEnrichment,
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
    ReasoningCodec,
)
from tolokaforge.core.llm.response_policy import (
    ArrayDictMapResponse,
    JsonCoerceResponse,
    ResponsePolicy,
    StandardResponse,
    UnwrapInputResponse,
    coerce_empty_containers,
    coerce_json_strings,
)
from tolokaforge.core.llm.schema_sanitizer import (
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
_LAZY: Final[dict[str, tuple[str, str]]] = {
    "ModelCapabilities": ("tolokaforge.core.llm.capabilities", "ModelCapabilities"),
    "LLMClient": ("tolokaforge.core.llm.client", "LLMClient"),
    "UserSimulator": ("tolokaforge.core.llm.client", "UserSimulator"),
    "GenerationResult": ("tolokaforge.core.llm.client", "GenerationResult"),
    "SIMULATOR_GREETING": ("tolokaforge.core.llm.client", "SIMULATOR_GREETING"),
    "build_capabilities": ("tolokaforge.core.llm.presets", "build_capabilities"),
}


# ``tolokaforge.core.llm.<name>`` deprecation shim for the eight per-model
# policy subclasses that moved to :mod:`tolokaforge_models.policies`. Emits
# ``DeprecationWarning`` on first access per name; subsequent accesses to the
# same name resolve silently through ``_WARNED``-caching (Python's default
# filter dedupes on ``(message, category, module, lineno)`` — two callsites
# on different source lines would each re-warn otherwise). Removed in v0.18.0.
_MOVED_SUBCLASSES: Final[dict[str, str]] = {
    "GeminiSchema": "tolokaforge_models.policies.gemini",
    "GeminiRecursiveSchema": "tolokaforge_models.policies.gemini",
    "ScalarArrayDictMapResponse": "tolokaforge_models.policies.gemini",
    "RefResolvingDictMapHints": "tolokaforge_models.policies.inkling",
    "JsonRecursiveCoerceResponse": "tolokaforge_models.policies.minimax",
    "ItemRecursiveUnwrapResponse": "tolokaforge_models.policies.minimax",
    "MinimaxM3TagRecoveryResponse": "tolokaforge_models.policies.minimax",
    "OpenAISummaryReplayReasoningCodec": "tolokaforge_models.policies.deepseek",
}
_WARNED: set[str] = set()


def __getattr__(name: str) -> Any:  # pragma: no cover - simple delegator
    # ``ParamPolicy`` is a class-identity alias for :class:`ParamsPolicy`
    # kept for one release. Same shim shape as the eight moved subclasses:
    # one warning per name per process, resolves to the current class.
    if name == "ParamPolicy":
        if name not in _WARNED:
            import warnings

            warnings.warn(
                "tolokaforge.core.llm.ParamPolicy is deprecated; import "
                "ParamsPolicy instead. Shim removed in v0.18.0.",
                DeprecationWarning,
                stacklevel=2,
            )
            _WARNED.add(name)
        return ParamsPolicy
    module_path = _MOVED_SUBCLASSES.get(name)
    if module_path is not None:
        if name not in _WARNED:
            import warnings

            warnings.warn(
                f"tolokaforge.core.llm.{name} moved to {module_path}.{name} — "
                f"import from the new path. Shim removed in v0.18.0.",
                DeprecationWarning,
                stacklevel=2,
            )
            _WARNED.add(name)
        import importlib

        return getattr(importlib.import_module(module_path), name)
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
    "FillEmptyAssistantAssembly",
    # Capabilities + presets (lazy)
    "ModelCapabilities",
    "build_capabilities",
    # Client (lazy)
    "LLMClient",
    "GenerationResult",
    "UserSimulator",
    "SIMULATOR_GREETING",
]
