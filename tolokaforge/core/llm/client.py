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
from collections.abc import Callable
from typing import Any

import litellm
import openai
import yaml
from litellm import completion
from tenacity import (
    RetryCallState,
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from tolokaforge.core.llm.capabilities import ModelCapabilities
from tolokaforge.core.llm.presets import build_capabilities
from tolokaforge.core.llm.prompt_policy import detect_dict_maps
from tolokaforge.core.llm.reasoning import ReasoningConfig, StructuredReasoning
from tolokaforge.core.llm.usage import CostSource, Usage, UsageExtractor
from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import Message, MessageRole, ModelConfig, ToolCall
from tolokaforge.core.pricing import estimate_cost

__all__ = ["GenerationResult", "LLMApiTimeoutError", "LLMClient", "UserSimulator"]


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
    ``@retry`` on :meth:`LLMClient.generate` can decline to re‑attempt
    (see :func:`_should_retry_exception`).
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
    """Determine if an exception should drive the outer ``@retry`` loop.

    Returns ``True`` for all transient errors **except**
    :class:`LLMApiTimeoutError` — which is already the result of an
    exhausted per-call retry budget inside
    :meth:`LLMClient._call_completion_with_timeout_retry` and so must
    not cascade into another ``stop_after_attempt(5)`` multiplier.

    Rate limits (429) ride the same outer exponential backoff as other
    errors — the long waits (up to 60s between attempts) give the
    provider quota time to recover.
    """
    return not isinstance(exc, LLMApiTimeoutError)


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
    :class:`RuntimeError` on a non-``None`` return so tenacity's
    ``@retry`` re-attempts the call.

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


class LLMClient:
    """Provider-agnostic LLM client using LiteLLM."""

    def __init__(self, config: ModelConfig):
        self.config = config
        self.model_name = self._format_model_name()
        self.capabilities = build_capabilities(
            self.config.name,
            self.config.provider,
            overrides=self.config.capabilities,
        )
        self.provider = (config.provider or "").lower()
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
        if self.provider.startswith("openrouter"):
            self._openrouter_headers = self._configure_openrouter_headers()
            self._configure_openrouter_base_url()
        elif self.provider == "nova":
            self._configure_nova_base_url()
        self._openrouter_headers = (
            self._configure_openrouter_headers() if self.provider.startswith("openrouter") else {}
        )

        self._api_call_timeout_s = self._load_api_timeout()
        self._api_timeout_retries = self._load_api_timeout_retries()
        self._api_call_wall_timeout_s = self._load_api_wall_timeout()

    # ------------------------------------------------------------------
    # Model capabilities — preserved entry point for tests that reassign it
    # ------------------------------------------------------------------

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._capabilities

    @capabilities.setter
    def capabilities(self, value: ModelCapabilities) -> None:
        self._capabilities = value

    # ------------------------------------------------------------------
    # API key handling
    # ------------------------------------------------------------------

    def _load_api_keys(self) -> list[str]:
        """Load API keys for rotation via SecretManager.

        Routes through ``tolokaforge.secrets.get_default()`` so behaviour stays
        identical between host and runner-container processes (the runner
        bootstraps a SecretManager from ``TOLOKAFORGE_SECRETS_JSON``).
        ``OPENROUTER_KEY_FILE`` is *not* a secret — only the key file's *path*
        is a config knob, so it stays as a non-credential env var. The keys
        themselves are read from disk by this code.
        """
        from tolokaforge.secrets import get_default

        secrets = get_default()

        keys_str = secrets.get_secret("OPENROUTER_API_KEYS") or ""
        if keys_str:
            keys = [k.strip() for k in keys_str.split(",") if k.strip()]
            if keys:
                self.logger.info(
                    "Loaded API keys from OPENROUTER_API_KEYS",
                    key_count=len(keys),
                )
                return keys

        # OPENROUTER_KEY_FILE is a path, not a credential — env-var read OK.
        key_file = os.environ.get("OPENROUTER_KEY_FILE", "keys.txt")
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

        key = secrets.get_secret("OPENROUTER_API_KEY") or ""
        if key:
            return [key]
        return []

    def _rotate_key(self) -> bool:
        """Rotate to the next available API key."""
        if self._current_key_index + 1 < len(self._api_keys):
            self._current_key_index += 1
            new_key = self._api_keys[self._current_key_index]
            os.environ["OPENROUTER_API_KEY"] = new_key
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

    def _configure_nova_base_url(self) -> None:
        """Configure Nova API base URL for LiteLLM (informational only)."""
        os.environ.setdefault("NOVA_API_BASE", "https://api.nova.amazon.com/v1")

    def _format_model_name(self) -> str:
        """Format model name for LiteLLM."""
        if self.config.name.startswith(f"{self.config.provider}/"):
            return self.config.name

        if self.config.provider.lower() == "nova":
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

    def _parse_tool_arguments(self, tool_name: str, raw_args: Any) -> dict[str, Any]:
        """Parse model-emitted tool arguments with tolerant fallbacks."""

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
            return _normalize(raw_args)
        if raw_args is None or not isinstance(raw_args, str):
            return {}

        args_str = raw_args.strip()
        if not args_str:
            return {}

        try:
            parsed = json.loads(args_str)
            return _normalize(parsed) if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            pass

        try:
            parsed = yaml.safe_load(args_str)
            if isinstance(parsed, dict):
                self.logger.warning("Recovered malformed tool arguments", tool=tool_name)
                return _normalize(parsed)
        except Exception:
            pass

        repaired = self._repair_json_like(args_str)
        parsers: list[Callable[[str], Any]] = [json.loads, yaml.safe_load]
        for parser in parsers:
            try:
                parsed = parser(repaired)
                if isinstance(parsed, dict):
                    self.logger.warning("Recovered malformed tool arguments", tool=tool_name)
                    return _normalize(parsed)
            except Exception:
                continue

        self.logger.warning(
            "Failed to parse tool arguments",
            tool=tool_name,
            error="Unable to parse with JSON/YAML fallbacks",
        )
        return {}

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

        User tool calls are kept in Message objects for ActionEvaluator,
        but stripped here since most LLM APIs don't support tool_use from USER role.
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
                    # Empty assistant content alongside ``tool_calls`` is
                    # legal on most provider APIs (OpenAI, Anthropic,
                    # Gemini-via-OpenRouter), but Bedrock/Nova rejects it.
                    # ``ToolContentPolicy.inject_empty_assistant_filler``
                    # gates the substitution per preset — only Nova
                    # opts in. Injecting it elsewhere creates a few-shot
                    # pattern that Gemini echoes back as its own content
                    # (2026-04-30 OTS regression). See content_policy.py.
                    if self.capabilities.content_policy.inject_empty_assistant_filler:
                        content = "I'll help you with that."
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

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2, min=4, max=60),
        retry=retry_if_exception(_should_retry_exception),
        before_sleep=before_sleep_log(get_logger("llm_retry").logger, logging.WARNING),
        reraise=True,
    )
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
    ) -> GenerationResult:
        """Generate completion from LLM.

        Orchestrator for the four-phase request pipeline:
        :meth:`_prepare_prompt_and_tools` -> :meth:`_build_kwargs` ->
        :meth:`_call_with_key_rotation` -> :meth:`_assemble_result`. Every
        provider-specific transform lives in one of the ``ModelCapabilities``
        policies; the orchestrator itself never branches on provider.

        Returns a :class:`GenerationResult` with text, tool-calls, full
        :class:`Usage` counters, latency, cost, and structured reasoning.
        """
        messages = messages or []

        if self.provider == "mock":
            return self._mock_generate(messages, tools)

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
            # Raise so tenacity's ``@retry`` decorator re-attempts the
            # call. Consuming the stub here would silently corrupt the
            # trajectory — see ``_detect_synthetic_envelope`` docstring.
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

        # Apply cache policy after prompt enrichment + tool sanitization,
        # BEFORE _convert_messages — this way the cache marker is the last
        # thing added to the wire-level request and the sanitiser never sees
        # a ``cache_control`` key it doesn't understand. Stage 6 only caches
        # system + tools; message-level caching is deferred (empty list).
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
        Nova's special-casing is deferred to :meth:`_call_with_key_rotation`
        because it needs ``NOVA_API_KEY`` read fresh per attempt.
        """
        kwargs: dict[str, Any] = {"model": self.model_name}

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
            if tool_choice:
                kwargs["tool_choice"] = tool_choice

        kwargs["messages"] = self._convert_messages(system, messages)

        if self.provider.startswith("openrouter"):
            extra_headers = dict(self._openrouter_headers)
            existing_extra = kwargs.get("extra_headers")
            if isinstance(existing_extra, dict):
                extra_headers.update(existing_extra)
            kwargs["extra_headers"] = extra_headers
            kwargs.setdefault("custom_llm_provider", self.provider.split("/")[0])
            or_cfg = self.config.openrouter
            if or_cfg and or_cfg.provider_order:
                kwargs.setdefault("extra_body", {})["provider"] = {
                    "order": list(or_cfg.provider_order),
                    "allow_fallbacks": or_cfg.allow_fallbacks,
                }

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
                except Exception as exc:  # noqa: BLE001 - re-raised on the caller thread
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

        Nova's special-casing is applied inline here (the ``NOVA_API_KEY``
        must be read fresh on every attempt — key rotation may clear it).
        On "Key limit exceeded" / 402 / 403 errors we rotate to the next
        OpenRouter key and retry; timeout failures are retried locally with
        a bounded backoff before the trial is aborted.

        Raises
        ------
        RuntimeError
            - ``"NOVA_API_KEY environment variable is required for Nova
               provider"`` when the Nova path fires without a key.
            - ``"All API keys exhausted"`` after the last OpenRouter key
               hit a quota error.
            - ``LLMApiTimeoutError`` when the call times out repeatedly.
            - ``f"LLM API call failed: {e}"`` for any other provider error.
        """
        kwargs = dict(kwargs)
        kwargs.setdefault("timeout", self._api_call_timeout_s)

        while True:
            try:
                if self.provider == "nova":
                    from tolokaforge.secrets import get_default

                    kwargs["api_base"] = "https://api.nova.amazon.com/v1"
                    kwargs["api_key"] = get_default().get_secret("NOVA_API_KEY")
                    if not kwargs["api_key"]:
                        raise RuntimeError(
                            "NOVA_API_KEY is required for Nova provider — set it in .env "
                            "or the environment so SecretManager can resolve it"
                        )

                    kwargs["custom_llm_provider"] = "openai"

                    if kwargs["model"].startswith("nova/"):
                        kwargs["model"] = kwargs["model"][5:]

                    if not kwargs["model"].startswith("openai/"):
                        kwargs["model"] = f"openai/{kwargs['model']}"

                elif "/" in self.config.provider:
                    base_provider = self.config.provider.split("/")[0]
                    kwargs["custom_llm_provider"] = base_provider

                return self._call_completion_with_timeout_retry(kwargs)
            except LLMApiTimeoutError:
                # Pass through unchanged so ``_should_retry_exception`` can
                # opt the outer ``@retry`` out of re-attempting the call.
                # Wrapping it in a generic ``RuntimeError`` here would
                # destroy the subtype and let the outer retry blow the
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
                    if self._rotate_key():
                        self.logger.warning(
                            "API key exhausted, rotated to next key",
                            key_index=self._current_key_index,
                            remaining_keys=len(self._api_keys) - self._current_key_index - 1,
                        )
                        continue
                    self.logger.error("All API keys exhausted")
                    raise RuntimeError("All API keys exhausted") from e
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

        ``cost_usd`` is computed iff usage is populated; unknown pricing
        surfaces as ``None``.
        """
        choice = response.choices[0]
        message = choice.message

        text = message.content or ""
        tool_calls: list[ToolCall] = []

        reasoning_result = self.capabilities.reasoning_codec.extract(message)

        # Build per-tool root-level parameter type maps once per call so the
        # response policy's schema-aware coercions (currently: empty-container
        # recovery for ``array`` / ``object`` params) can fire on every
        # tool-call argument set the model emits.
        param_types_by_tool = _root_param_types_by_tool(sanitized_tools)

        if hasattr(message, "tool_calls") and message.tool_calls:
            for tc in message.tool_calls:
                arguments = self._parse_tool_arguments(tc.function.name, tc.function.arguments)
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
        )

        return GenerationResult(
            text=text,
            tool_calls=tool_calls,
            usage=usage,
            latency_s=latency_s,
            cost_usd=cost_usd,
            reasoning=reasoning_result,
            effective_system_prompt=effective_system_prompt,
        )

    # ------------------------------------------------------------------
    # Mock generator (offline tests)
    # ------------------------------------------------------------------

    def _mock_generate(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None,  # noqa: ARG002
    ) -> GenerationResult:
        """Deterministic mock responder for offline tests."""

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


class UserSimulator:
    """User simulator for benchmarking."""

    def __init__(
        self,
        mode: str = "scripted",
        llm_config: ModelConfig | None = None,
        persona: str = "cooperative",
        backstory: str | None = None,
        scripted_flow: list[dict[str, str]] | None = None,
        tool_schemas: list[dict[str, Any]] | None = None,
    ):
        self.mode = mode
        self.persona = persona
        self.backstory = backstory
        self.scripted_flow = scripted_flow or []
        self.tool_schemas = tool_schemas or []
        self.llm_client = LLMClient(llm_config) if llm_config and mode == "llm" else None
        # Stage 7 (P5) — the last system prompt emitted on an LLM reply. Scripted
        # simulators stay ``None`` forever. Callers (``TrialRunner``) capture
        # this after the first user turn and thread it onto ``Trajectory``.
        self.last_system_prompt: str | None = None

    def reply(self, context: list[Message]) -> GenerationResult:
        """Generate user reply based on context."""
        if self.mode == "scripted":
            text = self._scripted_reply(context)
            return GenerationResult(text=text, tool_calls=[])
        elif self.mode == "llm":
            return self._llm_reply(context)
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
        """Assemble the simulator system prompt from backstory + tool schemas.

        Stage 7 (P5): pure helper so that the prompt is observable without
        firing a generation. ``TrialRunner`` uses the post-``_llm_reply``
        ``last_system_prompt`` capture to land this value on
        ``Trajectory.user_system_prompt``. If the simulator prompt shape is
        revised, bump ``Trajectory.simulator_schema_version``.
        """
        instruction_display = (
            ("\n\nInstruction: " + self.backstory + "\n") if self.backstory else ""
        )

        tool_guidance = ""
        if self.tool_schemas:
            tool_guidance = """
