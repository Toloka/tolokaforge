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

Which name reaches the gateway
------------------------------

The engine resolves the gateway's route name from this same catalog and addresses
the gateway by it (``tolokaforge.core.llm.gateway_route``), so :func:`lookup` asks
THAT resolver rather than restating its rules. Both shapes are reachable - the
prefixed ``openrouter/<slug>`` and the bare ``<slug>`` - and a wildcard entry is
honoured only for the namespace the run addresses, because a foreign namespace is
a different upstream. A wildcard still only says the gateway will forward the
request, not that the model exists behind it.

An exact entry and a wildcard are **not** equally strong evidence, so they are
reported separately: an exact entry means someone configured this model; a
wildcard means the gateway will *probably* accept it, and only a live call proves
it. Nothing here calls the model — availability is a catalog lookup.

Requires no credentials to be useful: with no gateway configured, every lookup
returns ``unknown`` and the flow is unchanged.

The catalog itself is fetched by the engine (:mod:`tolokaforge.core.llm.gateway_route`)
through a :class:`~tolokaforge.core.llm.proxy.ProxyConfig`, so the poller and the
integration run read the same gateway the same way. See ``docs/LLM_LAYER.md``
§ "Speaking to the gateway" for why this module owns no transport of its own.
"""

from __future__ import annotations

import dataclasses
import os
import sys

from tolokaforge.core.llm.gateway_route import (
    GatewayRouteError,
    fetch_gateway_catalog,
    resolve_gateway_route,
)
from tolokaforge.core.llm.proxy import ProxyConfigError, resolve_proxy_config
from tolokaforge.secrets import EnvProvider, SecretManager

#: Route identifiers accepted in a request and threaded into the integration run.
ROUTE_OPENROUTER = "openrouter"
ROUTE_GATEWAY = "litellm"

#: The default route. OpenRouter is what the leaderboard is calibrated on, so a
#: request that does not say otherwise must not silently change serving path.
DEFAULT_ROUTE = ROUTE_OPENROUTER

#: The provider an OpenRouter-sourced slug runs under (``integrate-model.yml``). The engine
#: builds its model string as ``<provider>/<slug>`` and resolves the gateway route from
#: THAT, so a lookup asking about any other name would answer a question nobody asked.
RUN_PROVIDER = "openrouter"

STATUS_EXACT = "exact"
STATUS_WILDCARD = "wildcard"
STATUS_ABSENT = "absent"
STATUS_UNKNOWN = "unknown"


@dataclasses.dataclass(frozen=True)
class Availability:
    """Whether a slug is reachable on the gateway, and under what route name.

    ``status`` is one of :data:`STATUS_EXACT` (a catalog entry names this model),
    :data:`STATUS_WILDCARD` (a passthrough covers it), :data:`STATUS_ABSENT` (the
    catalog was read and does not cover it), or :data:`STATUS_UNKNOWN` (no gateway
    configured, the catalog could not be read, or it serves the model under several
    names and the deployment ranks none of them - a run would refuse that too).
    """

    slug: str
    status: str
    route: str | None = None

    @property
    def reachable(self) -> bool:
        """True when the gateway can plausibly serve this model."""
        return self.status in (STATUS_EXACT, STATUS_WILDCARD)


#: The user simulator the wire probes run, from ``integrate-model.yml``. On the gateway route that
#: workflow's ``.env`` is JOB-WIDE, so the simulator is proxied too and the gateway must serve it
#: as well - otherwise observe goes infra-dirty in the simulator rather than in the candidate.
#: Pinned against the workflow by a test, since a rename there would silently void that check.
USER_SIMULATOR_SLUG = "anthropic/claude-sonnet-4.6"


def _warn(message: str) -> None:
    """Surface an operator error without failing the poll.

    A misconfigured gateway is not a model problem, but the reply a requester sees says
    the route could not be confirmed, which points at the model. The annotation is what
    connects the two.
    """
    print(f"[gateway_catalog] {message}", file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::warning title=Gateway configuration unusable::{message}")


def fetch_configured_catalog(timeout: int = 15) -> list[str] | None:
    """The gateway catalog for THIS deployment, or ``None`` when there is no readable gateway.

    The single owner of the environment read, so every caller sees the same catalog the poll
    does; a hand-run diagnostic that disagreed with the poll would be worse than no diagnostic.

    ``None`` is "no information", never an error to propagate: a notification path must not
    fail a poll because a gateway is down or misconfigured.
    """
    # Env-only: dotenv precedence would let a developer's local .env answer a real poll.
    try:
        proxy = resolve_proxy_config(SecretManager([EnvProvider()]))
    except ProxyConfigError as exc:
        _warn(f"gateway unusable, availability is unknown: {exc}")
        return None
    if proxy is None:
        return None
    served = fetch_gateway_catalog(proxy, timeout)
    # Sorted so the same gateway yields the same resolution and the same log twice over.
    return None if served is None else sorted(served)


@dataclasses.dataclass(frozen=True)
class GatewayPolicy:
    """The routing policy the engine's resolver takes, as this deployment sets it.

    Defaults are the ENGINE's defaults, so a caller that passes nothing gets the
    strictest reading rather than a more permissive one - the direction a poller
    has to err in, since it promises a route a run then has to honour.
    """

    preferred_route: str | None = None
    trust_namespace_wildcards: bool = False


def configured_policy() -> GatewayPolicy:
    """This deployment's routing policy, or the engine defaults when there is no gateway.

    Env-only for the same reason as :func:`fetch_configured_catalog`: a developer's
    local ``.env`` must not decide what a real poll reports.
    """
    try:
        proxy = resolve_proxy_config(SecretManager([EnvProvider()]))
    except ProxyConfigError:
        # Already annotated by the catalog read on the same poll; warning twice for one
        # misconfiguration reads as two problems.
        return GatewayPolicy()
    if proxy is None:
        return GatewayPolicy()
    return GatewayPolicy(
        preferred_route=proxy.preferred_route,
        trust_namespace_wildcards=proxy.trust_namespace_wildcards,
    )


def lookup(
    slug: str, catalog: list[str] | None, policy: GatewayPolicy | None = None
) -> Availability:
    """Classify how (or whether) ``slug`` is reachable on the gateway.

    Asks the engine's own resolver rather than restating its rules, because the verdict
    reported here is acted on by a run: a second implementation of "which name reaches
    the gateway" drifted from the first once already (a vendor wildcard was reported as
    covering a model the run addresses as ``openrouter/<slug>``, which the engine refuses
    - that wildcard is a different upstream). Delegation makes the two agree by
    construction instead of by review.

    ``policy`` is what the deployment trusts; omitted, the engine's own default, which
    trusts no wildcard at all.
    """
    if catalog is None:
        return Availability(slug=slug, status=STATUS_UNKNOWN)
    policy = policy or GatewayPolicy()
    try:
        route = resolve_gateway_route(
            f"{RUN_PROVIDER}/{slug}",
            frozenset(catalog),
            policy.preferred_route,
            trust_namespace_wildcards=policy.trust_namespace_wildcards,
        )
    except GatewayRouteError as exc:
        # A run raises this rather than guessing, so "cannot confirm" is the honest
        # verdict: the plan keeps the OpenRouter default instead of queueing a run
        # that dies on the first client construction.
        _warn(f"gateway route for {slug!r} is ambiguous, availability is unknown: {exc}")
        return Availability(slug=slug, status=STATUS_UNKNOWN)
    if route is None:
        return Availability(slug=slug, status=STATUS_ABSENT)
    status = STATUS_EXACT if route.kind == "exact" else STATUS_WILDCARD
    return Availability(slug=slug, status=status, route=str(route))


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
