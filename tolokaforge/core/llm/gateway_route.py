"""Which name does a model answer to on the gateway, and does it answer at all.

See ``docs/LLM_LAYER.md`` § "Speaking to the gateway" for why the gateway needs its
own model name and its own dialect, and what goes wrong without them.
"""

from __future__ import annotations

import http.client
import json
import logging
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from tolokaforge.core.llm.proxy import ProxyConfig

__all__ = [
    "GatewayRouteError",
    "ResolvedGatewayRoute",
    "RouteKind",
    "resolve_gateway_route",
    "fetch_gateway_catalog",
    "clear_catalog_cache",
]

#: How a route matched the catalog. Named so the run artifacts, the client field and
#: the automation's availability lookup all state the same two-value contract.
RouteKind = Literal["exact", "wildcard"]

logger = logging.getLogger(__name__)

#: Successful, non-empty reads only, keyed by base URL: a catalog changes when an
#: operator edits the gateway, not within a run, while a failure is transient and
#: caching it would keep a whole run degraded after one blip.
_CATALOG_CACHE: dict[str, frozenset[str]] = {}

#: When a base URL may be retried again, so a gateway that HANGS costs the timeout
#: occasionally rather than on every client construction. Time-based rather than a
#: hard cap: constructions cluster at run start, so a cap would burn every attempt
#: during warmup and pin a long run on the degraded path with no way back.
_RETRY_AFTER: dict[str, float] = {}

#: How long to stop asking after a failure.
CATALOG_RETRY_COOLDOWN_S = 60.0


class ResolvedGatewayRoute(str):
    """A gateway route name that also says HOW it matched.

    A ``str`` subclass so every existing caller keeps treating the route as
    the wire model name, while ``kind`` records whether the catalog held the
    exact entry ("exact") or only the model's own namespace wildcard
    ("wildcard") - a provenance distinction the run artifacts carry.
    """

    kind: RouteKind

    def __new__(cls, name: str, kind: RouteKind) -> ResolvedGatewayRoute:
        route = super().__new__(cls, name)
        route.kind = kind
        return route

    def __getnewargs__(self) -> tuple[str, RouteKind]:
        # str.__getnewargs__ returns only the string value, so copy.copy,
        # copy.deepcopy and pickle would reconstruct with a missing ``kind``
        # and die - and dataclasses.asdict deepcopies every ProviderRawCall
        # on its way into metrics.yaml, which is every routed call's path.
        return (str(self), self.kind)


class GatewayRouteError(Exception):
    """The gateway serves this model under more than one name and none was chosen."""


def clear_catalog_cache() -> None:
    """Drop the cached catalogs and the failure counters. For tests."""
    _CATALOG_CACHE.clear()
    _RETRY_AFTER.clear()


def fetch_gateway_catalog(proxy: ProxyConfig, timeout: int = 15) -> frozenset[str] | None:
    """Route ids the gateway serves, or ``None`` when the catalog cannot be read.

    ``None`` is "no information", never an error to propagate: the caller keeps the
    gateway and skips rewriting rather than failing a run. An **empty** answer counts
    as unreadable, since a gateway that serves nothing is broken rather than
    authoritative.
    """
    key = proxy.base_url
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cached
    if time.monotonic() < _RETRY_AFTER.get(key, 0.0):
        return None

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
        # An empty catalog is a broken answer, not an authoritative "serves nothing":
        # treating it as authoritative would take the run off the gateway.
        served = frozenset(str(e["id"]) for e in entries if isinstance(e, dict) and e.get("id"))
        if not served:
            raise ValueError("the catalog is empty")
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        TimeoutError,
        ValueError,
        OSError,
    ) as exc:
        _RETRY_AFTER[key] = time.monotonic() + CATALOG_RETRY_COOLDOWN_S
        logger.warning("Gateway catalog unreadable at %s: %s", url, exc)
        return None

    _RETRY_AFTER.pop(key, None)
    _CATALOG_CACHE[key] = served
    return served


def _candidates(model_string: str) -> list[str]:
    """The names this model could answer to on the gateway, most specific first.

    A gateway entry is either the engine's full ``<provider>/<name>`` (the gateway
    proxies that provider) or the bare ``<name>`` (the gateway serves the upstream
    itself under the name's own namespace).
    """
    _, _, tail = model_string.partition("/")
    return [model_string] if not tail else [model_string, tail]


def resolve_gateway_route(
    model_string: str,
    catalog: frozenset[str] | None,
    preferred_prefix: str | None = None,
    *,
    trust_namespace_wildcards: bool = False,
) -> ResolvedGatewayRoute | None:
    """Return the gateway's name for ``model_string``, or ``None`` if it serves none.

    Args:
        model_string: The engine's litellm model string, ``<provider>/<name>``.
        catalog: Route ids from :func:`fetch_gateway_catalog`. ``None`` means the
            catalog could not be read, which the caller treats as "keep the gateway,
            skip rewriting" rather than as "not served".
        preferred_prefix: Namespace(s) that win when the gateway serves the model
            under more than one name. A comma-separated list is honoured in
            order (first matching prefix wins), so a deployment serving several
            provider namespaces can rank them: ``"openrouter/,nebius/"``.
        trust_namespace_wildcards: Whether a catalog entry of ``<ns>/*`` counts
            as a route for a model whose OWN provider namespace is ``<ns>`` (an
            ``openrouter/*`` passthrough then serves ``provider: openrouter``
            models, addressed by their untranslated name). Namespace-matched on
            purpose: a foreign wildcard is a DIFFERENT upstream, the measured
            serving-path break documented in ``docs/LLM_LAYER.md``. Exact
            entries always win over a wildcard.

    Raises:
        GatewayRouteError: Several names match and ``preferred_prefix`` picks none of
            them. Guessing is refused: the names can be backed by different upstreams,
            which changes the serving path rather than the transport.
    """
    if not catalog:
        return None

    hits = [c for c in _candidates(model_string) if c in catalog]
    if not hits:
        if trust_namespace_wildcards and "/" in model_string:
            namespace = model_string.split("/", 1)[0]
            if f"{namespace}/*" in catalog:
                return ResolvedGatewayRoute(model_string, kind="wildcard")
        return None
    if len(hits) == 1:
        return ResolvedGatewayRoute(hits[0], kind="exact")

    if preferred_prefix:
        prefixes = [p.strip() for p in preferred_prefix.split(",") if p.strip()]
        for prefix in prefixes:
            for hit in hits:
                if hit.startswith(prefix):
                    return ResolvedGatewayRoute(hit, kind="exact")
    raise GatewayRouteError(
        f"the gateway serves {model_string!r} under {len(hits)} names ({', '.join(hits)}) "
        f"and no preference picks one. They can be backed by different upstreams, so "
        f"this is a serving-path choice rather than a transport detail: set "
        f"LLM_PROXY_PREFERRED_ROUTE to the namespace you want "
        f"(a comma-separated list is honoured in order)."
    )
