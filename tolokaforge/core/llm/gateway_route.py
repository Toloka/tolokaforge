"""Which name does a model answer to on the gateway, and does it answer at all.

See ``docs/LLM_LAYER.md`` § "Speaking to the gateway" for why the gateway needs its
own model name and its own dialect, and what goes wrong without them.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tolokaforge.core.llm.proxy import ProxyConfig

__all__ = [
    "GatewayRouteError",
    "resolve_gateway_route",
    "fetch_gateway_catalog",
    "clear_catalog_cache",
]

logger = logging.getLogger(__name__)

#: Cached per base URL. A catalog changes when an operator edits the gateway, not
#: within a run, and a run builds one client per role per trial.
_CATALOG_CACHE: dict[str, frozenset[str] | None] = {}


class GatewayRouteError(Exception):
    """The gateway serves this model under more than one name and none was chosen."""


def clear_catalog_cache() -> None:
    """Drop the cached catalogs. For tests and long-lived processes."""
    _CATALOG_CACHE.clear()


def fetch_gateway_catalog(proxy: ProxyConfig, timeout: int = 15) -> frozenset[str] | None:
    """Route ids the gateway serves, or ``None`` when the catalog cannot be read.

    ``None`` is "no information", never an error to propagate: an unreadable catalog
    must leave routing exactly as it was rather than fail a run.
    """
    key = proxy.base_url
    if key in _CATALOG_CACHE:
        return _CATALOG_CACHE[key]

    url = proxy.base_url.rstrip("/") + "/models"
    headers = {"Accept": "application/json", **proxy.request_headers()}
    if proxy.api_key:
        headers["Authorization"] = f"Bearer {proxy.api_key}"
    try:
        with urllib.request.urlopen(  # noqa: S310 - operator-supplied gateway URL
            urllib.request.Request(url, headers=headers), timeout=timeout
        ) as response:
            payload = json.load(response)
        entries = payload.get("data")
        if not isinstance(entries, list):
            raise ValueError(f"expected a data list, got {type(entries).__name__}")
        catalog: frozenset[str] | None = frozenset(
            str(e["id"]) for e in entries if isinstance(e, dict) and e.get("id")
        )
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError, OSError) as exc:
        logger.warning("Gateway catalog unreadable at %s: %s", url, exc)
        catalog = None

    _CATALOG_CACHE[key] = catalog
    return catalog


def _candidates(model_string: str) -> list[str]:
    """The names this model could answer to on the gateway, most specific first.

    A gateway entry is either the engine's full ``<provider>/<name>`` (the gateway
    proxies that provider) or the bare ``<name>`` (the gateway serves the upstream
    itself under the name's own namespace).
    """
    head, _, tail = model_string.partition("/")
    return [model_string] if not tail else [model_string, tail]


def resolve_gateway_route(
    model_string: str,
    catalog: frozenset[str] | None,
    preferred_prefix: str | None = None,
) -> str | None:
    """Return the gateway's name for ``model_string``, or ``None`` if it serves none.

    Args:
        model_string: The engine's litellm model string, ``<provider>/<name>``.
        catalog: Route ids from :func:`fetch_gateway_catalog`; ``None`` disables routing.
        preferred_prefix: Namespace that wins when the gateway serves the model under
            more than one name.

    Raises:
        GatewayRouteError: Several names match and ``preferred_prefix`` picks none of
            them. Guessing is refused: the names can be backed by different upstreams,
            which changes the serving path rather than the transport.
    """
    if not catalog:
        return None

    hits = [c for c in _candidates(model_string) if c in catalog]
    if not hits:
        return None
    if len(hits) == 1:
        return hits[0]

    if preferred_prefix:
        for hit in hits:
            if hit.startswith(preferred_prefix):
                return hit
    raise GatewayRouteError(
        f"the gateway serves {model_string!r} under {len(hits)} names ({', '.join(hits)}) "
        f"and no preference picks one. They can be backed by different upstreams, so "
        f"this is a serving-path choice rather than a transport detail: set "
        f"LLM_PROXY_PREFERRED_ROUTE to the namespace you want."
    )
