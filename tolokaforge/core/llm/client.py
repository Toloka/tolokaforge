"""LLM client abstraction using LiteLLM.

``LLMClient`` is the single provider-agnostic entry point for generation.
It composes a :class:`~tolokaforge.core.llm.capabilities.ModelCapabilities`
set (schema sanitizer, prompt enrichment, params, response, content,
reasoning codec, cache) and delegates per-request adaptation to each policy.

``UserSimulator`` wraps the same client for tau-bench-style user simulation.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tolokaforge.core.loop import TerminationDecision

import litellm
import openai
import tenacity.nap
import yaml
from litellm import completion
from tenacity import (
    RetryCallState,
    Retrying,
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_combine,
    wait_exponential,
    wait_fixed,
    wait_random,
)
from tenacity.wait import wait_base

from tolokaforge.core.actors.actor import Actor
from tolokaforge.core.actors.reply_guard import UserReplyGuard
from tolokaforge.core.llm.capabilities import ModelCapabilities
from tolokaforge.core.llm.gateway_route import (
    ResolvedGatewayRoute,
    RouteKind,
    fetch_gateway_catalog,
    resolve_gateway_route,
)
from tolokaforge.core.llm.litellm_params import allowed_openai_params
from tolokaforge.core.llm.params_policy import RuleAction
from tolokaforge.core.llm.presets import build_capabilities
from tolokaforge.core.llm.prompt_policy import detect_dict_maps
from tolokaforge.core.llm.providers import compile_rate_limit_patterns, get_provider_binding
from tolokaforge.core.llm.proxy import resolve_proxy_config
from tolokaforge.core.llm.reasoning import ReasoningConfig, StructuredReasoning
from tolokaforge.core.llm.usage import (
    CostSource,
    Usage,
    UsageExtractor,
    extract_openrouter_generation_id,
)
from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import (
    Message,
    MessageRole,
    ModelConfig,
    RateLimitProbeConfig,
    ReplyDefect,
    ToolCall,
)
from tolokaforge.core.pricing import estimate_cost
from tolokaforge.core.run_display_events import LLMCallObservation

# Silence litellm's stdout banners ("Provider List: https://docs.litellm.ai/docs/providers",
# "LiteLLM completion() model= ...") - pure noise in probe/eval logs; no effect on behavior/results.
litellm.suppress_debug_info = True

__all__ = [
    "AllApiKeysExhaustedError",
    "GenerationResult",
    "LLMApiTimeoutError",
    "LLMClient",
    "ParserError",
    "UserSimulator",
]

# Bound on the raw-arguments excerpt carried by ``ParserError.raw_arguments``.
# A malformed tool_call args payload has enough surrounding context at 500
# chars for the model to locate its syntactic issue on the resample. Distinct
# from ``REPLY_DEFECT_EXCERPT_MAX_CHARS`` (200): that constant caps a
# substring-match excerpt around a suspicious span, this one caps the whole
# malformed payload — different use cases, tuned independently.
PARSER_ERROR_RAW_ARGS_EXCERPT_MAX_CHARS = 500


@dataclass(frozen=True)
class ParserError:
    """Sidecar record of one un-parseable ``tool_call.function.arguments``
    string. Ephemeral in-process value; never crosses a wire boundary and
    never serialised into ``Trajectory`` or task artefacts, so a frozen
    dataclass fits and ``ReplyDefect`` (Pydantic) is the wrong shape.

    ``raw_arguments`` is the original ``tc.function.arguments`` string
    (verbatim, so the retry feedback can quote it back), bounded by
    :data:`PARSER_ERROR_RAW_ARGS_EXCERPT_MAX_CHARS`. ``reason`` is the parse
    failure phrase — the exception message from the last failing parser or
    ``"Unable to parse with JSON/YAML fallbacks"`` when every fallback
    exhausted.
    """

    tool_name: str
    raw_arguments: str
    reason: str


_module_logger = get_logger("llm_client_cost")


DEFAULT_API_CALL_TIMEOUT_S = 120.0
DEFAULT_API_TIMEOUT_RETRIES = 5
# Hard wall-clock ceiling per upstream call. ``None`` keeps the wall-clock
# abort disabled by default (backward-compatible); opt in per-model via the
# preset or the ``TOLOKAFORGE_LLM_API_CALL_WALL_TIMEOUT_S`` env var.
DEFAULT_API_CALL_WALL_TIMEOUT_S: float | None = None


class LLMApiTimeoutError(RuntimeError):
    """Raised when an LLM API call exceeds the configured timeout budget.

    Carries through ``_call_with_key_rotation`` un‑wrapped so the outer
    :class:`tenacity.Retrying` controller in :meth:`LLMClient.generate` can
    decline to re‑attempt (see :func:`_should_retry_exception`).
    """


class AllApiKeysExhaustedError(RuntimeError):
    """Raised when every rotatable API key has hit its own quota / credit cap.

    A :class:`RuntimeError` subclass, so :func:`_should_retry_exception` and the
    engine's remaining text-matching 429 classifiers
    (``TrialRunner._is_rate_limit_error``, ``core/resume.py``) treat it as the
    plain ``RuntimeError`` it always was.

    The type exists so :meth:`LLMClient._is_rate_limit_exception` and
    :func:`is_typed_rate_limit_exception` can tell this apart from a transient
    429. ``_call_with_key_rotation`` enters its rotation branch on
    the provider's own 429 ("Key limit exceeded") and chains it as ``__cause__``,
    so a ``__cause__`` walk would otherwise classify a **terminal** condition as
    a rate limit and hand it rate-limit probe mode's multi-hour fixed-interval
    budget. :meth:`LLMClient._rotate_key` only ever advances its index, so the
    condition never clears for the rest of the run.
    """


def _effective_system_as_text(system: str | list[dict[str, Any]] | None) -> str | None:
    """Flatten the post-policy system prompt to its plain-text form.

    Cache policies (e.g. :class:`AnthropicEphemeralCache`) may rewrite the
    system prompt into a list of content-blocks on the wire. Downstream
    consumers (``GenerationResult.effective_system_prompt``, trajectory
    writer, analytics) expect a single string — this helper concatenates any
    ``text`` fields in the block-list form and returns the ``str`` unchanged.
    Returns ``None`` only when *system* is ``None``.
    """
    if system is None:
        return None
    if isinstance(system, str):
        return system
    texts: list[str] = []
    for block in system:
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                texts.append(text)
    return "".join(texts) if texts else ""


def _root_param_types_by_tool(
    sanitized_tools: list[dict[str, Any]] | None,
) -> dict[str, dict[str, str]]:
    """Build ``{tool_name: {param_name: declared_type}}`` from sanitised tools.

    The response policy uses this to drive schema-aware coercions
    (currently: empty-container recovery — ``''`` → ``[]`` / ``''`` →
    ``{}`` for declared ``array`` / ``object`` params). Only root-level
    parameters are mapped; nested objects don't currently need coercion.

    For ``Optional[array | object]`` shapes (Pydantic ``anyOf``), pick the
    first non-null branch's ``type`` so empty-string coercion still fires
    for ``equipment: Optional[list[str]]``.
    """
    out: dict[str, dict[str, str]] = {}
    for tool in sanitized_tools or []:
        if not isinstance(tool, dict):
            continue
        func = tool.get("function") or {}
        name = func.get("name")
        if not isinstance(name, str):
            continue
        params = func.get("parameters") or {}
        props = params.get("properties") if isinstance(params, dict) else None
        if not isinstance(props, dict):
            continue
        type_map: dict[str, str] = {}
        for prop_name, prop_schema in props.items():
            if not isinstance(prop_schema, dict):
                continue
            declared = _resolve_declared_type(prop_schema)
            if declared is not None:
                type_map[prop_name] = declared
        if type_map:
            out[name] = type_map
    return out


def _resolve_declared_type(schema: dict[str, Any]) -> str | None:
    """Return the JSON-Schema ``type`` for a property, recursing one level
    into ``anyOf`` to handle ``Optional[X]`` (``[{type: X}, {type: null}]``).

    Distinguishes ``StrictSchema``'s dict-map → array conversion shape from
    a genuine ``array`` param: when a property is declared
    ``{type: array, items: {properties: {key, …}}}`` we return
    ``"dict_map"`` instead of ``"array"``. The receiving tool's Pydantic
    validator still expects ``Dict[str, T]`` for that param, so the
    response policy needs to know to pivot empty arrays back to empty
    dicts (otherwise the tool rejects with the confusing
    ``"Input should be a valid dictionary"`` type error).
    """
    declared = schema.get("type")
    if isinstance(declared, str) and declared != "null":
        if declared == "array" and _is_dict_map_array_shape(schema):
            return "dict_map"
        return declared
    branches = schema.get("anyOf")
    if isinstance(branches, list):
        for branch in branches:
            if not isinstance(branch, dict):
                continue
            inner = branch.get("type")
            if isinstance(inner, str) and inner != "null":
                if inner == "array" and _is_dict_map_array_shape(branch):
                    return "dict_map"
                return inner
    return None


# Synthetic key field that ``StrictSchema`` adds to items when converting
# a dict-map (``additionalProperties: {schema}``) parameter to an array.
# Kept here as a literal rather than importing from ``schema_sanitizer``
# to keep the dependency graph one-way (sanitizer → response_policy →
# client; the client builds ``param_types`` without circularly
# importing the sanitizer's constants).
_STRICT_SCHEMA_KEY_FIELD = "key"


def _is_dict_map_array_shape(schema: dict[str, Any]) -> bool:
    """True when ``schema`` is ``StrictSchema``'s dict-map → array output.

    Shape signature::

        {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {...},   # the synthetic discriminator field
                    ...other fields from the original dict-map value type...
                },
                ...
            },
            ...
        }
    """
    items = schema.get("items")
    if not isinstance(items, dict):
        return False
    item_props = items.get("properties")
    if not isinstance(item_props, dict):
        return False
    return _STRICT_SCHEMA_KEY_FIELD in item_props


def _should_retry_exception(exc: BaseException) -> bool:
    """Predicate for the outer :class:`tenacity.Retrying` controller in ``generate``.

    Returns ``True`` for all transient errors, with two exclusions:

    - :class:`LLMApiTimeoutError` — already the result of an exhausted
      per-call retry budget inside
      :meth:`LLMClient._call_completion_with_timeout_retry`; must not
      cascade into another ``stop_after_attempt(5)`` multiplier.
    - :class:`openai.AuthenticationError` — deterministic 401/403 that
      does not become truthy by retrying. Litellm wraps provider auth
      failures in this class regardless of the underlying vendor, so a
      single ``isinstance`` check covers OpenAI, OpenRouter, Anthropic,
      Google, and every other supported provider. Retrying wastes ~32s
      of exponential-backoff wall time per trial and hides the actual
      failure signal from the operator.

    Rate limits (429) ride the same outer exponential backoff as other
    transient errors — the long waits (up to 60s between attempts) give
    provider quota time to recover. This predicate stays 429-agnostic on
    both controllers; rate-limit probe mode differentiates 429s in ``stop``
    and ``wait`` (see :meth:`LLMClient._build_probe_retrying`), not here.
    """
    if isinstance(exc, LLMApiTimeoutError):
        return False
    if isinstance(exc, openai.AuthenticationError):
        return False
    return True


_EXCEPTION_CAUSE_DEPTH = 4
"""How far :meth:`LLMClient._is_rate_limit_exception` walks ``__cause__``.

