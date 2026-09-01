"""Normalized token / cache usage accounting.

Pulls the litellm-canonical fields from a ``ModelResponse.usage`` object.
Missing attributes default to ``0`` and never raise — usage extraction is
telemetry, not control flow.

Canonical field paths (verified against litellm v1.83 docs)::

    usage.prompt_tokens
    usage.completion_tokens
    usage.prompt_tokens_details.cached_tokens
    usage.completion_tokens_details.reasoning_tokens
    usage.cache_creation_input_tokens   # Anthropic-only (direct API)
    usage.cache_read_input_tokens       # Anthropic-only (direct API)

**Dual-path cache accounting.** Anthropic cache counters surface under two
different paths depending on provider routing:

- **Direct Anthropic API:** ``usage.cache_creation_input_tokens`` and
  ``usage.cache_read_input_tokens`` (top-level on the usage block).
- **OpenRouter-routed Anthropic:** ``usage.prompt_tokens_details.cache_write_tokens``
  carries cache writes; cache reads land in the OpenAI-canonical
  ``usage.prompt_tokens_details.cached_tokens`` (singular). OpenRouter does
  *not* emit a separate ``cache_read_tokens`` key — verified by live probe
  2026-04-27. See https://openrouter.ai/docs/use-cases/prompt-caching for
  the OpenRouter-side contract.

:class:`UsageExtractor` reads both, with top-level winning when non-zero. This
keeps direct-Anthropic behaviour identical while surfacing OpenRouter-routed
cache activity into the same ``Usage.cache_creation_input_tokens`` /
``cache_read_input_tokens`` fields — downstream observability code (metrics
aggregation, analytics tools) stays routing-agnostic.

See plan § Canonical litellm surface for the full surface.

**OpenRouter generation id.** Alongside the usage block, an OpenRouter-served
response carries the id of the generation it produced. It is the only key that
joins a call we made to the routing decision OpenRouter made for it, so
:class:`UsageExtractor` lifts it onto the call record — see
:func:`extract_openrouter_generation_id` and ``docs/LLM_LAYER.md``
§ "OpenRouter generation ids".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "OPENROUTER_GENERATION_ID_HEADER",
    "CostSource",
    "ProviderRawCall",
    "Usage",
    "UsageExtractor",
    "extract_openrouter_generation_id",
]


CostSource = Literal["litellm", "local", "unknown"]
"""Which pricing path filled :attr:`ProviderRawCall.cost_usd`.

* ``"litellm"`` — pulled from ``response._hidden_params['response_cost']``
  (provider-authoritative, cache-aware).
* ``"local"`` — fell back to :data:`tolokaforge.core.pricing.MODEL_PRICING`
  because litellm could not price the model.
* ``"unknown"`` — neither path produced a value; ``cost_usd is None``.
"""


@dataclass(frozen=True)
class ProviderRawCall:
    """Per-call provider metrics — the unit record for trial-level analytics.

    One ``ProviderRawCall`` is appended to :attr:`Usage.calls` for every
    LLM API call, so a dialogue's full cost / latency / cache history is
    preserved (instead of being collapsed via "latest wins" on
    ``provider_raw``). Downstream analytics walk this list to answer
    "which call stalled?" and "which calls were cache-hits?".
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float | None = None
    cost_source: CostSource = "unknown"
    latency_s: float = 0.0
    gateway_route: str | None = None
    """The gateway route name this call was addressed to, else ``None``.

    ``None`` on the direct-provider path and on the unreadable-catalog path
    (where the untranslated model string goes to the gateway unresolved).
    """

    gateway_route_kind: str | None = None
    """How the route matched the catalog: ``"exact"`` or ``"wildcard"``.

    The serving-path provenance a board audit needs: a wildcard route says
    the gateway forwarded by namespace rather than by an explicit entry.
    """

    openrouter_generation_id: str | None = None
    """OpenRouter's id for the generation this call produced, else ``None``.

    ``None`` for every route that is not OpenRouter — no other provider sends
    the header this is read from. See
    :func:`extract_openrouter_generation_id`."""


