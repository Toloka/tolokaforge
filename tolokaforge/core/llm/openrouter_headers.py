"""The headers the engine adds to every request an OpenRouter provider sends."""

from __future__ import annotations

import os
from enum import Enum

import litellm

__all__ = ["OpenRouterDefaultHeader", "is_openrouter_provider", "openrouter_default_headers"]


class OpenRouterDefaultHeader(str, Enum):
    REFERER = "HTTP-Referer"
    TITLE = "X-Title"
    DATA_COLLECTION_OPT_OUT = "X-Data-Collection-Opt-Out"


def is_openrouter_provider(provider: str) -> bool:
    """Whether a model config's ``provider`` sends the engine's OpenRouter defaults."""
    return provider.startswith("openrouter")


def openrouter_default_headers() -> dict[str, str]:
    """``litellm.openai_headers`` plus every default it does not already set; the
    global is set to the result."""
    existing_headers = dict(getattr(litellm, "openai_headers", {}) or {})

    referer = os.getenv("TOLOKAFORGE_OPENROUTER_REFERER", "https://github.com/Toloka-F/tolokaforge")
    title = os.getenv("TOLOKAFORGE_OPENROUTER_TITLE", "Tolokaforge Evaluation")

    existing_headers.setdefault(OpenRouterDefaultHeader.REFERER.value, referer)
    existing_headers.setdefault(OpenRouterDefaultHeader.TITLE.value, title)

    opt_out_pref = os.getenv("TOLOKAFORGE_OPENROUTER_OPT_OUT", "true").lower()
    if opt_out_pref in {"1", "true", "yes", "on"}:
        existing_headers.setdefault(OpenRouterDefaultHeader.DATA_COLLECTION_OPT_OUT.value, "true")

    litellm.openai_headers = existing_headers
    return existing_headers