- You have access to tools to check device status and perform actions. Use them when the agent asks you about your device state.
- ALWAYS use tools to ground your responses. For example, if the agent asks "what does your status bar show?", you must call check_status_bar tool first, then report the result.
- Never make up or hallucinate tool results. Always call the actual tool and report what it returns.
- If unsure whether you need to use a tool, prefer using it over making assumptions."""

        return f"""You are a user interacting with an agent.{instruction_display}
Rules:
- Just generate one line at a time to simulate the user's message.
- In your first message, clearly state the full request including ALL required steps, even if they must be done sequentially.
- After the first message, only provide information that is necessary for the current step unless the agent asks for details.
- Do not hallucinate information that is not provided in the instruction. For example, if the agent asks for the order id but it is not mentioned in the instruction, do not make up an order id, just say you do not remember or have it.
- If your instruction contains multiple numbered steps (Step 1, Step 2, etc.), you MUST complete ALL steps before ending the conversation. Track which steps you have completed.
- If the instruction includes sequential requirements using words like "after", "then", or "once", treat them as required steps and proactively mention the next step once the previous one is complete.
- If your instruction mentions specific apps or websites to use, you MUST explicitly mention those apps/websites in your first message.
- If your instruction includes verbs like save, shortlist, reserve, order, add to calendar, or take a note, you MUST explicitly include those actions in your first message.
- If the agent uses a different app or website than specified, correct them and restate the required app/website.
- If the agent performs the wrong task, selects the wrong restaurant/item/time/party size, or claims there are no results, correct them and restate the exact requirement. Do not accept alternative goals.
- When the agent asks "anything else?" or "Is there anything else I can help you with?", check if you have remaining steps. If yes, continue with the next step.
- Do not claim that you completed a required step yourself. Wait for the agent to complete steps, and only acknowledge completion after the agent explicitly confirms it.
- Only generate '###STOP###' when you have completed EVERY step in your instruction and the entire goal is satisfied, not partway through.
- Once the agent delivers the requested artifact/output, do not introduce new goals or remediation steps. Acknowledge completion and end with '###STOP###'.
- Do not repeat the exact instruction in the conversation. Instead, use your own words to convey the same information.
- Try to make the conversation as natural as possible, and stick to the personalities in the instruction.
- Never mention that this is a simulation, test, benchmark, prompt, or that you are an AI/model.{tool_guidance}"""

    def _llm_reply(self, context: list[Message]) -> GenerationResult:
        """Generate LLM-based user reply - tau-bench compatible with tool calling."""
        if not self.llm_client:
            raise RuntimeError("LLM client not initialized for LLM mode")

        # Stage 7 (P5) — capture every system prompt for runner-side persistence.
        system_prompt = self._build_system_prompt()
        self.last_system_prompt = system_prompt

        sim_context: list[Message] = []
        for msg in context:
            if msg.role == MessageRole.USER:
                sim_context.append(
                    Message(role=MessageRole.ASSISTANT, content=msg.content, ts=msg.ts)
                )
            elif msg.role == MessageRole.ASSISTANT:
                sim_context.append(Message(role=MessageRole.USER, content=msg.content, ts=msg.ts))

        if sim_context and sim_context[0].role == MessageRole.ASSISTANT:
            sim_context = sim_context[1:]

        result = self.llm_client.generate(
            system=system_prompt,
            messages=sim_context,
            tools=self.tool_schemas if self.tool_schemas else None,
            tool_choice="auto" if self.tool_schemas else None,
            temperature=0.2,
        )

        if result.tool_calls and not result.text.strip():
            result.text = "Let me check that."

        if result.text:
            result.text = self._sanitize_user_text(result.text)

        return result

    @staticmethod
    def _sanitize_user_text(text: str) -> str:
        banned = re.compile(
            r"\b(simulation|simulate|simulated|simulating|benchmark|prompt|ai|model|llm)\b",
            re.IGNORECASE,
        )
        if not text.strip():
            return text
        sentences = re.split(r"(?<=[.!?])\s+", text.strip())
        cleaned_sentences: list[str] = []
        for sentence in sentences:
            if banned.search(sentence):
                stripped = banned.sub("", sentence)
                stripped = re.sub(r"\s{2,}", " ", stripped).strip()
                if re.search(r"[A-Za-z]", stripped):
                    cleaned_sentences.append(stripped)
            else:
                cleaned_sentences.append(sentence)
        cleaned = " ".join(cleaned_sentences).strip()
        return cleaned or "Okay."