@dataclass(frozen=True)
class Usage:
    """Normalized token + cache accounting across providers.

    Every field is populated from the litellm-canonical ``ModelResponse.usage``
    surface by :class:`UsageExtractor` — see module docstring for the exact
    source paths. Missing attributes are coerced to ``0``; ``provider_raw``
    carries the forensic dump of the raw provider-specific block.

    **Per-call history (``calls``).** Each LLM API call produces one
    :class:`ProviderRawCall` appended to this tuple. ``__add__``
    concatenates, so a runner-accumulated ``Usage`` carries the full
    trial timeline — readers walk ``calls`` for per-call cost / latency
    diagnostics, while the flat token fields stay as a fast aggregate sum.

    **Accumulation semantics (``__add__``).** Token / cache counters sum
    field-wise. ``calls`` concatenates. ``provider_raw`` follows
    "latest wins" — the right-hand operand's dict is preserved (the
    raw shapes differ per call, so merging would be lossy).
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    provider_raw: dict[str, Any] = field(default_factory=dict)
    calls: tuple[ProviderRawCall, ...] = ()

    def __add__(self, other: Usage) -> Usage:
        """Combine two :class:`Usage` totals field-wise.

        ``provider_raw`` follows a "latest wins" convention; ``calls``
        concatenates (see class docstring). Returns ``NotImplemented``
        when ``other`` is not a :class:`Usage`, so Python falls back to
        ``__radd__`` / raises ``TypeError`` naturally.
        """
        if not isinstance(other, Usage):
            return NotImplemented
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
            cache_creation_input_tokens=(
                self.cache_creation_input_tokens + other.cache_creation_input_tokens
            ),
            cache_read_input_tokens=(self.cache_read_input_tokens + other.cache_read_input_tokens),
            provider_raw=dict(other.provider_raw) if other.provider_raw else {},
            calls=self.calls + other.calls,
        )


OPENROUTER_GENERATION_ID_HEADER = "x-generation-id"
"""Response header carrying OpenRouter's generation id.

The name is ``x-generation-id``, **not** ``x-openrouter-generation-id`` —
verified by live probe of ``openrouter.ai/api/v1/chat/completions``
2026-08-19, which returned ``x-generation-id: gen-…`` and no other
generation-bearing header. OpenRouter is the only provider we route to that
sends it, which is what makes its presence a sufficient test.
"""

_LITELLM_PROVIDER_HEADER_PREFIX = "llm_provider-"
"""Prefix litellm stamps on every raw upstream header it forwards.

