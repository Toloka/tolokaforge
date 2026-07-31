"""Deterministic model-slug resolution for the Slack-triggered integration flow.

A Slack request is free text ("integrate Grok 4.5 and GPT 5.6"); the RESOLUTION to an
OpenRouter slug is deterministic - no LLM guessing. :func:`parse_command` pulls the model
phrases out of the message; :func:`resolve` matches each phrase against the live OpenRouter
catalog with strict VERSION discipline: "Grok 4.5" resolves to ``x-ai/grok-4.5`` and never
to ``grok-4`` or ``grok-4.3``. An under-specified phrase like "GPT 5.6" that maps to several
slugs comes back AMBIGUOUS so the caller can ask the requester to pick an exact slug.

Matching rule (per phrase, against each catalog slug ``<provider>/<model>``):
  * a curated ALIAS ("hy3" -> a slug) or an exact slug the requester typed resolves immediately;
  * otherwise every ALPHA token of the phrase must be present in the slug's tokens
    (provider+model), and every VERSION digit-key must match. Tokens split at alpha/digit
    boundaries ("GPT5.6" -> "gpt","5.6"); a version's digit-key is its digits with dots dropped,
    so the dotless config style ("v32", "glm 51") matches the dotted slug ("v3.2", "glm-5.1")
    while distinct versions still differ ("4.5"->45 != "4" != "4.3"->43 != "4.53"->453).
Then 0 matches -> none; 1 -> resolved; >1 -> if exactly one candidate's MODEL-part alpha
tokens equal the phrase's alpha tokens, resolve to it (so "gpt 5.6 sol" picks ``gpt-5.6-sol``
over ``gpt-5.6-sol-pro``); otherwise AMBIGUOUS with every candidate listed.

Which catalogs are searched
---------------------------

OpenRouter first; the deployment's LLM-gateway catalog is a FALLBACK, consulted only for a
phrase OpenRouter cannot match at all. Fallback rather than a union, deliberately: every
phrase that resolves (or is ambiguous) against OpenRouter today behaves identically, so the
calibrated default route never moves and a gateway listing the same model under a second
route name cannot turn a working request into a clarify reply. The fallback is the only way
a gateway-ONLY model can be requested at all - before it existed, ``azure_ai/cohere-...``
came back as "no matching model on OpenRouter" even though the gateway served it.

Only the network fetch (:func:`fetch_openrouter_catalog`) does I/O; everything the tests
care about is pure and catalog-injected.
"""

from __future__ import annotations

import dataclasses
import json
import re
import urllib.request

from automation import gateway_catalog, icons

_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
# Maximal alpha runs (keeping a trailing "+", so "a+" != "a" and Cohere "A+" never collapses
# onto "command-a") OR dotted-number runs; splits glued names ("gpt5.6" -> gpt, 5.6) and
# ignores every other separator (space / _ / - / /).
_TOKEN_RE = re.compile(r"[a-z]+\+?|\d+(?:\.\d+)*")
# Request connectors, for "integrate a and b". The WORD connectors require
# surrounding whitespace; `\band\b` / `\bplus\b` alone would fire inside a slug,
# because a hyphen is a regex word boundary. That tore
# `azure_ai/cohere-command-a-plus-05-2026` into two halves and, worse, silently
# reduced `cohere/command-a-plus` to `cohere/command-a-` - a DIFFERENT model name
# that could go on to match something real.
_CONNECTOR_RE = re.compile(r"\s*(?:,|&)\s*|\s+(?:and|plus)\s+", re.IGNORECASE)
_MENTION_RE = re.compile(r"<@[^>]+>")
_INTEGRATE_RE = re.compile(r"\bintegrate\b", re.IGNORECASE)
# Optional route directive: "integrate Grok 4.5 via litellm". Stripped BEFORE the phrases are
# split, otherwise "Grok 4.5 via litellm" becomes the model phrase and resolves to nothing.
# Accepts the gateway's product name and the generic words for it, since a requester types
# whichever they think in.
_ROUTE_RE = re.compile(
    r"\b(?:via|through|over|using)\s+(?:the\s+)?"
    r"(?P<route>litellm(?:[\s-]+(?:proxy|gateway))?"
    r"|(?:litellm[\s-]+)?(?:gateway|proxy)"
    r"|openrouter|or)\b",
    re.IGNORECASE,
)
# Any of these words in the matched phrase means the gateway. Word-based rather than an
# exact-string table so widening the regex cannot silently stop mapping a form it matches.
_GATEWAY_WORDS = frozenset({"litellm", "gateway", "proxy"})
_OPENROUTER_WORDS = frozenset({"openrouter", "or"})


