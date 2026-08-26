"""Composite capabilities dataclass.

:class:`ModelCapabilities` bundles every per-model policy (schema sanitizer,
prompt enrichment, params, response, content, reasoning codec, cache) so the
client can adapt a generation request with zero conditional branching.

Instances are built by :func:`tolokaforge.core.llm.presets.build_capabilities`
from the YAML preset registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tolokaforge.core.llm.assistant_text_policy import (
    AssistantTextPolicy,
    PassthroughAssistantText,
)
from tolokaforge.core.llm.cache_policy import CachePolicy, NoCache
from tolokaforge.core.llm.content_policy import OpenAIContent, ToolContentPolicy
from tolokaforge.core.llm.message_assembly_policy import (
    MessageAssemblyPolicy,
    NullMessageAssembly,
)
from tolokaforge.core.llm.params_policy import GenerationParams
from tolokaforge.core.llm.prompt_policy import NoPromptEnrichment, SystemPromptPolicy
from tolokaforge.core.llm.reasoning_codec import NoReasoningCodec, ReasoningCodec
from tolokaforge.core.llm.response_policy import ResponsePolicy, StandardResponse
from tolokaforge.core.llm.schema_sanitizer import PassthroughSchema, ToolSchemaSanitizer

__all__ = ["ModelCapabilities"]


@dataclass(frozen=True)
class ModelCapabilities:
    """Declares what a model/provider combination supports for function calling
    and generation.

    Composes policy objects that encapsulate model-specific behaviour.
    ``LLMClient`` delegates to these policies to adapt tool schemas, system
    prompts, generation parameters, and response parsing — with zero
    conditional branching.
    """

    schema_sanitizer: ToolSchemaSanitizer = field(default_factory=PassthroughSchema)
    """Rewrites tool JSON Schema to the subset the target model accepts."""

    prompt_policy: SystemPromptPolicy = field(default_factory=NoPromptEnrichment)
    """Optionally enriches the system prompt before generation."""

    content_policy: ToolContentPolicy = field(default_factory=OpenAIContent)
    """Defines tool result content format for the target model."""

    params_policy: GenerationParams = field(default_factory=GenerationParams)
    """Adapts generation parameters (temperature, seed, reasoning)."""

    response_policy: ResponsePolicy = field(default_factory=StandardResponse)
    """Post-processes tool call arguments from model response."""

    reasoning_codec: ReasoningCodec = field(default_factory=NoReasoningCodec)
    """Provider-specific extract + replay for structured reasoning."""

    cache_policy: CachePolicy = field(default_factory=NoCache)
    """Attaches cache-control markers to cacheable request prefixes."""

    message_assembly_policy: MessageAssemblyPolicy = field(default_factory=NullMessageAssembly)
    """Empty-assistant-content substitution on tool-call turns.

    ``NullMessageAssembly`` leaves empty content empty (every preset
    outside the opt-in list). ``FillEmptyAssistantAssembly`` substitutes a
    filler string for the two provider families that reject the empty
    shape alongside ``tool_calls`` — AWS Bedrock/Nova and Moonshot direct.
    """

    assistant_text_policy: AssistantTextPolicy = field(default_factory=PassthroughAssistantText)
    """Reshapes assistant text on ``message.content`` before it lands in
    :class:`GenerationResult.text`.

    ``PassthroughAssistantText`` returns the text unchanged (every shipped
    preset). A subclass strips provider-specific markers (e.g. Cohere's
    ``<|START_TEXT|>…<|END_TEXT|>`` delimiters) without engine edits.
    """

    api_call_timeout_s: float | None = None
    """Per-call upstream API timeout, in seconds.

    ``None`` falls back to the env var ``TOLOKAFORGE_LLM_API_CALL_TIMEOUT_S``
    or the global default. Set per-preset for slow reasoning models that
    need a longer budget than other models in the same harness run.
    """

    api_call_wall_timeout_s: float | None = None
    """Hard wall-clock ceiling for a single upstream call, in seconds.

    Unlike :attr:`api_call_timeout_s` (a per-read/connect timeout that a
    slowly-streamed or runaway response can keep resetting, so it never
    bounds total elapsed time), this caps the total wall-clock duration of
    one ``completion`` call. On overrun the call is abandoned and raised as
    ``LLMApiTimeoutError`` terminally: it bypasses the per-call timeout retry
    (retrying a runaway generation would only stack abandoned in-flight
    calls).

    ``None`` (the default) disables the wall-clock abort and falls back to the
    env var ``TOLOKAFORGE_LLM_API_CALL_WALL_TIMEOUT_S``. Set it for providers
    that occasionally ignore ``max_tokens`` and stream a runaway response for
    tens of minutes.
    """

    api_call_retries: int | None = None
    """Bounded retries on transport-level timeouts.

    ``None`` falls back to the env var ``TOLOKAFORGE_LLM_API_CALL_RETRIES``
    or the global default.
    """