``process_response_headers`` re-keys provider headers as
``llm_provider-<name>`` before they land in
``_hidden_params['additional_headers']``, so the bare name never appears.
Stripped rather than hardcoded into the lookup so a direct-httpx caller
handing us unprefixed headers resolves identically.
"""


def extract_openrouter_generation_id(response: Any) -> str | None:
    """Lift OpenRouter's generation id off a litellm response, else ``None``.

    The id is a *response header* value, surfaced by litellm on
    ``response._hidden_params['additional_headers']``. Querying
    ``https://openrouter.ai/api/v1/generation?id=<id>`` afterwards reports
    which upstream provider actually served the call — the retroactive
    disambiguation this exists for (``docs/LLM_LAYER.md``
    § "OpenRouter generation ids").

    Returns ``None`` whenever the header is absent, which is the normal state
    for every non-OpenRouter route (Anthropic direct, Google direct, …). Never
    raises: like the rest of this module, this is telemetry, not control flow.
    """
    hidden = getattr(response, "_hidden_params", None)
    if not isinstance(hidden, dict):
        return None
    headers = hidden.get("additional_headers")
    if not isinstance(headers, dict):
        return None
    for key, value in headers.items():
        if not isinstance(key, str):
            continue
        name = key.lower().removeprefix(_LITELLM_PROVIDER_HEADER_PREFIX)
        if name == OPENROUTER_GENERATION_ID_HEADER and value:
            # Some litellm transport paths hand headers back as ``bytes``;
            # ``str(b'gen-…')`` produces the literal ``"b'gen-…'"`` string,
            # which would then 404 on OpenRouter's /generation endpoint.
            if isinstance(value, bytes):
                return value.decode("utf-8", errors="replace")
            return str(value)
    return None


def _int_attr(obj: Any, name: str) -> int:
    """Defensive integer pull from nested response attributes."""
    if obj is None:
        return 0
    value = getattr(obj, name, None)
    if value is None and isinstance(obj, dict):
        value = obj.get(name)
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _sub_obj(obj: Any, name: str) -> Any:
    """Return ``obj.name`` or ``obj[name]`` if present, else ``None``."""
    if obj is None:
        return None
    value = getattr(obj, name, None)
    if value is None and isinstance(obj, dict):
        value = obj.get(name)
    return value


def _jsonify(value: Any) -> Any:
    """Recursively coerce a value to a Pydantic/JSON-serializable primitive.

    Handles dicts, lists, ``SimpleNamespace``, and any object exposing
    ``model_dump`` / ``dict`` / ``to_dict``. Leaves primitives unchanged.
    The result is always safe to drop into ``Usage.provider_raw`` and
    subsequently ``Metrics.model_dump(mode="json")``.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {
            k: _jsonify(v)
            for k, v in value.items()
            if not (isinstance(k, str) and k.startswith("_"))
        }
    if isinstance(value, (list, tuple)):
        return [_jsonify(v) for v in value]
    for method_name in ("model_dump", "dict", "to_dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                result = method()
            except Exception:
                continue
            if isinstance(result, dict):
                return _jsonify(result)
    # Plain namespace / object with __dict__ → shallow + recurse
    try:
        raw = vars(value)
    except TypeError:
        return str(value)
    return {k: _jsonify(v) for k, v in raw.items() if not k.startswith("_")}


def _to_provider_raw(usage: Any) -> dict[str, Any]:
    """Best-effort serialization of the provider usage block for forensics.

    Guarantees the result is a dict whose leaves are JSON-primitive, so the
    :class:`Usage` dataclass remains Pydantic-serializable end-to-end
    (Stage 5 pipeline requirement).
    """
    if usage is None:
        return {}
    out = _jsonify(usage)
    return out if isinstance(out, dict) else {}


class UsageExtractor:
    """Pull a :class:`Usage` from a ``ModelResponse``-shaped object.

    Never raises — missing attributes become zeros so that observability
    degrades gracefully when a provider returns a partial usage block.
    Each call also yields a :class:`ProviderRawCall` in ``Usage.calls``
    when usage is present, carrying the per-call ``cost_usd``,
    ``cost_source``, and ``latency_s`` supplied by the caller, plus the
    ``openrouter_generation_id`` read off the response's own headers.
    """

    def extract(
        self,
        response: Any,
        *,
        latency_s: float = 0.0,
        cost_usd: float | None = None,
        cost_source: CostSource = "unknown",
        gateway_route: str | None = None,
        gateway_route_kind: str | None = None,
    ) -> Usage:
        usage = getattr(response, "usage", None)
        if usage is None and isinstance(response, dict):
            usage = response.get("usage")
        if usage is None:
            return Usage()

        prompt = _int_attr(usage, "prompt_tokens")
        completion = _int_attr(usage, "completion_tokens")
        cache_creation = _int_attr(usage, "cache_creation_input_tokens")
        cache_read = _int_attr(usage, "cache_read_input_tokens")

        prompt_details = _sub_obj(usage, "prompt_tokens_details")
        cached = _int_attr(prompt_details, "cached_tokens")

        completion_details = _sub_obj(usage, "completion_tokens_details")
        reasoning = _int_attr(completion_details, "reasoning_tokens")

        # OpenRouter-routed Anthropic surfaces cache counters under
        # `prompt_tokens_details.{cache_write_tokens, cached_tokens}` and zeroes
        # the top-level `cache_{creation,read}_input_tokens` that direct Anthropic
        # populates. The cache-READ key is the OpenAI-canonical `cached_tokens`
        # (singular); OpenRouter does NOT emit a separate `cache_read_tokens`
        # — verified by live probe of openrouter/anthropic/claude-opus-4.7
        # on 2026-04-27. Fall back to the nested path only when the top-level
        # field is zero, so direct-Anthropic routing is never overridden.
        # Ref: https://openrouter.ai/docs/use-cases/prompt-caching
        if cache_creation == 0:
            nested_write = _int_attr(prompt_details, "cache_write_tokens")
            if nested_write > 0:
                cache_creation = nested_write
        if cache_read == 0 and cached > 0:
            cache_read = cached

        call = ProviderRawCall(
            prompt_tokens=prompt,
            completion_tokens=completion,
            cached_tokens=cached,
            reasoning_tokens=reasoning,
            cache_creation_input_tokens=cache_creation,
            cache_read_input_tokens=cache_read,
            cost_usd=cost_usd,
            cost_source=cost_source,
            latency_s=latency_s,
            gateway_route=gateway_route,
            gateway_route_kind=gateway_route_kind,
            openrouter_generation_id=extract_openrouter_generation_id(response),
        )

        return Usage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            reasoning_tokens=reasoning,
            cached_tokens=cached,
            cache_creation_input_tokens=cache_creation,
            cache_read_input_tokens=cache_read,
            provider_raw=_to_provider_raw(usage),
            calls=(call,),
        )