#: Which catalog held a resolved slug. This decides the ROUTE: a model OpenRouter carries
#: keeps the calibrated OpenRouter path, while one that exists only on the gateway can run
#: nowhere else, so it pins its own route (:func:`route_for`).
SOURCE_OPENROUTER = "openrouter"
SOURCE_GATEWAY = "gateway"


@dataclasses.dataclass(frozen=True)
class _Tokens:
    alpha: frozenset[str]
    version: frozenset[str]


@dataclasses.dataclass(frozen=True)
class Resolution:
    """Outcome for one requested phrase. ``status`` is resolved / ambiguous / none."""

    query: str
    status: str
    slug: str | None = None
    candidates: tuple[str, ...] = ()
    #: Which catalog the slug came from (:data:`SOURCE_OPENROUTER` / :data:`SOURCE_GATEWAY`).
    #: Defaults to OpenRouter, which is what every resolution meant before the gateway
    #: fallback existed.
    source: str = SOURCE_OPENROUTER


def _tokenize(text: str) -> _Tokens:
    """Alpha tokens plus VERSION digit-keys. A version's key is its digits with dots dropped,
    so the dotless config style ("v32", "glm 51") matches the dotted slug ("v3.2", "glm-5.1")
    while distinct versions still differ ("4.5"->45 != "4" != "4.3"->43)."""
    alpha, version = set(), set()
    for tok in _TOKEN_RE.findall(text.lower()):
        if tok[0].isdigit():
            version.add(tok.replace(".", ""))
        else:
            alpha.add(tok)
    return _Tokens(frozenset(alpha), frozenset(version))


def _slug_tokens(slug: str) -> _Tokens:
    """All tokens of a slug (provider + model), for containment matching."""
    return _tokenize(slug.replace("/", " "))


def _slug_model_alpha(slug: str) -> frozenset[str]:
    """Alpha tokens of the model part only (provider dropped), for the exact-match tie-break."""
    return _tokenize(slug.split("/", 1)[-1]).alpha


def parse_route(text: str) -> str | None:
    """The explicitly requested integration route, or ``None`` for "not stated".

    ``None`` deliberately differs from :data:`gateway_catalog.DEFAULT_ROUTE`: the
    caller needs to know whether a human chose OpenRouter or merely did not ask,
    because only the first justifies overriding a gateway-only model. Several
    directives that DISAGREE in one message also return ``None`` ("not stated"):
    one message-level route cannot honour both, so the default applies.
    """
    routes: set[str] = set()
    for match in _ROUTE_RE.finditer(_MENTION_RE.sub(" ", text)):
        words = set(match.group("route").lower().replace("-", " ").split())
        if words & _GATEWAY_WORDS:
            routes.add(gateway_catalog.ROUTE_GATEWAY)
        elif words & _OPENROUTER_WORDS:
            routes.add(gateway_catalog.ROUTE_OPENROUTER)
    if len(routes) != 1:
        # Nothing asked, or several directives disagreeing in one message. One message-level
        # route cannot honour both, and guessing could move a model whose route was stated as
        # (or left at) OpenRouter onto the gateway -- so treat it as "not stated" and let the
        # calibrated default apply.
        return None
    return next(iter(routes))


def parse_command(text: str) -> list[str]:
    """Extract the model phrases from a free-text integrate request. Drops any bot mention
    and any ``via <route>`` directive, keeps everything after the first ``integrate`` keyword,
    and splits on and / & / , / plus.
    Returns ``[]`` when the message is not an integrate request (no keyword)."""
    cleaned = _ROUTE_RE.sub(" ", _MENTION_RE.sub(" ", text))
    match = _INTEGRATE_RE.search(cleaned)
    if not match:
        return []
    tail = cleaned[match.end() :]
    return [phrase for chunk in _CONNECTOR_RE.split(tail) if (phrase := chunk.strip(" .\t\r\n"))]


