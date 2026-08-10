"""Provider transport bindings — data snapshot of provider-specific engine knobs.

Provider-specific transport behaviour (endpoints, credential env-var names,
routability, rotation, slug rewrites, rate-limit text patterns) is declared
in ``tolokaforge/core/data/providers.yaml`` and consumed by the LLM client
through :class:`ProviderBinding`. Onboarding a new provider is one YAML
edit; extending rotation or a rate-limit pattern is one YAML edit.

The bundled snapshot loads once at import time via :func:`get_provider_binding`;
unknown provider names fall through to a default :class:`ProviderBinding` with
every field at its inert default, preserving today's behaviour for out-of-tree
RunConfig provider names.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import cache
from importlib import resources

import yaml
from pydantic import BaseModel, ConfigDict

__all__ = [
    "DEFAULT_RATE_LIMIT_PATTERNS",
    "ProviderBinding",
    "SlugRewrite",
    "compile_rate_limit_patterns",
    "get_provider_binding",
]


DEFAULT_RATE_LIMIT_PATTERNS: tuple[str, ...] = (
    # The class name litellm / openai put in the message itself, e.g.
    # "litellm.RateLimitError: RateLimitError: OpenrouterException - ...".
    r"\bRateLimitError\b",
    # 429 in a *status* position: "Error code: 429", "status_code=429",
    # "status 429", "HTTP/1.1 429". The trailing guard keeps it off longer
    # numbers, and requiring the keyword keeps it off token counts and ids.
    r"(?i)(?:error\s+code|status(?:[\s_-]*code)?|http(?:/[\d.]+)?)\s*[:=]?\s*429(?!\d)",
    # The HTTP reason phrase, with or without the numeric status.
    r"(?i)\btoo\s+many\s+requests\b",
    # Provider prose, but only in an error construction — "rate limit exceeded",
    # never a bare mention such as a docs link about rate limits and quotas.
    r"(?i)\brate[\s_-]?limit(?:s|ed|ing)?[\s:;,.-]*(?:error|exceeded|reached|hit)\b",
)
"""Anchored last-resort text shapes for the LLM client's rate-limit classifier.

An unanchored ``"429" in str(exc)`` matches token counts (``you requested
4429``), request ids (``req_8f429ab2``) and JSON bodies (``{'total_tokens':
429}``); an unanchored ``"rate limit" in ...`` matches an auth error whose
message links to rate-limit docs. Under probe mode a false positive hands a
*deterministic* failure the multi-hour fixed-interval budget and pollutes the
429 census the mode exists to produce, so each pattern requires a status
keyword, the HTTP reason phrase, or rate-limit prose in an error construction.
A bare ``429`` with no such context is deliberately NOT a match — guessing
would misroute control flow.

These are shapes an *engine* wrapper produces, not a catalogue of provider
quota prose. Vertex's ``RESOURCE_EXHAUSTED: Quota exceeded for quota metric``,
OpenAI's ``insufficient_quota``, ``TPM limit reached``, ``Requests limit
exceeded`` and Anthropic's ``overloaded_error`` all match **nothing** here and
are meant to: they arrive typed through litellm, so the type / status tier of
:meth:`LLMClient._is_rate_limit_exception` catches them and the text miss is
harmless. Adding prose for them would widen the false-positive surface for no
gain.

Every shipped provider in ``providers.yaml`` carries a verbatim copy of this
list. A new provider defaults to this shape unless its YAML overrides it.
"""


def compile_rate_limit_patterns(patterns: Iterable[str]) -> tuple[re.Pattern[str], ...]:
    """Compile ``patterns`` into an immutable tuple of regex objects.

    Flags are embedded inline (``(?i)``) rather than passed to :func:`re.compile`
    so a per-provider YAML entry can decide case-sensitivity per pattern.
    """
    return tuple(re.compile(pattern) for pattern in patterns)


class SlugRewrite(BaseModel):
    """Two-step rewrite of ``kwargs['model']`` applied per attempt.

    Step 1 drops ``strip_prefix`` if the string starts with it. Step 2
    prepends ``ensure_prefix`` unless already present. Empty strings on
    either field skip that step.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    strip_prefix: str = ""
    ensure_prefix: str = ""


class ProviderBinding(BaseModel):
    """Transport knobs for a single provider — one entry in ``providers.yaml``.

    Every field either mirrors an engine hardcode or is inert on the
    default. The engine reads the fields to shape wire-level behaviour
    (endpoint pinning, credential lookup, key rotation, slug rewrite,
    rate-limit classification).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint: str | None = None
    api_base_env: str | None = None
    api_key_env: str | None = None
    api_keys_env: str | None = None
    unroutable: bool = False
    custom_llm_provider: str | None = None
    rate_limit_patterns: tuple[str, ...] = DEFAULT_RATE_LIMIT_PATTERNS
    format_model_name_bare: bool = False
    kwargs_pin_transport: bool = False
    slug_rewrite: SlugRewrite | None = None


@cache
def _load_bundled_providers() -> dict[str, ProviderBinding]:
    raw = (
        resources.files("tolokaforge.core")
        .joinpath("data/providers.yaml")
        .read_text(encoding="utf-8")
    )
    payload = yaml.safe_load(raw) or {}
    if not isinstance(payload, dict):
        raise ValueError(
            "providers.yaml must be a mapping of provider name to binding; "
            f"got {type(payload).__name__}"
        )
    return {name: ProviderBinding.model_validate(spec) for name, spec in payload.items()}


def get_provider_binding(provider: str) -> ProviderBinding:
    """Look up ``provider``'s binding — default binding for unknown names.

    The lookup key is the first ``/``-separated segment lower-cased, so
    ``"openrouter/google"`` resolves to the ``openrouter`` entry. Names
    absent from ``providers.yaml`` fall through to a default
    :class:`ProviderBinding`.
    """
    key = (provider or "").split("/", 1)[0].lower()
    return _load_bundled_providers().get(key, ProviderBinding())