``_call_with_key_rotation`` re-raises every non-timeout provider error as
``RuntimeError(f"LLM API call failed: {e}") from e``, so the typed 429 the
provider raised is one link down the chain by the time the outer controller
sees it. Four links cover that wrap plus any future one without risking an
unbounded walk on a self-referencing chain.
"""


def matches_rate_limit_text(text: str, patterns: Iterable[re.Pattern[str]]) -> bool:
    """True when *text* carries one of the anchored 429 shapes in *patterns*.

    Callers supply the pattern set: shipped providers carry a bundled default
    (see :data:`tolokaforge.core.llm.providers.DEFAULT_RATE_LIMIT_PATTERNS`),
    but the client resolves them per-provider via
    :attr:`LLMClient._compiled_rate_limit_patterns`.

    Prose is not evidence of a rate limit on its own — this predicate acts as
    a *harness* diagnostic (via
    :func:`~tolokaforge.core.loop.classify_loop_error`) rather than as an
    infrastructure verdict.
    """
    return any(pattern.search(text) for pattern in patterns)


class _RateLimitTypeEvidence(str, Enum):
    """What a ``__cause__`` walk found about an exception's 429-ness.

    ``TERMINAL_EXHAUSTION`` and ``OTHER_HTTP_STATUS`` are both "not a transient
    429", but they are distinct findings: the first is authoritative about the
    condition, the second only about this link's status.
    """

    TYPED_429 = "typed_429"
    TERMINAL_EXHAUSTION = "terminal_exhaustion"
    OTHER_HTTP_STATUS = "other_http_status"
    NONE = "none"


def _rate_limit_type_evidence(exc: BaseException) -> _RateLimitTypeEvidence:
    """Walk *exc* and its causes for 429 evidence carried by type or status.

    The walk is bounded by :data:`_EXCEPTION_CAUSE_DEPTH` because the outer
    controller never sees the provider's exception directly.
    ``litellm.exceptions.RateLimitError`` subclasses ``openai.RateLimitError``,
    so one ``isinstance`` covers every provider litellm routes, and
    ``status_code == 429`` catches a bare ``APIStatusError``.

    :class:`AllApiKeysExhaustedError` stops the walk: it chains the provider's
    own 429 as its ``__cause__`` but every rotatable key is spent and
    :meth:`LLMClient._rotate_key` only ever advances its index, so the condition
    never clears.
    """
    candidate: BaseException | None = exc
    saw_http_status = False
    for _ in range(_EXCEPTION_CAUSE_DEPTH):
        if candidate is None:
            break
        if isinstance(candidate, openai.RateLimitError):
            return _RateLimitTypeEvidence.TYPED_429
        status = getattr(candidate, "status_code", None)
        if status == 429:
            return _RateLimitTypeEvidence.TYPED_429
        if isinstance(status, int):
            saw_http_status = True
        if isinstance(candidate, AllApiKeysExhaustedError):
            return _RateLimitTypeEvidence.TERMINAL_EXHAUSTION
        cause = candidate.__cause__
        candidate = cause if cause is not candidate else None

    if saw_http_status:
        return _RateLimitTypeEvidence.OTHER_HTTP_STATUS
    return _RateLimitTypeEvidence.NONE


def is_typed_rate_limit_exception(exc: BaseException) -> bool:
    """True when *exc* or one of its causes carries a 429 by type or status.

    Message text is never consulted, which is what makes this predicate usable
    as the gate on ``TerminationReason.RATE_LIMIT`` — and through it on
    excluding a trial from a benchmark's denominator. A rate limit that reaches
    us as prose only is a harness deficiency, not evidence.
    """
    return _rate_limit_type_evidence(exc) is _RateLimitTypeEvidence.TYPED_429


def _build_rate_limit_wait(probe: RateLimitProbeConfig) -> wait_base:
    """The probe's 429 wait strategy: a fixed interval plus symmetric jitter.

    Without jitter every client blocked at the provider's cap retries in
    lockstep — burst, all rejected, wait, burst — which biases the served-
    throughput measurement the mode exists to produce and is harsher on the
    provider than steady polling. The jitter is uniform on
    ``[-f x interval, +f x interval]`` with ``f = jitter_fraction < 1``, so
    every wait stays positive and the *mean* interval is exactly
    ``retry_interval_s``: the ``1 / retry_interval_s`` poll-rate inversion the
    estimator does still holds in expectation.

    ``jitter_fraction == 0`` returns the bare :func:`tenacity.wait_fixed`, i.e.
    byte-for-byte the pre-jitter fixed-interval behaviour.
    """
    fixed = wait_fixed(probe.retry_interval_s)
    spread = probe.retry_interval_s * probe.jitter_fraction
    if spread == 0.0:
        return fixed
    return wait_combine(fixed, wait_random(min=-spread, max=spread))


# Native finish_reason values that indicate the upstream provider produced
# an unrecoverable response and OpenRouter / litellm wrapped it in a stub
# completion. These are surfaced via litellm's
# :class:`~litellm.types.utils.Choices` constructor: when the original
# ``finish_reason`` (e.g. ``"MALFORMED_FUNCTION_CALL"``) maps to a
# different post-OpenAI value (``"stop"``), the original is preserved in
# ``provider_specific_fields["native_finish_reason"]``. The post-mapped
# ``finish_reason`` then looks like a normal completion to downstream
# consumers, hiding the failure.
#
# Empirical fingerprint of the stub envelope on the OpenRouter Gemini
# route: ``message.content`` is the canned filler ``"I'll help you with
# that."``, ``message.tool_calls`` may be populated with partial /
# fabricated arguments, ``usage`` reports zero tokens, and the
# ``reasoning_details`` block carries a placeholder UUID
# ``e24830a7-5cd6-42fe-998b-ee539e72b9c3``. Treating this as a normal
# turn poisons the trajectory: the placeholder reasoning is replayed on
# subsequent turns, and the partial tool call drops fields the model had
# correctly identified earlier in context.
#
# 2026-04-29 OTS evaluation evidence: 320 of 550 Gemini 3.1 Pro trials
# (58%) and 192 of 550 Gemini 3 Flash trials (35%) carried this
# fingerprint. Pass-rate gap WITH-vs-WITHOUT the marker: −14.3pp on Pro,
# −3.9pp on Flash. See ``plans/eval_output_new_diagnosis.md``.
_SYNTHETIC_ENVELOPE_NATIVE_REASONS: frozenset[str] = frozenset(
    {
        # Gemini / Vertex AI — model failed to produce a valid response.
        "MALFORMED_FUNCTION_CALL",
        "MALFORMED_RESPONSE",
        "TOO_MANY_TOOL_CALLS",
        "FINISH_REASON_UNSPECIFIED",
        # OpenRouter generic upstream-error wrapper.
        "error",
        # Cohere upstream error.
        "ERROR",
        # Zhipu GLM transient network error.
        "network_error",
    }
)


def _detect_synthetic_envelope(response: Any) -> str | None:
    """Detect OpenRouter / provider synthetic-error envelopes.

    When an upstream provider produces an unrecoverable response (e.g.
    Gemini ``MALFORMED_FUNCTION_CALL``, ``TOO_MANY_TOOL_CALLS``, or
    OpenRouter generic upstream errors), the wire response that reaches
    litellm has the shape of a normal completion — ``finish_reason``
    post-mapped to ``"stop"``, ``message.content`` carrying canned
    filler, optional ``tool_calls`` with partial arguments, and zero-
    token ``usage``. The original failure name survives in
    ``choice.provider_specific_fields["native_finish_reason"]`` because
    litellm's :class:`Choices` constructor stores the unmapped value
    there whenever it differed from the post-map result.

    Treating these as legitimate turns silently corrupts trajectories
    and (because the placeholder reasoning block is echoed back by
    :class:`GeminiReasoningCodec.encode_for_replay`) poisons every
    subsequent turn. :meth:`LLMClient.generate` raises
    :class:`RuntimeError` on a non-``None`` return so its
    :class:`tenacity.Retrying` controller re-attempts the call.

    Returns
    -------
    The offending ``native_finish_reason`` value, or ``None`` when the
    response is clean / cannot be inspected. Defensively swallows
    inspection errors so a malformed mock object surfaces as ``None``
    rather than masking a real test failure.
    """
    try:
        choice = response.choices[0]
    except (AttributeError, IndexError, TypeError):
        return None
    psf = getattr(choice, "provider_specific_fields", None)
    if not isinstance(psf, dict):
        return None
    native = psf.get("native_finish_reason")
    if isinstance(native, str) and native in _SYNTHETIC_ENVELOPE_NATIVE_REASONS:
        return native
    return None


def _litellm_response_cost(response: Any) -> float | None:
    """Pull per-call cost in USD from litellm.

    Prefers ``response._hidden_params['response_cost']`` (cached by litellm
    after every successful completion — see litellm ARCHITECTURE.md "Cost
    Attribution Flow"). Falls back to :func:`litellm.completion_cost`, which
    re-derives the same value but raises for models outside litellm's pricing
    catalog.

    Returns ``None`` when litellm cannot price the call so the caller can
    fall back to the bundled :data:`tolokaforge.core.pricing.MODEL_PRICING`
    table — the offline-reanalysis source of truth for our harness.
    """
    hidden = getattr(response, "_hidden_params", None)
    if isinstance(hidden, dict):
        cached = hidden.get("response_cost")
        if cached is not None:
            try:
                value = float(cached)
            except (TypeError, ValueError):
                value = None
            if value is not None and value > 0:
                return value
    try:
        value = float(litellm.completion_cost(completion_response=response))
    except Exception as exc:  # litellm raises NotFoundError on unknown models
        _module_logger.debug("litellm_cost_unavailable", error=str(exc))
        return None
    return value if value > 0 else None


class GenerationResult:
    """Result from LLM generation.

    ``usage`` is always a :class:`Usage` dataclass — never a dict. Callers
    that previously read ``token_usage["input"]`` / ``token_usage["output"]``
    now read ``usage.prompt_tokens`` / ``usage.completion_tokens`` (plus the
    new cache/reasoning counters). See Stage 5 of
    [`plans/llm_reasoning_and_observability_fix.md`](../../../plans/llm_reasoning_and_observability_fix.md)
    for the full migration rationale.
    """

    def __init__(
        self,
        text: str,
        tool_calls: list[ToolCall] | None = None,
        usage: Usage | None = None,
        latency_s: float = 0.0,
        cost_usd: float | None = None,
        reasoning: StructuredReasoning | None = None,
        effective_system_prompt: str | None = None,
        openrouter_generation_id: str | None = None,
        finish_reason: str | None = None,
    ):
        self.text = text
        self.tool_calls = tool_calls or []
        # Full, normalised usage — default to the empty Usage() so callers
        # never have to None-check before reading prompt/completion counters.
        self.usage: Usage = usage if usage is not None else Usage()
        self.latency_s = latency_s
        # None means pricing unknown for this model
        self.cost_usd = cost_usd
        # Structured thinking/reasoning blocks (see
        # tolokaforge.core.llm.reasoning).
        self.reasoning = reasoning
        # Final system prompt after policy enrichment
        self.effective_system_prompt = effective_system_prompt
        # OpenRouter's id for this generation; None on every other route. Also
        # on ``usage.calls[-1]`` — carried here so the turn loop can stamp it
        # onto the assistant message without reaching into the usage record.
        self.openrouter_generation_id = openrouter_generation_id
        # litellm's post-mapped ``choice.finish_reason`` (e.g. ``"stop"``,
        # ``"length"``, ``"tool_calls"``). ``"length"`` on a content-carrying
        # result means the provider truncated the response at ``max_tokens``
        # — the engine loop's output-length retry seam reads it to decide
        # whether to resample with feedback (see
        # :attr:`ModelCapabilities.output_length_retry_count`). ``None`` when
        # the response carries no finish_reason at all.
        self.finish_reason = finish_reason
        # Defects of the attempts discarded before this reply was accepted.
        # Stamped only by ``UserSimulator._llm_reply``; every other producer
        # of a result leaves it empty.
        self.guard_rejections: tuple[ReplyDefect, ...] = ()
        # One ``ParserError`` per ``tool_call.function.arguments`` string that
        # ``_try_parse_tool_arguments`` could not decode. Stamped by
        # ``LLMClient._assemble_result`` alongside the ``{}`` coercion the
        # tolerant parser applies. Read by ``ToolCallingLoop._run_turn``'s
        # parser-error retry seam to decide whether to discard the response,
        # append ``role=user`` parse-error feedback naming the failing tools,
        # and resample under ``LoopConfig.parser_error_retry_count``.
        self.parser_errors: tuple[ParserError, ...] = ()
        # True iff ``UserSimulator._llm_reply`` substituted the fixed filler
        # for a tool-call-only reply with no text. Callers whose downstream
        # semantics depend on the model having written the text (the
        # bootstrap seed the agent is graded against) refuse on this flag
        # instead of accepting the engine's own words as the turn.
        self.filler_substituted: bool = False


class LLMClient:
    """Provider-agnostic LLM client using LiteLLM."""

    def __init__(
        self,
        config: ModelConfig,
        *,
        rate_limit_probe: RateLimitProbeConfig | None = None,
    ):
        self.config = config
        self.provider = (config.provider or "").lower()
        self._provider_binding = get_provider_binding(self.provider)
        self.model_name = self._format_model_name()
        # Parameters an overlay admits for a model litellm's map does not carry.
        # Empty for every model it does: the kwarg is then omitted entirely.
        self.allowed_openai_params = allowed_openai_params(self.model_name, self.config.provider)
        self.capabilities = build_capabilities(
            self.config.name,
            self.config.provider,
            overrides=self.config.capabilities,
        )
        self.logger = get_logger("llm_client")
        if self.config.capabilities:
            self.logger.info(
                "Model capability overrides applied",
                overrides=self.config.capabilities,
            )
        self.logger.info(
            "Model capabilities configured",
            schema_sanitizer=type(self.capabilities.schema_sanitizer).__name__,
            prompt_policy=type(self.capabilities.prompt_policy).__name__,
            content_policy=type(self.capabilities.content_policy).__name__,
        )
        # Load API key chain for rotation on key exhaustion
        self._api_keys = self._load_api_keys()
        self._current_key_index = 0

        # Optional OpenAI-compatible gateway in front of every provider. Held
        # as ``None`` both when unconfigured and when configured but out of
        # scope for this provider, so downstream checks are a single
        # ``is None`` test. See ``tolokaforge/core/llm/proxy.py``.
        self._proxy = resolve_proxy_config()
        if self._proxy is not None and not self._proxy.applies_to(self.provider):
            if not self._provider_binding.unroutable:
                # Warn rather than drop quietly: a deployment that configured a
                # gateway for compliance reasons needs to see which roles still
                # reach providers directly.
                self.logger.warning(
                    "Gateway configured but this provider is out of scope; "
                    "calling provider directly",
                    base_url=self._proxy.base_url,
                    provider=self.provider,
                )
            self._proxy = None

        # A gateway that does not serve this model must not intercept it: fall back
        # to the direct provider rather than post a name it cannot route.
        # docs/LLM_LAYER.md § Speaking to the gateway.
        self._gateway_route: ResolvedGatewayRoute | None = None
        self._gateway_route_kind: RouteKind | None = None
        # Declared here rather than created on the first pin drop, so the flag stays
        # inside the client's attribute set. See the drop site in _call_with_retry.
        self._pin_drop_warned: bool = False
        if self._proxy is not None:
            catalog = fetch_gateway_catalog(self._proxy)
            if catalog is None:
                # Unreadable is NOT "absent": silently leaving the gateway would be
                # the unattributed-spend outcome this transport exists to prevent.
                self.logger.warning(
                    "Gateway catalog unreadable; routing on the untranslated name",
                    base_url=self._proxy.base_url,
                    model=self.model_name,
                )
            else:
                # A readable catalog is never empty here: the fetch maps an empty
                # answer to None, so resolver-None below can only mean "omitted".
                self._gateway_route = resolve_gateway_route(
                    self.model_name,
                    catalog,
                    self._proxy.preferred_route,
                    trust_namespace_wildcards=self._proxy.trust_namespace_wildcards,
                )
                self._gateway_route_kind = (
                    self._gateway_route.kind if self._gateway_route is not None else None
                )
                if self._gateway_route_kind == "wildcard":
                    self.logger.info(
                        "Gateway route resolved via the model namespace wildcard",
                        base_url=self._proxy.base_url,
                        model=self.model_name,
                    )
                if self._gateway_route is None:
                    self.logger.warning(
                        "Gateway does not serve this model; calling the provider directly",
                        base_url=self._proxy.base_url,
                        model=self.model_name,
                    )
                    self._proxy = None

        self._openrouter_headers = (
            self._configure_openrouter_headers() if self.provider.startswith("openrouter") else {}
        )
        if self._proxy is not None:
            self.logger.info(
                "Routing LLM calls through gateway",
                base_url=self._proxy.base_url,
                provider=self.provider,
                static_header_count=len(self._proxy.headers),
                request_id_header=self._proxy.request_id_header,
            )
            if self._proxy.api_key is None:
                self.logger.warning(
                    "Gateway has no LLM_PROXY_API_KEY; litellm will forward the "
                    "PROVIDER credential to the gateway host. Set LLM_PROXY_API_KEY "
                    "unless this gateway authenticates by network position.",
                    base_url=self._proxy.base_url,
                )
        elif self.provider.startswith("openrouter"):
            self._configure_openrouter_base_url()
        elif self._provider_binding.endpoint and self._provider_binding.api_base_env:
            os.environ.setdefault(
                self._provider_binding.api_base_env, self._provider_binding.endpoint
            )

        self._api_call_timeout_s = self._load_api_timeout()
        self._api_timeout_retries = self._load_api_timeout_retries()
        self._api_call_wall_timeout_s = self._load_api_wall_timeout()
        self._rate_limit_probe = self._load_rate_limit_probe(rate_limit_probe)

        self._compiled_rate_limit_patterns = compile_rate_limit_patterns(
            self._provider_binding.rate_limit_patterns
        )

        # Sleep hook the outer-retry ``Retrying`` controller in ``generate``
        # binds per call. Tests replace it with a no-op to make the 5-attempt
        # ``wait_exponential(multiplier=2, min=4, max=60)`` backoff instant.
        self._retry_sleep: Callable[[float], None] = tenacity.nap.sleep

    # ------------------------------------------------------------------
    # Model capabilities — preserved entry point for tests that reassign it
    # ------------------------------------------------------------------

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._capabilities

    @capabilities.setter
    def capabilities(self, value: ModelCapabilities) -> None:
        self._capabilities = value

    def sanitize_tools_for_execution(
        self, tools: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        """Return ``{tool_name: parameters_schema}`` after running this client's
        :class:`~tolokaforge.core.llm.schema_sanitizer.ToolSchemaSanitizer` over
        ``tools``. The per-tool schema in the returned map is what the model
        actually saw for that tool, and is the schema
        :class:`~tolokaforge.tools.registry.ToolExecutor` validates argument
        dicts against when
        :attr:`~tolokaforge.core.loop.ToolCallingLoop.validation_schemas_by_tool`
        is wired to this method's result. See
        ``docs/LLM_LAYER.md`` § ``schema_sanitizer``.
        """
        sanitised = self.capabilities.schema_sanitizer.sanitize(tools)
        return {
            t["function"]["name"]: t["function"]["parameters"]
            for t in sanitised
            if isinstance(t.get("function"), dict)
            and isinstance(t["function"].get("parameters"), dict)
        }

    # ------------------------------------------------------------------
    # Rate-limit classification — per-provider, closes over the binding's
    # compiled patterns so downstream callers never reach for internals.
    # ------------------------------------------------------------------

    def _is_rate_limit_exception(self, exc: BaseException) -> bool:
        """True when *exc* is a **transient** upstream 429.

        Three tiers, strongest evidence first:

        1. **Type / status**, and 2. the **terminal-condition veto** — both from
           :func:`_rate_limit_type_evidence`. The veto keeps key exhaustion on
           the ordinary bounded-exponential branch instead of probe mode's
           multi-hour fixed-interval budget.
        3. **Anchored text**, last resort — the client's provider binding
           carries the shipped default pattern list (see
           :data:`tolokaforge.core.llm.providers.DEFAULT_RATE_LIMIT_PATTERNS`).
           It runs only when *no* link in the chain carried an HTTP status at
           all, i.e. for the shape the tier exists for: a wrapper that
           stringified the provider error instead of chaining it. An
           authoritative non-429 status beats prose, because the outermost
           message is ``RuntimeError(f"LLM API call failed: {e}")`` and ``e``'s
           message can embed a provider response body that echoes request
           content — a task conversation about rate limiting would otherwise
           hand a deterministic 400 the multi-hour budget. This is deliberately
           *not* a narrowing of tiers 1-2: an untyped chain still text-matches,
           so a real 429 that arrives only as prose is still absorbed.

        Used by the rate-limit probe controller, which asks about
        *transience* — a different question from
        :func:`is_typed_rate_limit_exception`, which asks what the error can
        be proven to be.
        """
        evidence = _rate_limit_type_evidence(exc)
        if evidence is _RateLimitTypeEvidence.TYPED_429:
            return True
        if evidence in (
            _RateLimitTypeEvidence.TERMINAL_EXHAUSTION,
            _RateLimitTypeEvidence.OTHER_HTTP_STATUS,
        ):
            return False
        return matches_rate_limit_text(str(exc), self._compiled_rate_limit_patterns)

    def _is_rate_limit_retry_state(self, retry_state: RetryCallState) -> bool:
        """:meth:`_is_rate_limit_exception` for a tenacity retry state.

        ``outcome`` is ``None`` before the first attempt has produced anything;
        tenacity does not reach ``stop`` / ``wait`` / ``before_sleep`` in that
        state, but the hooks stay total so a tenacity change cannot turn this
        into an ``AttributeError`` mid-run.
        """
        outcome = retry_state.outcome
        if outcome is None:
            return False
        exc = outcome.exception()
        return exc is not None and self._is_rate_limit_exception(exc)

    def classify_loop_error(self, exc: Exception) -> TerminationDecision:
        """Classify a turn-loop exception against this client's rate-limit patterns.

        Bound entry point the loop and the judge consume as
        ``classify_error=llm_client.classify_loop_error``. Closes over
        :attr:`_compiled_rate_limit_patterns` so provider-specific text shapes
        stay behind the client's public surface.
        """
        # Deferred to break the loop.py -> client.py -> loop.py import cycle.
        from tolokaforge.core.loop import classify_loop_error

        return classify_loop_error(exc, self._compiled_rate_limit_patterns)

    # ------------------------------------------------------------------
    # API key handling
    # ------------------------------------------------------------------

    def _load_api_keys(self) -> list[str]:
        """Load API keys for rotation via SecretManager.

        Routes through ``tolokaforge.secrets.get_default()`` so behaviour stays
        identical between host and runner-container processes (the runner
        bootstraps a SecretManager from ``TOLOKAFORGE_SECRETS_JSON``). The
        rotation-list and primary env-var names come from the provider's
        binding (:attr:`ProviderBinding.api_keys_env` /
        :attr:`ProviderBinding.api_key_env`). ``OPENROUTER_KEY_FILE`` is *not*
        a secret — only the key file's *path* is a config knob, so it stays
        as a non-credential env var. The keys themselves are read from disk
        by this code, and only for the OpenRouter binding: a stray ``keys.txt``
        in cwd would otherwise leak OpenRouter keys into every provider's
        rotation list and, on the next :meth:`_rotate_key`, into that
        provider's ``api_key_env`` env var.
        """
        from tolokaforge.secrets import get_default

        secrets = get_default()
        binding = self._provider_binding

        if binding.api_keys_env:
            keys_str = secrets.get_secret(binding.api_keys_env) or ""
            if keys_str:
                keys = [k.strip() for k in keys_str.split(",") if k.strip()]
                if keys:
                    self.logger.info(
                        f"Loaded API keys from {binding.api_keys_env}",
                        key_count=len(keys),
                    )
                    return keys

        if binding.key_file_env is not None:
            key_file = os.environ.get(binding.key_file_env, "keys.txt")
            if os.path.exists(key_file):
                keys = []
                with open(key_file) as f:
                    for line in f:
                        line = line.split("#")[0].strip()
                        if line:
                            # Take first field (before comma) as OpenRouter key
                            or_key = line.split(",")[0].strip()
                            if or_key:
                                keys.append(or_key)
                if keys:
                    self.logger.info(
                        "Loaded API keys from file",
                        key_file=key_file,
                        key_count=len(keys),
                    )
                    return keys

        if binding.api_key_env:
            key = secrets.get_secret(binding.api_key_env) or ""
            if key:
                return [key]
        return []

    def _rotate_key(self) -> bool:
        """Rotate to the next available API key.

        Republishes the fresh key into the environment under
        :attr:`ProviderBinding.api_key_env` so a provider that reads the env
        var per call (litellm's default path when no ``api_key`` kwarg is
        pinned) picks the rotated value on the next attempt.
        """
        binding = self._provider_binding
        if self._current_key_index + 1 < len(self._api_keys):
            self._current_key_index += 1
            new_key = self._api_keys[self._current_key_index]
            if binding.api_key_env:
                os.environ[binding.api_key_env] = new_key
            self.logger.info(
                "Rotated to API key",
                key_suffix=new_key[-6:] if len(new_key) >= 6 else "***",
                index=self._current_key_index,
            )
            return True
        return False

    def _load_api_timeout(self) -> float:
        """Resolve the per-call timeout.

        Priority: env var ``TOLOKAFORGE_LLM_API_CALL_TIMEOUT_S`` (operational
        override) → ``self.capabilities.api_call_timeout_s`` (per-model
        preset) → :data:`DEFAULT_API_CALL_TIMEOUT_S`.
        """
        env_value = self._parse_env_positive_float(
            "TOLOKAFORGE_LLM_API_CALL_TIMEOUT_S",
            default=None,
        )
        if env_value is not None:
            return env_value
        if self.capabilities.api_call_timeout_s is not None:
            return self.capabilities.api_call_timeout_s
        return DEFAULT_API_CALL_TIMEOUT_S

    def _load_api_timeout_retries(self) -> int:
        """Resolve the per-call retry budget.

        Priority: env var ``TOLOKAFORGE_LLM_API_CALL_RETRIES`` (operational
        override) → ``self.capabilities.api_call_retries`` (per-model
        preset) → :data:`DEFAULT_API_TIMEOUT_RETRIES`.
        """
        env_value = self._parse_env_non_negative_int(
            "TOLOKAFORGE_LLM_API_CALL_RETRIES",
            default=None,
        )
        if env_value is not None:
            return env_value
        if self.capabilities.api_call_retries is not None:
            return self.capabilities.api_call_retries
        return DEFAULT_API_TIMEOUT_RETRIES

    def _load_api_wall_timeout(self) -> float | None:
        """Resolve the hard wall-clock ceiling for a single call.

        Priority: env var ``TOLOKAFORGE_LLM_API_CALL_WALL_TIMEOUT_S``
        (operational override) → ``self.capabilities.api_call_wall_timeout_s``
        (per-model preset) → :data:`DEFAULT_API_CALL_WALL_TIMEOUT_S`.

        ``None`` disables the wall-clock abort (the backward-compatible
        default) — the call is then bounded only by the per-read
        ``api_call_timeout_s``.
        """
        env_value = self._parse_env_positive_float(
            "TOLOKAFORGE_LLM_API_CALL_WALL_TIMEOUT_S",
            default=None,
        )
        if env_value is not None:
            return env_value
        if self.capabilities.api_call_wall_timeout_s is not None:
            return self.capabilities.api_call_wall_timeout_s
        return DEFAULT_API_CALL_WALL_TIMEOUT_S

    def _load_rate_limit_probe(
        self, configured: RateLimitProbeConfig | None
    ) -> RateLimitProbeConfig | None:
        """Resolve rate-limit probe mode for this client.

        The passed block is the *only* activation channel — there is
        deliberately no env override. An env var could not reach the agent
        client (the orchestrator always passes an explicit block, so the var
        would be dead there) while it *would* reach every site that omits the
        kwarg: the rubric judge, a ``--fallback-models`` chain, and
        :func:`~tolokaforge.core.run_trial.run_trial`. Those are exactly the
        paths that must never probe, and an env-armed probe would also skip
        both budget assertions and the fallback-chain rejection, which only run
        on the config block. ``None`` therefore means "never probe" rather than
        "unspecified".

        Returns ``None`` for a missing or disabled block, which is what keeps
        :meth:`_build_retrying` on its default path.
        """
        if configured is None or not configured.enabled:
            return None
        self.logger.info(
            "Rate-limit probe mode enabled",
            retry_interval_s=configured.retry_interval_s,
            jitter_fraction=configured.jitter_fraction,
            per_call_budget_s=configured.per_call_budget_s,
            model=self.model_name,
        )
        return configured

    def _parse_env_positive_float(self, name: str, default: float | None) -> float | None:
        raw = os.getenv(name)
        if raw is None:
            return default
        try:
            value = float(raw)
            if value <= 0:
                raise ValueError("must be positive")
            return value
        except ValueError:
            self.logger.warning(
                "Invalid env-var float; ignoring", env_var=name, value=raw, default=default
            )
            return default

    def _parse_env_non_negative_int(self, name: str, default: int | None) -> int | None:
        raw = os.getenv(name)
        if raw is None:
            return default
        try:
            value = int(raw)
            if value < 0:
                raise ValueError("must be non-negative")
            return value
        except ValueError:
            self.logger.warning(
                "Invalid env-var int; ignoring", env_var=name, value=raw, default=default
            )
            return default

    def _configure_openrouter_headers(self) -> dict[str, str]:
        """Ensure OpenRouter requests include the required headers."""
        existing_headers = dict(getattr(litellm, "openai_headers", {}) or {})

        referer = os.getenv(
            "TOLOKAFORGE_OPENROUTER_REFERER", "https://github.com/Toloka-F/tolokaforge"
        )
        title = os.getenv("TOLOKAFORGE_OPENROUTER_TITLE", "Tolokaforge Evaluation")

        existing_headers.setdefault("HTTP-Referer", referer)
        existing_headers.setdefault("X-Title", title)

        opt_out_pref = os.getenv("TOLOKAFORGE_OPENROUTER_OPT_OUT", "true").lower()
        if opt_out_pref in {"1", "true", "yes", "on"}:
            existing_headers.setdefault("X-Data-Collection-Opt-Out", "true")

        litellm.openai_headers = existing_headers
        return existing_headers

    def _configure_openrouter_base_url(self) -> None:
        """Propagate OpenRouter base URL overrides to LiteLLM."""
        from tolokaforge.secrets import get_default

        secrets = get_default()
        base_url = secrets.get_secret("OPENROUTER_BASE_URL") or secrets.get_secret(
            "OPENROUTER_API_BASE"
        )

        if not base_url:
            return

        os.environ.setdefault("OPENROUTER_API_BASE", base_url)
        self._openrouter_base_url = base_url

    def _format_model_name(self) -> str:
        """Format model name for LiteLLM."""
        if self.config.name.startswith(f"{self.config.provider}/"):
            return self.config.name

        if self._provider_binding.format_model_name_bare:
            return self.config.name

        return f"{self.config.provider}/{self.config.name}"

    # ------------------------------------------------------------------
    # JSON/argument repair helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _repair_json_like(raw: str) -> str:
        """Apply lightweight repairs to near-JSON argument payloads."""
        repaired = raw.strip()

        if repaired.startswith("```"):
            repaired = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", repaired)
            repaired = repaired.replace("```", "").strip()

        repaired = repaired.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")

        repaired = re.sub(
            r"([{\[,]\s*)([A-Za-z_][A-Za-z0-9_-]*)(\s*:)",
            r'\1"\2"\3',
            repaired,
        )

        open_braces = repaired.count("{")
        close_braces = repaired.count("}")
        if open_braces > close_braces:
            repaired = repaired + ("}" * (open_braces - close_braces))

        return repaired

    def _try_parse_tool_arguments(
        self, tool_name: str, raw_args: Any
    ) -> tuple[dict[str, Any], str | None]:
        """Parse model-emitted tool arguments with tolerant fallbacks and
        report whether every parser exhausted.

        Returns ``(parsed, error_reason)``. ``error_reason`` is ``None`` on
        any recovery branch that yielded a dict (including the "provider
        legitimately sent no args" branch: ``None`` / empty string / non-str)
        and on the shape-mismatch branch (JSON parsed but yielded a list or
        scalar rather than a dict — a shape issue rather than a parse
        failure). It is a short human phrase (``"Unable to parse with
        JSON/YAML fallbacks"``) only when every parser in the JSON / YAML /
        repair-JSON / repair-YAML ladder failed to yield any dict.

        Read by :meth:`_assemble_result` to populate
        :attr:`GenerationResult.parser_errors` alongside the tolerant ``{}``
        coercion the engine's parser-error retry seam consumes.
        """

        def _normalize(parsed_args: dict[str, Any]) -> dict[str, Any]:
            normalized = dict(parsed_args)
            if tool_name in {"browser", "mobile"}:
                actions = normalized.get("actions")
                if isinstance(actions, str):
                    cleaned = re.sub(r"</?invoke>", "", actions).strip()
                    for parser in (json.loads, yaml.safe_load):
                        try:
                            decoded = parser(cleaned)
                            if isinstance(decoded, list):
                                normalized["actions"] = decoded
                                self.logger.warning(
                                    "Recovered malformed browser/mobile actions payload",
                                    tool=tool_name,
                                )
                                break
                        except Exception:
                            continue
            return normalized

        if isinstance(raw_args, dict):
            return _normalize(raw_args), None
        if raw_args is None or not isinstance(raw_args, str):
            return {}, None

        args_str = raw_args.strip()
        if not args_str:
            return {}, None

        try:
            parsed = json.loads(args_str)
            return (_normalize(parsed) if isinstance(parsed, dict) else {}), None
        except json.JSONDecodeError:
            pass

        try:
            parsed = yaml.safe_load(args_str)
            if isinstance(parsed, dict):
                self.logger.warning("Recovered malformed tool arguments", tool=tool_name)
                return _normalize(parsed), None
        except Exception:
            pass

        repaired = self._repair_json_like(args_str)
        parsers: list[Callable[[str], Any]] = [json.loads, yaml.safe_load]
        for parser in parsers:
            try:
                parsed = parser(repaired)
                if isinstance(parsed, dict):
                    self.logger.warning("Recovered malformed tool arguments", tool=tool_name)
                    return _normalize(parsed), None
            except Exception:
                continue

        self.logger.warning(
            "Failed to parse tool arguments",
            tool=tool_name,
            error="Unable to parse with JSON/YAML fallbacks",
        )
        return {}, "Unable to parse with JSON/YAML fallbacks"

    def _parse_tool_arguments(self, tool_name: str, raw_args: Any) -> dict[str, Any]:
        """Tolerant parse of model-emitted tool arguments.

        Thin wrapper over :meth:`_try_parse_tool_arguments` that discards the
        parse-error phrase. Existing callers that only need the ``{}``-coerced
        dict use this signature; :meth:`_assemble_result` calls the ``_try``
        variant directly to stamp
        :attr:`GenerationResult.parser_errors`.
        """
        parsed, _ = self._try_parse_tool_arguments(tool_name, raw_args)
        return parsed

    # ------------------------------------------------------------------
    # Tool content adaptation
    # ------------------------------------------------------------------

    def _tool_block_format(self) -> str:
        """Content block format for tool results."""
        return self.capabilities.content_policy.format

    def supports_tool_image_blocks(self) -> bool:
        """Whether tool-result image blocks are supported by the target model."""
        return self.capabilities.content_policy.supports_images

    def _tool_blocks_to_text(self, blocks: list[dict[str, Any]]) -> str:
        """Flatten content blocks into a text-only summary."""
        texts: list[str] = []
        has_image = False
        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text", "")
                if isinstance(text, str) and text.strip():
                    texts.append(text)
            elif btype in {"image", "image_url"}:
                has_image = True
        if texts:
            return "\n".join(texts)
        if has_image:
            return "Screenshot captured."
        return ""

    def _adapt_tool_content_blocks(self, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert tool content blocks into the provider-appropriate format."""
        target = self._tool_block_format()
        texts: list[str] = []
        images: list[dict[str, str]] = []

        def _push_image(data: str | None, media_type: str | None, url: str | None):
            if data:
                images.append({"data": data, "media_type": media_type or "image/png"})
            elif url:
                images.append({"url": url, "media_type": media_type or "image/png"})

        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text", "")
                if isinstance(text, str) and text.strip():
                    texts.append(text)
            elif btype == "image":
                source = block.get("source", {}) if isinstance(block.get("source"), dict) else {}
                stype = source.get("type")
                if stype == "base64":
                    _push_image(source.get("data"), source.get("media_type"), None)
                elif stype == "url":
                    _push_image(None, source.get("media_type"), source.get("url"))
            elif btype == "image_url":
                image_url = (
                    block.get("image_url", {}) if isinstance(block.get("image_url"), dict) else {}
                )
                url = image_url.get("url")
                if isinstance(url, str) and url.startswith("data:image/"):
                    header, _, data = url.partition(",")
                    media_type = (
                        header.split(";")[0].replace("data:", "") if header else "image/png"
                    )
                    _push_image(data or None, media_type, None)
                elif isinstance(url, str) and url:
                    _push_image(None, None, url)

        if target == "anthropic":
            adapted: list[dict[str, Any]] = []
            for text in texts:
                adapted.append({"type": "text", "text": text})
            for img in images:
                if "data" in img:
                    adapted.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": img.get("media_type", "image/png"),
                                "data": img["data"],
                            },
                        }
                    )
                elif "url" in img:
                    adapted.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "url",
                                "media_type": img.get("media_type", "image/png"),
                                "url": img["url"],
                            },
                        }
                    )
            if adapted:
                return adapted
            return [{"type": "text", "text": ""}]

        # OpenAI-compatible blocks
        adapted = []
        for text in texts:
            adapted.append({"type": "text", "text": text})
        for img in images:
            if "data" in img:
                url = f"data:{img.get('media_type', 'image/png')};base64,{img['data']}"
                adapted.append({"type": "image_url", "image_url": {"url": url}})
            elif "url" in img:
                adapted.append({"type": "image_url", "image_url": {"url": img["url"]}})
        if adapted:
            return adapted
        return [{"type": "text", "text": ""}]

    # ------------------------------------------------------------------
    # Message conversion
    # ------------------------------------------------------------------

    def _convert_messages(
        self,
        system: str | list[dict[str, Any]] | None,
        messages: list[Message],
    ) -> list[dict[str, Any]]:
        """Convert our Message format to LiteLLM format.

        ``system`` is forwarded verbatim to LiteLLM as ``messages[0].content``.
        It may be either:

        * a plain ``str`` (non-Anthropic presets / ``NoCache``);
        * a list of content-blocks already rewritten by
          :class:`~tolokaforge.core.llm.cache_policy.AnthropicEphemeralCache`
          (Anthropic prompt-caching wire shape — marker on the last block).

        LiteLLM forwards the list-of-content-blocks form untouched to the
        Anthropic provider, which is exactly the shape the Messages API
        expects. See [`docs/LLM_LAYER.md`](../../../docs/LLM_LAYER.md)
        § ``cache_policy`` for the contract.

        User tool calls are kept in Message objects so ``required_actions`` can
        match them, but stripped here since most LLM APIs don't support tool_use
        from USER role.
        """
        litellm_messages: list[dict[str, Any]] = []

        if system:
            litellm_messages.append({"role": "system", "content": system})

        for msg in messages:
            litellm_msg: dict[str, Any] = {"role": msg.role.value}

            if msg.role == MessageRole.TOOL:
                if msg.content_blocks:
                    if self.supports_tool_image_blocks():
                        litellm_msg["content"] = self._adapt_tool_content_blocks(msg.content_blocks)
                    else:
                        content = self._tool_blocks_to_text(msg.content_blocks)
                        litellm_msg["content"] = content or "{}"
                else:
                    content = msg.content
                    if not content or (isinstance(content, str) and content.strip() == ""):
                        content = "{}"
                    litellm_msg["content"] = content
                litellm_msg["tool_call_id"] = msg.tool_call_id
            elif msg.role == MessageRole.ASSISTANT and msg.tool_calls:
                content = msg.content or ""
                if not content or content.strip() == "":
                    # Bedrock/Nova rejects empty assistant content alongside
                    # ``tool_calls`` while every other provider accepts it.
                    # ``MessageAssemblyPolicy`` decides both whether to
                    # substitute and which string to substitute; the filler
                    # is data on the policy instance because a universal
                    # string poisoned Gemini via few-shot echo-back on
                    # 2026-04-30. See message_assembly_policy.py.
                    policy = self.capabilities.message_assembly_policy
                    if policy.inject_empty_assistant_filler:
                        content = policy.empty_assistant_filler
                    else:
                        content = ""
                litellm_msg["content"] = content
                litellm_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": (
                                json.dumps(tc.arguments)
                                if isinstance(tc.arguments, dict)
                                else tc.arguments
                            ),
                        },
                    }
                    for tc in msg.tool_calls
                ]
            else:
                content = msg.content
                if not content or (isinstance(content, str) and content.strip() == ""):
                    if msg.role == MessageRole.USER:
                        content = "Please continue."
                    else:
                        content = "I understand."
                litellm_msg["content"] = content

            # P4b — splice reasoning-codec replay payload (e.g. Anthropic
            # ``thinking_blocks``) onto assistant dicts. Zero provider-specific
            # branching: the codec Protocol is the only abstraction.
            if msg.role == MessageRole.ASSISTANT and msg.reasoning is not None:
                replay_payload = self.capabilities.reasoning_codec.encode_for_replay(msg.reasoning)
                if replay_payload:
                    litellm_msg.update(replay_payload)

            litellm_messages.append(litellm_msg)

        return litellm_messages

    # ------------------------------------------------------------------
    # generate()
    # ------------------------------------------------------------------

    def generate(
        self,
        system: str | None = None,
        messages: list[Message] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        seed: int | None = None,
        reasoning: ReasoningConfig | None = None,
        top_p: float | None = None,
        observation: LLMCallObservation | None = None,
    ) -> GenerationResult:
        """Generate completion from LLM.

        Drives up to five attempts through a per-call
        :class:`tenacity.Retrying` controller with
        ``stop_after_attempt(5)`` / ``wait_exponential(multiplier=2, min=4,
        max=60)`` and :func:`_should_retry_exception` — the outer semantic
        retry that bounds cascading provider failures (rate limits, 5xx,
        synthetic-envelope re-raises). Under rate-limit probe mode the
        controller instead comes from :meth:`_build_probe_retrying`, which
        retries 429s at a fixed interval against a wall-clock budget and
        leaves every other error class on the five-attempt exponential
        bound. Each attempt runs the four-phase
        request pipeline via :meth:`_generate_once`
        (:meth:`_prepare_prompt_and_tools` -> :meth:`_build_kwargs` ->
        :meth:`_call_with_key_rotation` -> :meth:`_assemble_result`);
        every provider-specific transform lives in a ``ModelCapabilities``
        policy so this orchestrator never branches on provider.

        When ``observation`` is supplied, the controller fires the
        LLM-call trio into ``observation.events`` — ``llm_call_started``
        before each attempt, ``llm_call_finished`` after (with
        ``duration_s`` and ``error`` set on exception), and
        ``llm_retry_scheduled`` in the ``before_sleep`` hook BEFORE the
        backoff so a display can render "next attempt in Xs" while the
        pause is still in flight. ``observation=None`` fires nothing and
        keeps output byte-identical.

        Under probe mode the two ends of the same pair of hooks also record the
        two sides of throughput onto ``observation.probe_stats``: 429s in
        :meth:`_make_before_sleep`, successful calls in
        :meth:`_record_probe_success`.

        Returns a :class:`GenerationResult` with text, tool-calls, full
        :class:`Usage` counters, latency, cost, and structured reasoning.
        """
        messages = messages or []

        if self.provider == "mock":
            return self._mock_generate(messages, tools)

        retrying = self._build_retrying(observation)
        for attempt in retrying:
            with attempt:
                attempt_num = attempt.retry_state.attempt_number
                self._fire_call_started(observation, attempt_num)
                start = time.monotonic()
                try:
                    result = self._generate_once(
                        system=system,
                        messages=messages,
                        tools=tools,
                        tool_choice=tool_choice,
                        temperature=temperature,
                        seed=seed,
                        reasoning=reasoning,
                        top_p=top_p,
                        max_tokens=max_tokens,
                    )
                except BaseException as exc:
                    self._fire_call_finished(
                        observation, attempt_num, time.monotonic() - start, exc, None
                    )
                    raise
                self._fire_call_finished(
                    observation, attempt_num, time.monotonic() - start, None, result
                )
                return result
        raise RuntimeError("Retrying controller exited without a result")

    def _build_retrying(self, observation: LLMCallObservation | None) -> Retrying:
        """Build the per-call outer :class:`Retrying` controller.

        Fresh per call so ``self._retry_sleep`` is read at call time (tests
        stub it after construction) and the ``before_sleep`` closure captures
        the observation of *this* call — the client instance is shared across
        concurrent trials.
        """
        probe = self._rate_limit_probe
        if probe is not None:
            return self._build_probe_retrying(probe, observation)

        return Retrying(
            stop=stop_after_attempt(5),
            wait=wait_exponential(multiplier=2, min=4, max=60),
            retry=retry_if_exception(_should_retry_exception),
            before_sleep=self._make_before_sleep(observation),
            sleep=self._retry_sleep,
            reraise=True,
        )

    def _build_probe_retrying(
        self,
        probe: RateLimitProbeConfig,
        observation: LLMCallObservation | None,
    ) -> Retrying:
        """Build the rate-limit-probe outer :class:`Retrying` controller.

        429s retry at a fixed interval until the per-call wall-clock budget is
        spent; every other error keeps the standard bounded exponential path.
        The split accounting is load-bearing — a hard 5xx must not inherit the
        multi-hour 429 budget, or one dead upstream hangs a whole run leg.

        Non-429 attempts are counted separately from 429 attempts, so a long
        429 stretch can never consume another error class's five attempts.
        tenacity 9.x runs ``wait`` before ``stop`` within one iteration, so the
        ``seen_429`` counter lags the current attempt inside ``wait``; that is
        immaterial because only ``stop`` reads it.

        The 429 wait carries symmetric jitter (see
        :attr:`RateLimitProbeConfig.jitter_fraction`) so blocked clients do not
        retry in lockstep; ``jitter_fraction=0`` collapses it to
        :func:`tenacity.wait_fixed`, the exact fixed interval.
        """
        standard_wait = wait_exponential(multiplier=2, min=4, max=60)
        rate_limit_wait = _build_rate_limit_wait(probe)
        seen_429 = 0

        def _probe_stop(retry_state: RetryCallState) -> bool:
            nonlocal seen_429
            if self._is_rate_limit_retry_state(retry_state):
                seen_429 += 1
                elapsed = retry_state.seconds_since_start or 0.0
                return elapsed >= probe.per_call_budget_s
            return (retry_state.attempt_number - seen_429) >= 5

        def _probe_wait(retry_state: RetryCallState) -> float:
            if self._is_rate_limit_retry_state(retry_state):
                return rate_limit_wait(retry_state)
            return standard_wait(retry_state)

        return Retrying(
            stop=_probe_stop,
            wait=_probe_wait,
            retry=retry_if_exception(_should_retry_exception),
            before_sleep=self._make_before_sleep(observation),
            sleep=self._retry_sleep,
            reraise=True,
        )

    def _make_before_sleep(
        self, observation: LLMCallObservation | None
    ) -> Callable[[RetryCallState], None]:
        """Build the shared ``before_sleep`` hook both controllers install.

        Emits ``llm_retry_scheduled`` and, in probe mode, accumulates the 429
        counters onto the trial's :class:`RateLimitProbeStats`. tenacity only
        runs ``before_sleep`` when ``stop`` returned False, so this fires once
        per *retried* attempt and never after the final one.

        The 429 accounting is keyed by this call's ``role`` and this client's
        model slug — both already in scope here, which is the whole reason the
        recording lives in this hook. It is additionally gated on *this*
        client's probe being active, so a default-path client can never
        contribute exponential waits to ``rate_limit_wait_s``; today the
        conductor derives both from one config, and this keeps the invariant
        local instead of an emergent property of a distant caller.

        :meth:`_record_probe_success` is the exact mirror on the success side,
        with the same key and the same two-part gate.
        """
        log_hook = before_sleep_log(get_logger("llm_retry").logger, logging.WARNING)

        def _before_sleep(retry_state: RetryCallState) -> None:
            log_hook(retry_state)
            next_action = retry_state.next_action
            sleep_s = float(next_action.sleep) if next_action else 0.0
            if observation is None:
                return
            probe_stats = observation.probe_stats
            if (
                probe_stats is not None
                and self._rate_limit_probe is not None
                and self._is_rate_limit_retry_state(retry_state)
            ):
                probe_stats.record_retry(
                    role=observation.role,
                    model=self.model_name,
                    wait_s=sleep_s,
                    ts=time.time(),
                )
            outcome = retry_state.outcome
            exc = outcome.exception() if outcome is not None else None
            observation.events.llm_retry_scheduled(
                trial_id=observation.trial_id,
                role=observation.role,
                provider=self.provider,
                model=self.model_name,
                attempt=retry_state.attempt_number,
                next_attempt_in_s=sleep_s,
                reason=f"{type(exc).__name__}: {exc}" if exc is not None else "",
            )

        return _before_sleep

    def _fire_call_started(self, observation: LLMCallObservation | None, attempt: int) -> None:
        if observation is None:
            return
        observation.events.llm_call_started(
            trial_id=observation.trial_id,
            role=observation.role,
            provider=self.provider,
            model=self.model_name,
            attempt=attempt,
        )

    def _fire_call_finished(
        self,
        observation: LLMCallObservation | None,
        attempt: int,
        duration_s: float,
        exc: BaseException | None,
        result: GenerationResult | None,
    ) -> None:
        """Emit ``llm_call_finished`` and, in probe mode, record the success.

        *result* is the attempt's :class:`GenerationResult` on success and
        ``None`` on failure; it carries this call's already-extracted
        :class:`~tolokaforge.core.llm.usage.Usage`, which is where the token
        counts come from — nothing is re-derived here.
        """
        if observation is None:
            return
        self._record_probe_success(observation, duration_s, exc, result)
        observation.events.llm_call_finished(
            trial_id=observation.trial_id,
            role=observation.role,
            provider=self.provider,
            model=self.model_name,
            attempt=attempt,
            duration_s=duration_s,
            error=None if exc is None else f"{type(exc).__name__}: {exc}",
        )

    def _record_probe_success(
        self,
        observation: LLMCallObservation,
        duration_s: float,
        exc: BaseException | None,
        result: GenerationResult | None,
    ) -> None:
        """Accumulate this call's SUCCESS side onto the trial's probe stats.

        The mirror image of the 429 recording in :meth:`_make_before_sleep`, and
        gated identically: the trial must carry an accumulator *and* this
        client's own probe must be active. Without the second half a
        default-path client — the rubric judge, a fallback-chain member — could
        contribute to a measurement it is not part of.

        Keyed by this call's ``role`` and this client's model slug, both already
        in scope. That attribution is the gap this closes: ``Metrics.usage``
        accumulates every role's calls into one object with no role field, so
        counting log lines conflated the agent's model with the user
        simulator's and inflated the number.

        Recorded quantities and why:

        * ``duration_s`` is the outer per-attempt wall time
          (:meth:`generate` brackets :meth:`_generate_once`), i.e. how long the
          client actually held this call in flight. Summed and divided by wall
          time it is the Little's-law in-flight concurrency the provider served
          — the schedule-independent estimator, computed on SUCCESSFUL calls
          only. The 429 census is schedule-dependent and, for some providers,
          silent: a provider can throttle by slowing calls down rather than
          rejecting them (``docs/OUTPUT_FORMAT.md`` § Field observations).
        * Tokens come off ``result.usage``, which :meth:`_assemble_result`
          already built for exactly this call, so nothing is re-extracted and
          the trial's ``usage.calls`` list is untouched — no double counting.

        ``exc is None and result is not None`` are the same condition in
        practice; both are checked so a future caller that forgets the result
        records nothing rather than a call with zero tokens.
        """
        if exc is not None or result is None:
            return
        probe_stats = observation.probe_stats
        if probe_stats is None or self._rate_limit_probe is None:
            return
        probe_stats.record_success(
            role=observation.role,
            model=self.model_name,
            duration_s=duration_s,
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            ts=time.time(),
        )

    def _generate_once(
        self,
        *,
        system: str | None,
        messages: list[Message],
        tools: list[dict[str, Any]] | None,
        tool_choice: str | None,
        temperature: float | None,
        seed: int | None,
        reasoning: ReasoningConfig | None,
        top_p: float | None,
        max_tokens: int | None,
    ) -> GenerationResult:
        """One outer-retry attempt: prepare → build → call → detect → assemble.

        A synthetic-envelope detection raises :class:`RuntimeError` so the
        outer :class:`Retrying` controller re-attempts the call — see
        :func:`_detect_synthetic_envelope`.
        """
        wire_system, sanitized_tools, effective_system_prompt = self._prepare_prompt_and_tools(
            system, tools
        )
        kwargs = self._build_kwargs(
            system=wire_system,
            messages=messages,
            tools=sanitized_tools,
            tool_choice=tool_choice,
            temperature=temperature,
            seed=seed,
            reasoning=reasoning,
            top_p=top_p,
            max_tokens=max_tokens,
        )
        start_time = time.time()
        response = self._call_with_key_rotation(kwargs)
        latency = time.time() - start_time
        synthetic = _detect_synthetic_envelope(response)
        if synthetic is not None:
            self.logger.warning(
                "Discarding synthetic-error envelope from upstream provider",
                native_finish_reason=synthetic,
                model=self.model_name,
            )
            raise RuntimeError(
                f"LLM API returned synthetic-error envelope "
                f"(native_finish_reason={synthetic!r}). Upstream provider "
                f"produced an unrecoverable response; retrying."
            )
        return self._assemble_result(
            response=response,
            effective_system_prompt=effective_system_prompt,
            latency_s=latency,
            sanitized_tools=sanitized_tools,
        )

    # ------------------------------------------------------------------
    # generate() sub-phases
    # ------------------------------------------------------------------

    def _prepare_prompt_and_tools(
        self,
        system: str | None,
        tools: list[dict[str, Any]] | None,
    ) -> tuple[str | list[dict[str, Any]] | None, list[dict[str, Any]] | None, str | None]:
        """Enrich + sanitise + cache-decorate the request's prompt / tools.

        Runs three pre-wire policies in order: ``prompt_policy.enrich`` →
        ``schema_sanitizer.sanitize`` → ``cache_policy.apply``.

        Returns a 3-tuple ``(wire_system, sanitised_tools,
        effective_system_prompt)``:

        * ``wire_system`` — ``str`` or list-of-content-blocks; forwarded to
          :meth:`_build_kwargs` as ``messages[0].content``.
        * ``sanitised_tools`` — post-sanitise + post-cache tool list; ``None``
          when the caller passed no tools.
        * ``effective_system_prompt`` — ``str`` form of the post-enrichment,
          pre-cache system prompt. Threaded onto
          :attr:`GenerationResult.effective_system_prompt` so downstream
          consumers never see the block-list wire shape.
        """
        original_system = system
        system = self.capabilities.prompt_policy.enrich(system, tools)
        if system and system != original_system:
            hints_length = len(system) - len(original_system or "")
            self.logger.info("Dict-map prompt hints injected", hint_length=hints_length)
            self.logger.debug(
                "Prompt policy enrichment applied",
                prompt_policy=type(self.capabilities.prompt_policy).__name__,
                original_length=len(original_system or ""),
                enriched_length=len(system),
            )

        # Preserve the enriched, pre-cache system prompt as a plain string
        # for downstream consumers. Cache-policy may rewrite this into a
        # list of content-blocks on the wire, but the observable state stays
        # string-shaped. See docs/LLM_LAYER.md § effective_system_prompt.
        effective_system_prompt = _effective_system_as_text(system)

        sanitized_tools: list[dict[str, Any]] | None = None
        if tools:
            sanitized_tools = self.capabilities.schema_sanitizer.sanitize(tools)
            dict_maps = detect_dict_maps(tools)
            if dict_maps:
                self.logger.debug(
                    "Dict-map parameters detected in tools",
                    count=len(dict_maps),
                    params=[f"{dm.tool_name}.{dm.param_name}" for dm in dict_maps],
                    schema_sanitizer=type(self.capabilities.schema_sanitizer).__name__,
                )

        # Cache policy runs in two phases: ``apply`` decorates system + tools
        # BEFORE ``_convert_messages`` so the schema sanitiser never sees a
        # ``cache_control`` key it doesn't understand; ``apply_messages`` runs
        # on the wire-shape messages AFTER ``_convert_messages`` in
        # :meth:`_build_kwargs`, since message-block marker attachment needs
        # the exact shape ``litellm.completion`` will receive.
        cached_system, cached_tools, _ = self.capabilities.cache_policy.apply(
            system, sanitized_tools, []
        )
        return cached_system, cached_tools, effective_system_prompt

    def _build_kwargs(
        self,
        *,
        system: str | list[dict[str, Any]] | None,
        messages: list[Message],
        tools: list[dict[str, Any]] | None,
        tool_choice: str | None,
        temperature: float | None,
        seed: int | None,
        reasoning: ReasoningConfig | None,
        top_p: float | None,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        """Build the complete kwargs dict for the downstream litellm call.

        Composes four independent parameter sources: ``params_policy.adapt``
        (temperature / seed / reasoning routing), explicit per-call overrides
        (``top_p`` / ``max_tokens`` / ``tool_choice`` / ``tools``), provider
        routing (OpenRouter headers + ``custom_llm_provider``), and
        :meth:`_convert_messages` (content policy + reasoning-codec replay).
        Providers whose binding declares ``kwargs_pin_transport`` defer their
        transport pinning to :meth:`_call_with_key_rotation` so the API key
        is read fresh per attempt.
        """
        kwargs: dict[str, Any] = {"model": self.model_name}
        if self.allowed_openai_params:
            # litellm's own escape hatch for a model its map does not carry: the
            # named parameters are admitted past the gating for this call, and
            # stripped before the request body is built. Anything NOT named is
            # still refused, so the declaration stays the boundary.
            kwargs["allowed_openai_params"] = list(self.allowed_openai_params)

        # Adapt model-specific parameters (temperature, seed, reasoning)
        kwargs = self.capabilities.params_policy.adapt(
            kwargs=kwargs,
            config_temperature=self.config.temperature,
            config_seed=self.config.seed,
            config_reasoning=self.config.reasoning,
            temperature=temperature,
            seed=seed,
            reasoning=reasoning,
        )

        top_p_value = top_p if top_p is not None else self.config.top_p
        if top_p_value is not None:
            kwargs["top_p"] = top_p_value

        max_tokens_value = max_tokens if max_tokens is not None else self.config.max_tokens
        if max_tokens_value is not None:
            kwargs["max_tokens"] = max_tokens_value

        if tools:
            kwargs["tools"] = tools
            # A preset or provider may declare that this route will not take
            # the value we are about to send. Omitting `tool_choice` is how the
            # OpenAI-shaped envelope says "the model decides", which is what
            # `auto` names, so a drop here costs nothing — but the declaration
            # is the operator's, not ours, so it is logged like any other
            # change to the request.
            policy = self.capabilities.params_policy
            action = policy.rule_for("tool_choice", tool_choice)
            if action == RuleAction.REJECT and tool_choice:
                raise ValueError(
                    f"tool_choice={tool_choice!r} is declared unusable for this "
                    f"provider+model combination. Evidence: "
                    f"{policy.rule_evidence('tool_choice', tool_choice)}. Pass a "
                    f"different tool_choice, or declare 'drop' / 'override' if "
                    f"the call should proceed anyway."
                )
            if action == RuleAction.OVERRIDE and tool_choice:
                substitute = policy.rule_substitute("tool_choice", tool_choice)
                if substitute:
                    policy.warn_substituted("tool_choice", tool_choice, substitute)
                    tool_choice = substitute
            if action == RuleAction.DROP and tool_choice:
                policy.warn_substituted("tool_choice", tool_choice, "<omitted>")
            if tool_choice and action != RuleAction.DROP:
                kwargs["tool_choice"] = tool_choice

        kwargs["messages"] = self.capabilities.cache_policy.apply_messages(
            self._convert_messages(system, messages)
        )

        if self.provider.startswith("openrouter"):
            extra_headers = dict(self._openrouter_headers)
            existing_extra = kwargs.get("extra_headers")
            if isinstance(existing_extra, dict):
                extra_headers.update(existing_extra)
            kwargs["extra_headers"] = extra_headers
            kwargs.setdefault(
                "custom_llm_provider",
                self._provider_binding.custom_llm_provider or self.provider.split("/")[0],
            )
            user_or = self.config.openrouter
            preset_or = self.capabilities.openrouter_defaults
            provider_order: list[str] | None
            if user_or is not None and user_or.provider_order:
                provider_order = list(user_or.provider_order)
            elif preset_or is not None and preset_or.provider_order:
                provider_order = list(preset_or.provider_order)
            else:
                provider_order = None
            if user_or is not None:
                allow_fallbacks = user_or.allow_fallbacks
            elif preset_or is not None:
                allow_fallbacks = preset_or.allow_fallbacks
            else:
                allow_fallbacks = True
            if provider_order:
                kwargs.setdefault("extra_body", {})["provider"] = {
                    "order": provider_order,
                    "allow_fallbacks": allow_fallbacks,
                }

        if self._proxy is not None:
            # The gateway is an OpenAI-compatible endpoint, so speak that dialect
            # and address it by ITS route name. docs/LLM_LAYER.md § Speaking to the
            # gateway has the failure modes this replaces.
            route = self._gateway_route
            if route is not None:
                kwargs["model"] = route
                kwargs["custom_llm_provider"] = "openai"
            # ONE rule for the provider pin on BOTH gateway paths (resolved route
            # and unreadable catalog): ``extra_body.provider`` survives the hop
            # exactly when the wire name's first segment IS the model's own
            # provider namespace - the upstream behind such a route is the same
            # family the pin was written for, and honours it. A route into any
            # other namespace is another upstream, which rejects the field or,
            # worse, silently ignores it. docs/LLM_LAYER.md § Speaking to the
            # gateway.
            wire_model = str(kwargs.get("model", self.model_name))
            if wire_model.split("/", 1)[0] != self.provider.split("/", 1)[0]:
                extra_body = kwargs.get("extra_body")
                if isinstance(extra_body, dict) and "provider" in extra_body:
                    extra_body.pop("provider")
                    if not self._pin_drop_warned:
                        # Once per client, not per call: a full eval makes
                        # thousands of calls and the fact does not change.
                        self._pin_drop_warned = True
                        self.logger.warning(
                            "Dropping the OpenRouter provider pin: the gateway route "
                            "targets another namespace, whose upstream rejects it",
                            route=wire_model,
                            provider=self.provider,
                        )
                    if not extra_body:
                        kwargs.pop("extra_body")
            kwargs["api_base"] = self._proxy.base_url
            if self._proxy.api_key:
                kwargs["api_key"] = self._proxy.api_key
            existing_headers = kwargs.get("extra_headers")
            merged_headers = dict(existing_headers) if isinstance(existing_headers, dict) else {}
            # Gateway headers win on a name collision: they are explicit
            # operator configuration, whereas the headers already in the dict
            # are this engine's own provider defaults (OpenRouter's
            # ``HTTP-Referer`` / ``X-Title`` / opt-out trio).
            merged_headers.update(self._proxy.request_headers())
            kwargs["extra_headers"] = merged_headers

        return kwargs

    def _is_timeout_error(self, exc: BaseException) -> bool:
        """Detect transport-level timeout errors from the LLM client stack.

        Recognises:

        * built-in :class:`TimeoutError`,
        * :class:`openai.APITimeoutError` (when present),
        * concrete httpx / requests timeout classes by qualified name
          (so we don't pull a hard import on either).

        Deliberately does **not** match by substring on the exception
        message — non-timeout errors with the word "timeout" in their text
        (tool-execution timeouts, sandbox timeouts, validation messages)
        must not be routed into the per-call retry loop.
        """
        if isinstance(exc, TimeoutError):
            return True

        api_timeout_cls = getattr(openai, "APITimeoutError", None)
        if api_timeout_cls is not None and isinstance(exc, api_timeout_cls):
            return True

        qualname = f"{type(exc).__module__}.{type(exc).__qualname__}"
        return qualname in {
            "httpx.ReadTimeout",
            "httpx.ConnectTimeout",
            "httpx.WriteTimeout",
            "httpx.PoolTimeout",
            "requests.exceptions.Timeout",
            "requests.exceptions.ReadTimeout",
            "requests.exceptions.ConnectTimeout",
        }

    def _log_timeout_retry(self, retry_state: RetryCallState) -> None:
        """Log timeout retry attempts."""
        self.logger.warning(
            "LLM API call timed out; retrying",
            event="api_call_timeout",
            attempt=retry_state.attempt_number,
            timeout_s=self._api_call_timeout_s,
        )

    def _call_completion_with_timeout_retry(self, kwargs: dict[str, Any]) -> Any:
        """Call litellm ``completion`` with a bounded per-call timeout retry.

        Worst-case wait: ``stop_after_attempt(retries + 1)`` × ``timeout``
        plus the exponential ``wait_exponential(min=1, max=5)`` between
        attempts. With the defaults (5 retries, 120 s timeout) that's
        ~6 × 120 s + ~17 s ≈ 12 minutes before :class:`LLMApiTimeoutError`
        fires.

        When ``api_call_wall_timeout_s`` is set, a call that exceeds that hard
        wall-clock budget is aborted at ~``wall`` and raised as
        :class:`LLMApiTimeoutError` immediately, bypassing the retry budget
        above (retrying a runaway generation would only stack abandoned
        in-flight calls). That retry budget still applies to transient
        read-timeouts raised by the call itself.
        """

        @retry(
            retry=retry_if_exception(self._is_timeout_error),
            stop=stop_after_attempt(self._api_timeout_retries + 1),
            wait=wait_exponential(multiplier=1, min=1, max=5),
            before_sleep=self._log_timeout_retry,
            reraise=True,  # surface the last TimeoutError, not RetryError
        )
        def _call() -> Any:
            wall_timeout_s = self._api_call_wall_timeout_s
            if wall_timeout_s is None:
                return completion(**kwargs)
            # Enforce a hard wall-clock ceiling. The ``timeout`` in kwargs is
            # only a per-read httpx timeout that a slowly-streamed or runaway
            # response keeps resetting, so it never bounds total elapsed time.
            # Run the call on a daemon thread and stop waiting once the budget
            # is spent. A daemon thread cannot block interpreter shutdown, and
            # the overrun is terminal (raised as LLMApiTimeoutError, not routed
            # back into the retry below) so a persistently runaway provider
            # cannot stack multiple abandoned in-flight calls (upstream cost +
            # connection-pool pressure). Transient read-timeouts raised by the
            # call itself still propagate and retry normally.
            result_box: dict[str, Any] = {}
            error_box: list[BaseException] = []

            def _run() -> None:
                try:
                    result_box["value"] = completion(**kwargs)
                except BaseException as exc:  # captured, re-raised on the caller thread
                    error_box.append(exc)

            worker = threading.Thread(target=_run, daemon=True, name="llm-wall-timeout-call")
            worker.start()
            worker.join(timeout=wall_timeout_s)
            if worker.is_alive():
                raise LLMApiTimeoutError(
                    f"LLM API call exceeded wall-clock timeout of {wall_timeout_s}s"
                )
            if error_box:
                raise error_box[0]
            return result_box["value"]

        try:
            return _call()
        except Exception as e:
            if self._is_timeout_error(e):
                self.logger.warning(
                    "LLM API call timed out; retry limit exhausted",
                    event="api_call_timeout",
                    attempts=self._api_timeout_retries + 1,
                    timeout_s=self._api_call_timeout_s,
                )
                raise LLMApiTimeoutError(
                    f"LLM API call timed out after {self._api_timeout_retries + 1} attempts (timeout={self._api_call_timeout_s}s)"
                ) from e
            raise

    def _call_with_key_rotation(self, kwargs: dict[str, Any]) -> Any:
        """Call litellm ``completion`` with OpenRouter key rotation.

        Providers whose binding declares ``kwargs_pin_transport`` read the
        endpoint and API key fresh per attempt (key rotation may have cleared
        them). On "Key limit exceeded" / 402 / 403 errors we rotate to the
        next OpenRouter key and retry; timeout failures are retried locally
        with a bounded backoff before the trial is aborted.

        Raises
        ------
        RuntimeError
            - ``f"{binding.api_key_env} is required for {provider} provider"``
               when a ``kwargs_pin_transport`` provider's key resolves empty.
            - :class:`AllApiKeysExhaustedError` (``"All API keys exhausted"``)
               after the last OpenRouter key hit a quota error. A dedicated
               subclass because the condition is terminal — see the class
               docstring and :meth:`LLMClient._is_rate_limit_exception`.
            - ``LLMApiTimeoutError`` when the call times out repeatedly.
            - ``f"LLM API call failed: {e}"`` for any other provider error.
        """
        kwargs = dict(kwargs)
        kwargs.setdefault("timeout", self._api_call_timeout_s)
        binding = self._provider_binding

        while True:
            try:
                # Every binding-driven kwarg rewrite is skipped when the call
                # is routed through the gateway: `_build_kwargs` already set
                # the gateway's dialect + api_base, and overwriting them from
                # the provider binding here reverts the name AND the body
                # shape. `kwargs_pin_transport` pins api_base/api_key per
                # attempt (Nova needs the key read fresh because rotation
                # clears it), so it belongs under the same guard — a routable
                # pin-transport provider (none today; hypothetical future
                # binding) would otherwise fight the gateway.
                if self._gateway_route is None:
                    if binding.kwargs_pin_transport:
                        from tolokaforge.secrets import get_default

                        kwargs["api_base"] = binding.endpoint
                        kwargs["api_key"] = get_default().get_secret(binding.api_key_env)
                        if not kwargs["api_key"]:
                            raise RuntimeError(
                                f"{binding.api_key_env} is required for {self.provider} provider — "
                                f"set it in .env or the environment so SecretManager can resolve it"
                            )

                    if binding.custom_llm_provider is not None:
                        kwargs["custom_llm_provider"] = binding.custom_llm_provider
                    elif "/" in self.config.provider:
                        kwargs["custom_llm_provider"] = self.config.provider.split("/")[0]

                    if binding.slug_rewrite is not None:
                        rewrite = binding.slug_rewrite
                        model = kwargs["model"]
                        if rewrite.strip_prefix and model.startswith(rewrite.strip_prefix):
                            model = model[len(rewrite.strip_prefix) :]
                        if rewrite.ensure_prefix and not model.startswith(rewrite.ensure_prefix):
                            model = rewrite.ensure_prefix + model
                        kwargs["model"] = model

                return self._call_completion_with_timeout_retry(kwargs)
            except LLMApiTimeoutError:
                # Pass through unchanged so ``_should_retry_exception`` can
                # opt the outer ``Retrying`` controller out of re-attempting
                # the call. Wrapping it in a generic ``RuntimeError`` here
                # would destroy the subtype and let the outer retry blow the
                # bounded budget up by ``stop_after_attempt(5)``×.
                raise
            except Exception as e:
                error_str = str(e)
                if (
                    "Key limit exceeded" in error_str
                    or "requires more credits" in error_str
                    or '"code":403' in error_str
                    or '"code":402' in error_str
                ):
                    if self._proxy is not None and self._proxy.api_key:
                        # Rotation cannot help *when a gateway key is pinned*:
                        # ``_rotate_key`` republishes the provider's
                        # ``api_key_env`` into the environment, but the pinned
                        # ``api_key`` kwarg takes precedence in litellm.
                        # Rotating would resend byte-identical requests and
                        # then report an exhausted key chain that was never in
                        # play. Without a gateway key litellm reads the
                        # provider env var, so rotation still works and must
                        # be left alone.
                        self.logger.error(
                            "Gateway rejected the request as over quota or unauthorized",
                            base_url=self._proxy.base_url,
                        )
                        raise RuntimeError(
                            f"LLM gateway at {self._proxy.base_url} rejected the request "
                            f"(quota or authorization). Provider key rotation does not apply "
                            f"to gateway-routed calls; check the gateway credential and its "
                            f"budget: {e}"
                        ) from e
                    if self._rotate_key():
                        self.logger.warning(
                            "API key exhausted, rotated to next key",
                            key_index=self._current_key_index,
                            remaining_keys=len(self._api_keys) - self._current_key_index - 1,
                        )
                        continue
                    self.logger.error("All API keys exhausted")
                    raise AllApiKeysExhaustedError("All API keys exhausted") from e
                raise RuntimeError(f"LLM API call failed: {e}") from e

    def _assemble_result(
        self,
        *,
        response: Any,
        effective_system_prompt: str | None,
        latency_s: float,
        sanitized_tools: list[dict[str, Any]] | None = None,
    ) -> GenerationResult:
        """Convert a raw litellm response into a :class:`GenerationResult`.

        Delegates provider-specific extraction to the registered policies:

        * ``reasoning_codec.extract`` — lifts ``thinking_blocks`` / OpenAI
          reasoning into :class:`StructuredReasoning` (``None`` for
          non-reasoning models; zero inline attribute branching).
        * ``response_policy.parse_arguments`` — reverses e.g. array-dict-map
          wire shape back into typed ``Dict[str, T]``, and (when given the
          post-sanitised tool schema) coerces empty strings back to empty
          containers for declared array / object params.
        * :class:`UsageExtractor` — pulls full prompt / completion / reasoning
          / cache counters (see docs/LLM_LAYER.md § ``usage``).
        * :func:`extract_openrouter_generation_id` — lifts OpenRouter's
          generation id off the response headers (``None`` elsewhere).

        ``cost_usd`` is computed iff usage is populated; unknown pricing
        surfaces as ``None``.
        """
        choice = response.choices[0]
        message = choice.message

        text = message.content or ""
        text = self.capabilities.assistant_text_policy.parse_assistant_text(
            text, model_config=self.config
        )
        tool_calls: list[ToolCall] = []

        reasoning_result = self.capabilities.reasoning_codec.extract(message)

        # Build per-tool root-level parameter type maps once per call so the
        # response policy's schema-aware coercions (currently: empty-container
        # recovery for ``array`` / ``object`` params) can fire on every
        # tool-call argument set the model emits.
        param_types_by_tool = _root_param_types_by_tool(sanitized_tools)

        parser_errors_list: list[ParserError] = []
        if hasattr(message, "tool_calls") and message.tool_calls:
            for tc in message.tool_calls:
                raw = tc.function.arguments
                arguments, parse_reason = self._try_parse_tool_arguments(tc.function.name, raw)
                if parse_reason is not None:
                    raw_str = raw if isinstance(raw, str) else str(raw)
                    excerpt = raw_str[:PARSER_ERROR_RAW_ARGS_EXCERPT_MAX_CHARS]
                    parser_errors_list.append(
                        ParserError(
                            tool_name=tc.function.name,
                            raw_arguments=excerpt,
                            reason=parse_reason,
                        )
                    )
                arguments = self.capabilities.response_policy.parse_arguments(
                    arguments,
                    param_types=param_types_by_tool.get(tc.function.name),
                )

                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        name=tc.function.name,
                        arguments=arguments,
                    )
                )

        # Cost ladder (also pinned by tests/canonical/test_cost_extraction_canon.py):
        #   1. litellm `_hidden_params['response_cost']` / completion_cost
        #      → cache-aware, provider-authoritative (cost_source="litellm").
        #   2. bundled pricing table fallback (cost_source="local").
        #   3. neither — cost_usd is None, cost_source="unknown".
        # The local fallback needs token counts, so we extract once just to
        # peek at tokens; the authoritative extraction below threads the
        # resolved cost + source into the per-call ProviderRawCall record.
        cost_source: CostSource = "unknown"
        cost_usd: float | None = _litellm_response_cost(response)
        if cost_usd is not None:
            cost_source = "litellm"
        else:
            tokens_only = UsageExtractor().extract(response)
            if tokens_only.prompt_tokens or tokens_only.completion_tokens:
                cost_usd = estimate_cost(
                    model=self.model_name,
                    input_tokens=tokens_only.prompt_tokens,
                    output_tokens=tokens_only.completion_tokens,
                    cache_read_input_tokens=tokens_only.cache_read_input_tokens,
                    cache_creation_input_tokens=tokens_only.cache_creation_input_tokens,
                    reasoning_tokens=tokens_only.reasoning_tokens,
                )
                if cost_usd is not None:
                    cost_source = "local"

        usage = UsageExtractor().extract(
            response,
            latency_s=latency_s,
            cost_usd=cost_usd,
            cost_source=cost_source,
            # Plain str on purpose: the artifact carries data, not the resolver's
            # str subclass (asdict deepcopies every call record).
            gateway_route=str(self._gateway_route) if self._gateway_route is not None else None,
            gateway_route_kind=self._gateway_route_kind,
        )

        result = GenerationResult(
            text=text,
            tool_calls=tool_calls,
            usage=usage,
            latency_s=latency_s,
            cost_usd=cost_usd,
            reasoning=reasoning_result,
            effective_system_prompt=effective_system_prompt,
            # Read off the response rather than off ``usage.calls``: a provider
            # that returned no usage block contributes no call record, and the
            # routing decision is still worth recording for that turn.
            openrouter_generation_id=extract_openrouter_generation_id(response),
            # litellm post-maps every current provider's max-tokens truncation
            # to the OpenAI-compatible ``"length"`` on this field; a response
            # that carries no finish_reason at all lands as ``None``.
            finish_reason=getattr(choice, "finish_reason", None),
        )
        # Attribute-post-init idiom matches ``guard_rejections`` and
        # ``filler_substituted``. Any other producer of a ``GenerationResult``
        # (mock generator, inline test constructions) inherits the ``()``
        # default and the seam is inert.
        result.parser_errors = tuple(parser_errors_list)
        return result

    # ------------------------------------------------------------------
    # Mock generator (offline tests)
    # ------------------------------------------------------------------

    def _mock_generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None,  # noqa: ARG002
    ) -> GenerationResult:
        """Deterministic mock responder for offline tests.

        The synthesised text bypasses
        :attr:`~ModelCapabilities.assistant_text_policy` on purpose: offline
        tests use ``mock`` provider to exercise the harness without any real
        provider text on the wire, so there are no provider markers to strip.
        Piping the mock text through the policy would couple offline tests
        to whatever policy the resolved preset picks — including subclasses
        that strip strings the mock scenario deliberately embeds.
        """

        last_message = messages[-1] if messages else None
        text: str

        name_hint = (self.config.name or "").lower()
        last_content = (last_message.content if last_message else "") or ""
        lower_content = last_content.lower()

        if "judge" in name_hint or "grading judge" in lower_content:
            text = '{"score": 0.7, "reasons": "Mock judge: baseline structured evaluation."}'
        elif "user" in name_hint:
            if not messages or (
                len(messages) == 1 and last_message and last_message.role == MessageRole.ASSISTANT
            ):
                text = "Hello, I need help completing this benchmark task."
            else:
                text = "Thanks, that answers my question. ###STOP###"
        else:
            text = "Acknowledged. Task complete."

        return GenerationResult(
            text=text,
            tool_calls=[],
            usage=Usage(completion_tokens=len(text.split())),
            latency_s=0.0,
            cost_usd=0.0,
        )