def _gateway_candidates(entries: list[str] | None, openrouter_slugs: set[str]) -> list[str]:
    """The gateway route ids that are model names in their own right.

    A gateway catalog is a routing table, not a model list, so most of it must not become a
    resolution candidate:

    * a wildcard (``x-ai/*``, ``*``) is a passthrough, not a model name;
    * an id the OpenRouter catalog already carries adds no new candidate - the fallback only
      runs when OpenRouter matched nothing, so such an entry cannot match either;
    * an id that is an OpenRouter slug under a route prefix (``openrouter/x-ai/grok-4.5``) is
      the SAME model under a second name, and :func:`gateway_catalog.lookup` documents that
      prefixed route as one this flow cannot reach anyway.

    What survives is exactly what OpenRouter does not carry - a gateway-only route such as
    ``azure_ai/cohere-command-a-plus-05-2026``.
    """
    if not entries:
        return []
    candidates = []
    for entry in entries:
        slug = entry.strip()
        if not slug or "*" in slug or slug in openrouter_slugs:
            continue
        if slug.split("/", 1)[-1] in openrouter_slugs:  # an OpenRouter slug under a route prefix
            continue
        candidates.append(slug)
    return candidates


def route_for(resolution: Resolution, requested_route: str | None = None) -> str:
    """The route a resolved phrase must run over.

    A gateway-only model pins the gateway even when nothing was requested: OpenRouter does not
    carry it, so the calibrated default is not an option and an explicit ``via openrouter``
    cannot be honoured (the caller says so in the reply). Everything else keeps the requested
    route, or the calibrated default.
    """
    if resolution.source == SOURCE_GATEWAY:
        return gateway_catalog.ROUTE_GATEWAY
    return requested_route or gateway_catalog.DEFAULT_ROUTE


def resolve(
    query: str,
    catalog: list[str],
    aliases: dict[str, str] | None = None,
    gateway_entries: list[str] | None = None,
) -> Resolution:
    """Deterministically resolve one phrase, OpenRouter first and the gateway as a fallback.

    ``catalog`` is the OpenRouter slug list; ``gateway_entries`` the deployment's gateway
    catalog (``None`` when there is no gateway or it could not be read). The gateway is only
    consulted for a phrase OpenRouter matched NOTHING for - see the module docstring for why
    this is a fallback and not a union. ``aliases`` (lowercased phrase -> slug) covers opaque
    internal shortnames like ``hy3`` that no string match can reach; it is consulted first.
    """
    primary = _resolve_against(query, catalog, aliases)
    gateway = _gateway_candidates(gateway_entries, set(catalog))
    if primary.status == "resolved":
        # Only reachable through an alias: the token path returns catalog members only. An alias
        # pointing at a gateway-only route must still be routed there.
        if primary.slug not in set(catalog) and primary.slug in set(gateway):
            return dataclasses.replace(primary, source=SOURCE_GATEWAY)
        return primary
    if primary.status != "none" or not gateway:
        return primary
    fallback = _resolve_against(query, gateway, None)
    if fallback.status == "none":
        return primary  # nothing anywhere; keep the primary answer
    return dataclasses.replace(fallback, source=SOURCE_GATEWAY)


def _resolve_against(
    query: str, catalog: list[str], aliases: dict[str, str] | None = None
) -> Resolution:
    """One phrase against ONE catalog. The matching rule in the module docstring, verbatim."""
    stripped = query.strip()
    if aliases and (alias_slug := aliases.get(stripped.lower())):
        return Resolution(query, "resolved", slug=alias_slug)
    # A requester who already typed an exact slug resolves straight away (bypasses the
    # provider-token vs model-alpha tie-break, which cannot know "openai" is the provider).
    for slug in catalog:
        if slug.lower() == stripped.lower():
            return Resolution(query, "resolved", slug=slug)
    q = _tokenize(query)
    if not q.alpha:  # a bare version ("4.5") is not a model name
        return Resolution(query, "none")
    matches = sorted(
        {
            slug
            for slug in catalog
            if q.alpha <= (st := _slug_tokens(slug)).alpha and q.version <= st.version
        }
    )
    if not matches:
        return Resolution(query, "none")
    if len(matches) == 1:
        return Resolution(query, "resolved", slug=matches[0])
    exact = [slug for slug in matches if _slug_model_alpha(slug) == q.alpha]
    if len(exact) == 1:
        return Resolution(query, "resolved", slug=exact[0])
    return Resolution(query, "ambiguous", candidates=tuple(matches))


