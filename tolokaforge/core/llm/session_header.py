"""Refusal of a ``ModelConfig.session`` header another header source also sets.

A model's session header is merged into the same ``extra_headers`` dict as the
engine's OpenRouter defaults and the gateway's ``LLM_PROXY_HEADERS`` /
``LLM_PROXY_REQUEST_ID_HEADER``; a shared name would let one silently replace
the other on every request. The rule is fed the env-resolved, pre-catalog
:func:`~tolokaforge.core.llm.proxy.resolve_proxy_config` at every site
(``LLMClient`` construction, run start, ``config validate``), so each gives one
verdict for one config and environment. Run start and ``config validate`` read
that environment only when some model config declares ``session``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from tolokaforge.core.llm.openrouter_headers import OpenRouterDefaultHeader, is_openrouter_provider
from tolokaforge.core.llm.proxy import (
    ENV_HEADERS,
    ENV_REQUEST_ID_HEADER,
    ProxyConfig,
    resolve_proxy_config,
)

if TYPE_CHECKING:
    from tolokaforge.core.models import ModelConfig

__all__ = [
    "SessionHeaderConflictError",
    "session_header_conflict",
    "session_header_conflicts",
]

_OPENROUTER_DEFAULTS_SOURCE = "the engine's OpenRouter default headers"


class SessionHeaderConflictError(ValueError):
    """A ``session.header`` shares its name with another header source."""

    def __init__(self, *, path: str, header: str, source: str):
        self.path = path
        self.header = header
        self.source = source
        self.reason = (
            f"{header!r} is also set by {source}, so one would overwrite the other on "
            f"every request. Pick another session header name."
        )
        super().__init__(f"{path}: {self.reason}")

    def __reduce__(self) -> tuple[Callable[..., SessionHeaderConflictError], tuple[str, str, str]]:
        return _rebuild_conflict, (self.path, self.header, self.source)


def _rebuild_conflict(path: str, header: str, source: str) -> SessionHeaderConflictError:
    return SessionHeaderConflictError(path=path, header=header, source=source)


def _competing_sources(cfg: ModelConfig, proxy: ProxyConfig | None) -> list[tuple[str, str]]:
    """``(header name, source)`` for every header the engine sends beside ``cfg``'s session."""
    sources: list[tuple[str, str]] = []
    if proxy is not None and proxy.applies_to(cfg.provider):
        if proxy.request_id_header:
            sources.append((proxy.request_id_header, ENV_REQUEST_ID_HEADER))
        sources.extend((name, ENV_HEADERS) for name in proxy.headers)
    if is_openrouter_provider(cfg.provider):
        sources.extend(
            (header.value, _OPENROUTER_DEFAULTS_SOURCE) for header in OpenRouterDefaultHeader
        )
    return sources


def session_header_conflict(
    cfg: ModelConfig, proxy: ProxyConfig | None, *, header_path: str
) -> SessionHeaderConflictError | None:
    """The first source ``cfg``'s session header collides with (case-insensitive), or ``None``."""
    if cfg.session is None:
        return None
    header = cfg.session.header
    for name, source in _competing_sources(cfg, proxy):
        if name.lower() == header.lower():
            return SessionHeaderConflictError(path=header_path, header=header, source=source)
    return None


def session_header_conflicts(
    models: Mapping[str, ModelConfig],
) -> list[tuple[str, SessionHeaderConflictError]]:
    """Every model config, fallbacks included, whose session header collides with
    this environment's header sources.

    ``config validate`` reports all of them and ``run`` / ``prepare`` /
    ``worker`` raise the first, so both refuse the same configs. The gateway
    environment is resolved only when some config declares ``session``, so its
    :class:`~tolokaforge.core.llm.proxy.ProxyConfigError` reaches no other config.
    """
    from tolokaforge.core.models.run_config import iter_model_configs

    configs = list(iter_model_configs(models))
    if not any(cfg.session for _, cfg in configs):
        return []
    proxy = resolve_proxy_config()
    conflicts: list[tuple[str, SessionHeaderConflictError]] = []
    for path, cfg in configs:
        conflict = session_header_conflict(cfg, proxy, header_path=f"{path}.session.header")
        if conflict is not None:
            conflicts.append((path, conflict))
    return conflicts
