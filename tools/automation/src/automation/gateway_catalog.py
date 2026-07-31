"""Is a candidate model also reachable through an LLM gateway?

The Slack-triggered integration flow resolves a request to an **OpenRouter slug**
and integrates it over OpenRouter. That stays the default. This module answers a
second, advisory question: *could this model also be served by the deployment's
LLM gateway (a LiteLLM proxy or equivalent)?* — so the reply can say so, and a
requester can ask for that route explicitly.

Why it is worth reporting rather than switching automatically: a gateway route
may be backed by a different upstream than OpenRouter for the same model name.
That changes the serving path, which is a leaderboard-comparability decision, not
a transport detail. The automaton surfaces the option; a human picks it.

Route-name convention
---------------------

A gateway that fronts OpenRouter typically exposes the slug under an
``openrouter/`` prefix (``openrouter/x-ai/grok-4.5``), because that is how the
underlying library namespaces it. Some gateways also register a wildcard
passthrough (``openrouter/*``) that covers every slug without listing it.

Those two are **not** equally strong evidence, so they are reported separately:
an exact entry means someone configured this model; a wildcard means the gateway
will *probably* accept it, and only a live call proves it. Nothing here calls the
model — availability is a catalog lookup.

Requires no credentials to be useful: with no gateway configured, every lookup
returns ``unknown`` and the flow is unchanged.
"""

from __future__ import annotations

import dataclasses
import json
import urllib.error
import urllib.request

#: Route identifiers accepted in a request and threaded into the integration run.
ROUTE_OPENROUTER = "openrouter"
ROUTE_GATEWAY = "litellm"

#: The default route. OpenRouter is what the leaderboard is calibrated on, so a
#: request that does not say otherwise must not silently change serving path.
DEFAULT_ROUTE = ROUTE_OPENROUTER

STATUS_EXACT = "exact"
STATUS_WILDCARD = "wildcard"
STATUS_ABSENT = "absent"
STATUS_UNKNOWN = "unknown"


@dataclasses.dataclass(frozen=True)
class Availability:
    """Whether a slug is reachable on the gateway, and under what route name.

    ``status`` is one of :data:`STATUS_EXACT` (a catalog entry names this model),
    :data:`STATUS_WILDCARD` (a passthrough covers it), :data:`STATUS_ABSENT` (the
    catalog was read and does not cover it), or :data:`STATUS_UNKNOWN` (no
    gateway configured, or the catalog could not be read).
    """

    slug: str
    status: str
    route: str | None = None

    @property
    def reachable(self) -> bool:
        """True when the gateway can plausibly serve this model."""
        return self.status in (STATUS_EXACT, STATUS_WILDCARD)


def fetch_gateway_catalog(
    base_url: str | None, api_key: str | None, timeout: int = 15
) -> list[str] | None:
    """Route ids the gateway serves, or ``None`` when it cannot be read.

    ``None`` is not an error to propagate: an unconfigured or unreachable gateway
    must leave the integration flow exactly as it was, so callers treat it as
    "no information" rather than failing the poll.
    """
    if not base_url or not base_url.strip():
        return None
    url = base_url.strip().rstrip("/") + "/models"
    headers = {"Accept": "application/json"}
    if api_key and api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"
    try:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
    entries = data.get("data")
    if not isinstance(entries, list):
        return None
    return sorted(
        str(entry["id"]) for entry in entries if isinstance(entry, dict) and entry.get("id")
    )


def lookup(slug: str, catalog: list[str] | None) -> Availability:
    """Classify how (or whether) ``slug`` is reachable on the gateway.

    Checks, in descending strength: the prefixed route, the bare slug, then a
    prefix wildcard.
    """
    if catalog is None:
        return Availability(slug=slug, status=STATUS_UNKNOWN)

    entries = set(catalog)
    prefixed = f"{ROUTE_OPENROUTER}/{slug}"
    if prefixed in entries:
        return Availability(slug=slug, status=STATUS_EXACT, route=prefixed)
    if slug in entries:
        return Availability(slug=slug, status=STATUS_EXACT, route=slug)
    if f"{ROUTE_OPENROUTER}/*" in entries:
        return Availability(slug=slug, status=STATUS_WILDCARD, route=prefixed)
    return Availability(slug=slug, status=STATUS_ABSENT)


def describe(availability: Availability) -> str:
    """One-line Slack mrkdwn phrase for a reply. Empty when there is nothing to say."""
    if availability.status == STATUS_EXACT:
        return f"also on the gateway as `{availability.route}`"
    if availability.status == STATUS_WILDCARD:
        return (
            f"probably reachable on the gateway as `{availability.route}` "
            f"(matched a passthrough, not an explicit entry)"
        )
    if availability.status == STATUS_ABSENT:
        return "not on the gateway"
    return ""


def as_dict(availability: Availability) -> dict:
    """JSON-friendly view for the plan / CLI."""
    return {
        "slug": availability.slug,
        "status": availability.status,
        "route": availability.route,
    }
