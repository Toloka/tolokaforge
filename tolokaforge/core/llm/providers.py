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

from functools import cache
from importlib import resources

import yaml
from pydantic import BaseModel, ConfigDict

__all__ = [
    "ProviderBinding",
    "SlugRewrite",
    "get_provider_binding",
]


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
    rate_limit_patterns: tuple[str, ...] = ()
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