def resolve_all(
    text: str,
    catalog: list[str],
    aliases: dict[str, str] | None = None,
    gateway_entries: list[str] | None = None,
) -> list[Resolution]:
    """Parse a command and resolve every phrase in it."""
    return [resolve(phrase, catalog, aliases, gateway_entries) for phrase in parse_command(text)]


def format_resolution_reply(
    requester_id: str,
    resolutions: list[Resolution],
    availability: dict[str, gateway_catalog.Availability] | None = None,
    requested_route: str | None = None,
    gateway_searched: bool = False,
    overrides: dict[str, str] | None = None,
) -> str:
    """Slack mrkdwn reply: what started, and which phrases need an exact slug. The requester
    is pinged at the top so the ambiguous re-request lands on the right person.

    When ``availability`` is supplied, each resolved model also reports whether the
    deployment's LLM gateway could serve it — advisory only, since a gateway route may be
    backed by a different upstream and that is a comparability call for a human.
    ``requested_route`` echoes an explicit ``via <route>`` directive so the requester can
    see it was honoured; absent, the default route is used and named. The route is named PER
    MODEL, because a gateway-only model pins its own (:func:`route_for`).

    Every icon here comes from the role registry, so a workspace override restyles the whole
    reply; ``overrides`` is injectable for tests and resolved once otherwise.

    ``gateway_searched`` says whether the gateway catalog was readable, so a failure can name
    what was actually consulted. Reporting "no matching model on OpenRouter" when a configured
    gateway had never been searched is what sent a requester chasing a name that was fine."""
    # Resolved ONCE: icons.icon() re-reads and re-parses the env JSON whenever it is handed
    # None, and this function emits one icon per requested model.
    if overrides is None:
        overrides = icons.load_icon_overrides()
    lines = [f"<@{requester_id}> here is what I could resolve from your request:", ""]
    searched = "OpenRouter or the gateway" if gateway_searched else "OpenRouter"
    for r in resolutions:
        if r.status == "resolved":
            lines.append(
                f"{icons.icon('request_resolved', overrides)} *{r.query}*: starting "
                f"integration as `{r.slug}` via *{route_for(r, requested_route)}*"
            )
            if r.source == SOURCE_GATEWAY:
                lines.append(
                    "    ◦ only on the gateway, not on OpenRouter, so it can run nowhere else"
                )
            # Suppressed for a gateway-sourced model: "ALSO on the gateway" reads as "in
            # addition to OpenRouter", which is exactly what is not true, and the line above
            # already said where it lives.
            note = (
                gateway_catalog.describe(availability[r.slug])
                if availability and r.slug in availability and r.source == SOURCE_OPENROUTER
                else ""
            )
            if note:
                lines.append(f"    ◦ {note}")
        elif r.status == "ambiguous":
            lines.append(
                f"{icons.icon('request_ambiguous', overrides)} *{r.query}* is ambiguous "
                f"({len(r.candidates)} matches). Re-request with one of these exact slugs:"
            )
            lines += [f"    • `{slug}`" for slug in r.candidates]
        else:
            unsearched = (
                ""
                if gateway_searched
                else " (no gateway catalog was readable, so only OpenRouter was searched)"
            )
            lines.append(
                f"{icons.icon('request_unresolved', overrides)} *{r.query}*: no matching "
                f"model on {searched}. Check the name and version.{unsearched}"
            )
    if requested_route is None and availability:
        openrouter_sourced = [r for r in resolutions if r.source == SOURCE_OPENROUTER and r.slug]
        if openrouter_sourced and any(
            availability[r.slug].reachable for r in openrouter_sourced if r.slug in availability
        ):
            lines += [
                "",
                f"_Default route is *{gateway_catalog.DEFAULT_ROUTE}*. To use the gateway "
                f"instead, re-request with `via litellm`._",
            ]
    return "\n".join(lines)


def as_dict(resolution: Resolution) -> dict:
    """JSON-friendly view (candidates as a list) for the CLI / poller."""
    return {
        "query": resolution.query,
        "status": resolution.status,
        "slug": resolution.slug,
        "candidates": list(resolution.candidates),
        "source": resolution.source,
    }


def fetch_openrouter_catalog(timeout: int = 15) -> list[str]:
    """Live list of OpenRouter model ids (slugs). Public endpoint, no auth needed."""
    request = urllib.request.Request(_OPENROUTER_MODELS_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    return sorted(model["id"] for model in data.get("data", []) if model.get("id"))