# The canned agent-side greeting: dispatched by the runner when the simulator
# bootstraps turn 0, and re-used to lead the simulator's flipped context when
# the opening was caller-seeded (see ``_llm_reply``). One constant so the two
# sites cannot diverge — the mid-conversation reconstruction must show the
# simulator the same agent opening it answers at bootstrap.
SIMULATOR_GREETING = "Hi! How can I help you today?"


class UserSimulator(Actor):
    """User simulator for benchmarking.

    Declares :class:`~tolokaforge.core.actors.actor.Actor` conformance so
    the turn-loop seam can accept any Protocol-satisfying actor kind, not
    just this concrete simulator. Behaviour is unchanged — the existing
    :meth:`reply` already matches the Protocol signature.
    """

    def __init__(
        self,
        mode: str = "scripted",
        llm_config: ModelConfig | None = None,
        persona: str = "cooperative",
        backstory: str | None = None,
        scripted_flow: list[dict[str, str]] | None = None,
        tool_schemas: list[dict[str, Any]] | None = None,
        *,
        rate_limit_probe: RateLimitProbeConfig | None = None,
    ):
        self.mode = mode
        self.persona = persona
        self.backstory = backstory
        self.scripted_flow = scripted_flow or []
        self.tool_schemas = tool_schemas or []
        self.llm_client = (
            LLMClient(llm_config, rate_limit_probe=rate_limit_probe)
            if llm_config and mode == "llm"
            else None
        )
        # The last system prompt emitted on an LLM reply; scripted simulators
        # stay ``None`` forever.
        self.last_system_prompt: str | None = None
        self._reply_guard = UserReplyGuard()

    def reply(
        self,
        context: list[Message],
        *,
        observation: LLMCallObservation | None = None,
    ) -> GenerationResult:
        """Generate user reply based on context.

        ``observation`` is forwarded to the inner :meth:`LLMClient.generate`
        for LLM mode so the display can render user-role LLM-call events.
        Scripted mode ignores it — scripted replies never touch the wire.
        """
        if self.mode == "scripted":
            text = self._scripted_reply(context)
            return GenerationResult(text=text, tool_calls=[])
        elif self.mode == "llm":
            return self._llm_reply(context, observation=observation)
        else:
            raise ValueError(f"Unknown user simulator mode: {self.mode}")

    def _scripted_reply(self, context: list[Message]) -> str:
        """Generate scripted reply based on flow rules."""
        if not context:
            return "Hello, I need help with this task."

        if self.scripted_flow:
            sent_messages = {
                msg.content.strip()
                for msg in context
                if msg.role == MessageRole.USER and msg.content
            }

            for rule in self.scripted_flow:
                has_condition = any(key.startswith("if_") for key in rule)
                if not has_condition:
                    text = rule.get("user", "").strip()
                    if text and text not in sent_messages:
                        return text

        last_assistant_msg = None
        for msg in reversed(context):
            if msg.role == MessageRole.ASSISTANT:
                last_assistant_msg = msg.content
                break

        if not last_assistant_msg:
            return "I'm waiting for your response."

        default_response = None
        for rule in self.scripted_flow:
            if (
                "if_assistant_contains" in rule
                and rule["if_assistant_contains"].lower() in last_assistant_msg.lower()
            ):
                return rule["user"]
            if "default" in rule:
                default_response = rule["default"]

        if default_response:
            return default_response

        if "?" in last_assistant_msg:
            return "Yes, please proceed."
        return "Okay."

    def _build_system_prompt(self) -> str:
        """The task's Instruction outranks every rule the block below states.

        Pure, so the prompt is observable without firing a generation:
        ``TrialRunner`` captures ``last_system_prompt`` after the first user
        turn and writes it to the trial bundle's ``prompts.yaml``.

        The body is fixed apart from two conditional segments — the
        ``Instruction:`` block, absent entirely when the task supplied no
        backstory, and the tool-guidance block, appended only when the
        simulator holds tool schemas.
        ``tests/canonical/test_simulator_prompt_generation.py`` holds what this
        renders to ``Trajectory.simulator_schema_version``.
        """
        instruction_display = (
            ("\n\nInstruction: " + self.backstory + "\n") if self.backstory else ""
        )

        tool_guidance = ""
        if self.tool_schemas:
            tool_guidance = """
- You have access to tools. Use them whenever the agent asks you for something one of them can establish.
- ALWAYS use a tool to ground your answer rather than answering from memory or assumption.
- Never make up or hallucinate tool results. Always call the actual tool and report what it returns.
- If unsure whether you need to use a tool, prefer using it over making assumptions."""

        return f"""You are a user interacting with an agent.{instruction_display}
Rules:
- Your Instruction above is authoritative. Where any rule below conflicts with it, follow the Instruction.
- Reply with one short message per turn — a single turn of dialogue in your own voice, never a multi-turn script and never a monologue.
- Where your Instruction gives you an exact line to say, say it verbatim. Otherwise use your own words rather than quoting the Instruction back.
- Reveal only what the current step needs, and answer the agent's questions directly. Give information your Instruction withholds only when your Instruction says to.
- Never invent details your Instruction does not give you. If the agent asks for something you were not told — an order id, a reference number — say you do not have it rather than making one up.
- Raise your remaining requests yourself: when your Instruction sequences work with words like "after", "then", or "once", bring up the next part once the previous one is done, and when the agent asks whether there is anything else, continue with what is left.
- Do not claim to have done a step yourself, and do not credit the agent with work it has not reported.
- '###STOP###' ends the conversation. Send it once every part of your request has reached an outcome — carried out, or turned down by the agent. An outcome you did not want still counts. Do not send it while part of your request is still unaddressed.
- If your Instruction still specifies an unsent mandatory reply (e.g. a verbal decline, confirmation, or acknowledgement you MUST say to the agent), send that reply first. A terminal-looking message from the agent — case reference, summary, apology, goodbye — does NOT release you from that reply. '###STOP###' may only accompany or follow the reply, never precede or replace it.
- Once the agent has substantively addressed your request, do not re-state or restart the original opening as if it had not been answered. Send at most one short acknowledgement and end with '###STOP###'; do not introduce new goals or remediation steps.
- Keep the conversation natural and stay in the personality your Instruction describes.
- Never mention that this is a simulation, test, benchmark, prompt, or that you are an AI/model.{tool_guidance}"""

    def _llm_reply(
        self,
        context: list[Message],
        *,
        observation: LLMCallObservation | None = None,
    ) -> GenerationResult:
        """Generate LLM-based user reply - tau-bench compatible with tool calling.

        Every generation passes through
        :class:`~tolokaforge.core.actors.reply_guard.UserReplyGuard`, which
        regenerates a reply a detector flags rather than editing it and raises
        :class:`~tolokaforge.core.actors.reply_guard.UserReplyRefused` once the
        attempt budget is spent. The returned text is exactly what the accepted
        generation produced; the discarded attempts' defects ride back on
        ``guard_rejections``.
        """
        if not self.llm_client:
            raise RuntimeError("LLM client not initialized for LLM mode")
        llm_client = self.llm_client

        # The simulator converses from the customer's seat: its own past
        # messages replay as ``assistant`` turns and the agent's as ``user``
        # turns. Turns with no dialogue text (agent tool-call turns,
        # whitespace-only replies) are skipped — replaying them as empty
        # turns adds noise the simulator's provider may reject. The skip is
        # text-only: a turn carrying ``content_blocks`` with no text would be
        # dropped too, a latent gap no USER/ASSISTANT call site produces
        # today (the flip has never carried blocks).
        # Adjacent same-role turns are coalesced so the request alternates
        # strictly — a skipped turn can leave two dialogue turns of the same
        # party back to back, which strict-alternation providers reject.
        sim_context: list[Message] = []
        flip = {MessageRole.USER: MessageRole.ASSISTANT, MessageRole.ASSISTANT: MessageRole.USER}
        for msg in context:
            if not msg.content.strip():
                continue
            role = flip.get(msg.role)
            if role is None:
                continue
            if sim_context and sim_context[-1].role == role:
                previous = sim_context[-1]
                sim_context[-1] = Message(
                    role=role, content=f"{previous.content}\n\n{msg.content}", ts=msg.ts
                )
            else:
                sim_context.append(Message(role=role, content=msg.content, ts=msg.ts))

        # Providers require the first message to be user-role, and the trial's
        # seeded opening flips to ``assistant`` at index 0. Prepend a synthetic
        # agent-side greeting rather than dropping the opening: without its own
        # opening in context the simulator believes it never asked and restarts
        # the conversation verbatim after the agent has already answered.
        if sim_context and sim_context[0].role == MessageRole.ASSISTANT:
            sim_context.insert(
                0,
                Message(
                    role=MessageRole.USER,
                    content=SIMULATOR_GREETING,
                    ts=sim_context[0].ts,
                ),
            )

        # The request must end on a user-role turn the simulator can answer.
        # A trailing assistant-role message is a prefill of the simulator's
        # own words (the provider continues it), and an empty list is
        # unanswerable — both mean no agent dialogue turn is awaiting a
        # reply, so surface that instead of letting the simulator improvise.
        if not sim_context or sim_context[-1].role != MessageRole.USER:
            raise RuntimeError(
                "User simulator dispatched with no agent dialogue turn to answer "
                f"(flipped context roles: {[m.role.value for m in sim_context]}; "
                f"shared transcript roles: {[m.role.value for m in context]})."
            )

        # Captured only for a request that is actually dispatched, so the
        # runner persists into ``prompts.yaml`` a prompt that really drove a
        # generation — never one belonging to a refused dispatch.
        system_prompt = self._build_system_prompt()
        self.last_system_prompt = system_prompt

        def generate() -> GenerationResult:
            result = llm_client.generate(
                system=system_prompt,
                messages=sim_context,
                tools=self.tool_schemas if self.tool_schemas else None,
                tool_choice="auto" if self.tool_schemas else None,
                temperature=0.2,
                observation=observation,
            )
            # The one substitution the reply guard wraps rather than forbids, and
            # the only text the engine contributes to a user turn. The conductor
            # now wires a user_tool_executor when the spec declares user tools,
            # so this branch is reachable in-tree — callers whose downstream
            # semantics need the model's own text (the bootstrap seed the agent
            # is graded against) read ``result.filler_substituted`` and refuse.
            # TODO(#1089): remove it — a universal filler is hazardous (AGENTS.md
            # gotcha 23), so the removal carries its own analysis.
            if result.tool_calls and not result.text.strip():
                result.text = "Let me check that."
                result.filler_substituted = True
            return result

        # The guard logs under its own logger name, so the trial identity has to
        # travel as record context; ``observation`` is where the trial already
        # hands this call site its identity.
        result, rejected = self._reply_guard.enforce(
            generate,
            log_extra=None if observation is None else {"trial_id": observation.trial_id},
        )
        result.guard_rejections = rejected
        return result
