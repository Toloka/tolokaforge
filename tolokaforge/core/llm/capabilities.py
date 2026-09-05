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
from tolokaforge.core.models.model_config import OpenRouterConfig

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

    empty_retry_count: int = 0
    """Resample budget for a provider-side empty completion.

    On a returned ``result.text == "" and not result.tool_calls`` shape the
    engine resamples up to ``empty_retry_count`` times without appending the
    empty assistant message and without advancing the outer turn counter; on
    the ``(N + 1)``-th empty result the trial terminates with
    ``TerminationReason.EMPTY_COMPLETION``. The default ``0`` matches the
    one-shot terminal behaviour for presets that do not opt in. Metrics record
    every resampled generation because the trial paid for each call.
    """

    output_length_retry_count: int = 0
    """Resample budget for a content-carrying max-tokens truncation.

    On a returned ``GenerationResult`` with content and
    ``finish_reason == "length"`` (litellm's post-mapped value for a provider
    that truncated the response at ``max_tokens``), the engine appends a
    ``role=user`` feedback turn advising the model that its previous response
    was cut off and asking it to split the next action into smaller pieces,
    then resamples up to ``output_length_retry_count`` times without
    appending the truncated assistant message and without advancing the outer
    turn counter. On budget exhaustion the loop appends the last truncated
    response as the assistant turn and continues — the seam is strictly
    recoverable and never a new terminal reason.

    Orthogonal to :attr:`empty_retry_count` (which fires on empty-shape
    results with no content and terminates on exhaustion) and to the
    loop-level API-error retry: each retry class owns a distinct trigger and
    a dedicated budget. The default ``0`` accepts the truncated response as
    the assistant turn unchanged. Metrics record every resampled generation
    because the trial paid for each call.
    """

    parser_error_retry_count: int = 0
    """Resample budget for a response whose ``tool_call.function.arguments``
    string could not be decoded by :meth:`LLMClient._try_parse_tool_arguments`.

    On a returned ``GenerationResult`` with non-empty ``parser_errors``, the
    engine appends a ``role=user`` feedback turn naming the failing tools,
    quoting the raw arguments excerpt, and reporting the parse reason, then
    resamples up to ``parser_error_retry_count`` times without appending the
    discarded assistant message and without advancing the outer turn counter.
    On budget exhaustion the loop falls through to accept-and-continue: the
    ``{}``-coerced assistant response lands and either the tool executor
    surfaces an ``INVALID_ARGUMENTS`` tool_result (for tools with a real
    schema) or the tool runs against empty args (for no-arg tools) —
    reproducing today's behaviour byte-for-byte. The seam is strictly
    recoverable and never a new terminal reason.

    Orthogonal to :attr:`empty_retry_count` (empty-shape completion,
    terminates on exhaustion), :attr:`output_length_retry_count`
    (content-carrying max-tokens truncation), and the loop-level API-error
    retry (raised exception). Each retry class owns a distinct trigger and a
    dedicated budget. The default ``0`` accepts the ``{}``-coerced response
    as the assistant turn unchanged — the seam is opt-in per preset. Metrics
    record every resampled generation because the trial paid for each call.
    """

    tool_output_max_chars: int | None = None
    """Loop-layer cap on the ``role=tool`` message content, in chars.

    When set, ``ToolCallingLoop._execute_tool_calls`` middle-elides the
    ``Message.content`` string via
    :func:`~tolokaforge.core.tool_output_truncation.keep_head_and_tail` before
    the tool message is appended, so accumulated context stays predictable
    across long-running trials. The trial's
    :class:`~tolokaforge.core.models.ToolCallRecorder` and the grader inputs
    still read the full text via ``resolve_tool_output(tool_result)`` upstream
    of the cap. ``Message.content_blocks`` is unaffected: multimodal payloads
    are fixed-size per call and would break if partially clipped. ``None``
    (the default) leaves every tool message threaded through verbatim.
    """

    default_max_turns: int | None = None
    """Preset-level default for the per-trial turn budget when the task
    did not declare its own ``TaskConfig.max_turns``.

    Different models converge in different numbers of steps on the same
    task — a coarser-grained model completes in fewer turns than one whose
    per-turn edit style is more granular. This slot fills the gap when
    neither the task nor the operator's run config set a budget: the
    conductor's ``resolve_max_turns`` reads it as the value default before
    the engine-wide fallback ``DEFAULT_MAX_TURNS = 50`` applies.
    ``TaskConfig.max_turns`` still wins when set, and
    ``OrchestratorConfig.max_turns`` still ceilings the resolved value.
    ``None`` (the default) leaves the engine-wide fallback in place — every
    preset that does not name the key inherits it.
    """

    max_context_tokens: int | None = None
    """Preset-declared provider max input tokens.

    Paired with :attr:`context_watermark` to gate the loop's pre-turn
    summarize check: when the previous generation's ``Usage.prompt_tokens +
    context_watermark >= max_context_tokens``, the loop invokes its
    :class:`~tolokaforge.core.summarize_policy.SummarizePolicy` and rewrites
    the wire message history. ``None`` disables both the pre-turn watermark
    check and the reactive summarize on a
    :class:`~litellm.exceptions.ContextWindowExceededError`; the trial's
    behaviour on those events matches the pre-opt-in path (a reactive
    context-window error still terminates via the typed classifier, with
    :attr:`~tolokaforge.core.models.TerminationReason.CONTEXT_WINDOW_EXCEEDED`).
    """

    context_watermark: int | None = None
    """Free-token threshold that arms the pre-turn summarize check.

    ``None`` disables the check even when :attr:`max_context_tokens` is set —
    a preset can declare its context size for other uses without opting into
    summarize. See :attr:`max_context_tokens` for the arithmetic.
    """

    openrouter_defaults: OpenRouterConfig | None = None
    """Preset-level default for :attr:`ModelConfig.openrouter`.

    ``LLMClient._build_kwargs`` resolves the effective OpenRouter routing
    field-by-field: the user's :attr:`ModelConfig.openrouter` value wins
    per-field when explicitly set (``provider_order`` non-``None``,
    ``allow_fallbacks`` present on the block), and the preset default fills
    the gap. Both unset — the default here plus no user block — leaves
    ``extra_body.provider`` off the wire, matching the pre-opt-in behaviour.
    The gateway pin-drop rule at :meth:`LLMClient._build_kwargs` applies
    identically to preset-sourced pins and user-sourced pins: a route into
    another provider namespace still drops the pin.
    """
