"""Slack-triggered integration poller.

Scans the automation channel for ``@bot integrate <models>`` requests, resolves each
model phrase to an OpenRouter slug (deterministic, :mod:`automation.model_resolver` - no
LLM guessing), posts ONE threaded reply per request saying what started and what needs an
exact slug, and writes a plan.json of the slugs to integrate. The workflow reads that plan
and dispatches the integration engine once per slug.

Runs as ``github-actions[bot]`` (no PAT / no GitHub App): everything Slack goes through the
existing bot token; the GitHub side is a plain ``gh workflow run`` done by the workflow. The
bot's own user-id comes from ``auth.test`` (never hardcoded), so mention-detection and dedup
work if the bot is swapped.

Dedup is STATE-FREE: a request whose thread already has a bot reply is skipped - the reply IS
the processed-marker, so re-polling every few minutes never double-acts and no watermark has to
be stored (a repo variable would need admin the bot token lacks). A single-flight concurrency
group on the workflow closes the within-interval race. A slug is added to the plan only AFTER
its reply posts, so a Slack failure retries next poll instead of dispatching with no marker.

A missing token / channel / scope degrades to a logged no-op with an empty plan (a notification
path must never break the pipeline), but an unexpected exception - a bug - fails the run loudly
with a GH warning annotation. (Marker-based dedup has one inherent property: deleting the bot's
reply lets a request reprocess - acceptable, and it needs Slack delete perms.)
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re
import time

import typer

from automation import gateway_catalog, icons, model_resolver, slack

# Opaque internal shortnames whose OpenRouter slug shares NO substring with the phrase go here
# (lowercased phrase -> exact slug). Intentionally empty: almost everything resolves by token
# match against the live catalog - even "hy3" -> ``tencent/hy3`` - so an alias is only needed
# for a genuinely opaque codename, added here when one appears.
ALIASES: dict[str, str] = {}

# The charset a model slug may use, shared with the resolver so there is one rule rather than two
# copies to drift. A resolved slug is always a live catalog id so it passes, but a catalog is an
# EXTERNAL untrusted source and slugs flow into shell (branch name, PR title/body, `gh workflow
# run -f model=`); a slug with a shell metacharacter is dropped, never interpolated.
# Defence-in-depth behind the resolver's own "only ever returns a catalog entry".
_SAFE_SLUG_RE = model_resolver.SAFE_SLUG_RE

# Sentinel for "the gateway catalog has not been fetched yet". Distinct from None, which is a
# *fetched* answer meaning "no gateway configured or unreachable" — without it, an unreachable
# gateway would be re-fetched (and re-timed-out) once per request in the poll.
_UNFETCHED: list[str] = []


# --- pure helpers (unit-tested) ------------------------------------------------


def mentions_bot(text: str, bot_id: str) -> bool:
    """True if the message @-mentions the bot. Slack encodes a mention as ``<@U…>`` or, when a
    display name is cached, ``<@U…|label>`` - match the id with either closer."""
    return re.search(rf"<@{re.escape(bot_id)}(?:\|[^>]*)?>", text) is not None


def is_request(message: dict, bot_id: str) -> bool:
    """A real integrate request: a plain user message (no subtype, not a bot post, not the bot's
    own) that @-mentions the bot AND carries an ``integrate`` command with >=1 model phrase."""
    if message.get("subtype") or message.get("bot_id"):
        return False
    if message.get("user") == bot_id:
        return False
    text = message.get("text", "")
    return mentions_bot(text, bot_id) and bool(model_resolver.parse_command(text))


def parse_allowed(raw: str | None) -> frozenset[str]:
    """Comma-separated Slack user-ids -> a set. Tolerates ``U123``, ``@U123`` and ``<@U123>``."""
    ids = set()
    for token in (raw or "").split(","):
        cleaned = token.strip().strip("<>").lstrip("@").strip()
        if cleaned:
            ids.add(cleaned)
    return frozenset(ids)


def is_allowed(user_id: str, allowed: frozenset[str]) -> bool:
    """Empty allowlist => anyone in the channel may trigger (channel membership is the gate);
    otherwise the requester's Slack user-id must be listed."""
    return not allowed or user_id in allowed


def bot_replied(replies: list[dict], bot_id: str, parent_ts: str) -> bool:
    """State-free dedup: has the bot already replied in this thread? The parent message is
    ignored (it carries the same ts and, for a mention, is authored by the requester not the bot,
    but guard anyway)."""
    return any(m.get("user") == bot_id and m.get("ts") != parent_ts for m in replies)


def history_oldest(now: float, window_hours: float) -> str:
    """Slack ``oldest`` bound (Unix-ts string): only scan messages newer than ``window_hours``
    ago, so a re-poll never re-reads (or acts on) stale requests and the scan stays cheap. A
    non-positive window returns "" = no bound (fall back to the 200-message cap)."""
    if window_hours <= 0:
        return ""
    return f"{now - window_hours * 3600:.6f}"


@dataclasses.dataclass(frozen=True)
class RoutePlan:
    """Which route each resolved model runs over, plus what to tell the requester.

    ``requested_route`` is the directive AFTER any downgrade, so the reply reports the route
    that will actually be used rather than the one that was asked for.
    """

    routes: dict[str, str]
    warnings: tuple[str, ...]
    requested_route: str | None
    #: True when a stated route could not be honoured. Both that and "nothing was asked" leave
    #: ``requested_route`` as ``None``, and the reply has to tell them apart: only the second one
    #: should go on to offer the gateway as an option.
    downgraded: bool = False


def route_plan(
    resolutions: list[model_resolver.Resolution],
    availability: dict[str, gateway_catalog.Availability],
    requested_route: str | None,
    overrides: dict[str, str] | None = None,
    simulator: gateway_catalog.Availability | None = None,
) -> RoutePlan:
    """Decide the route PER MODEL, and the warnings that make a non-honoured directive visible.

    Per model rather than per message because the two sides of a request can disagree: a
    gateway-only model can run nowhere but the gateway, while everything else must keep the
    calibrated OpenRouter default (changing a model's serving path silently is a
    leaderboard-comparability decision, not a transport detail).

    Two directives cannot be honoured, and each says so rather than being dropped:

    * ``via litellm`` when the gateway is not confirmed to serve every downgradable model - the
      run would fail against a gateway we already know lacks it, and the outcome would read as a
      model failure. Only models OpenRouter CARRIES are considered here; a gateway-only model is
      not evidence against the gateway.
    * ``via openrouter`` for a model OpenRouter does not carry at all.

    ``simulator`` is the gateway availability of the wire probes' user simulator. It belongs in
    the same evidence, because the integration run's gateway ``.env`` is JOB-WIDE: the simulator
    is proxied too, so a gateway that does not serve it sends observe infra-dirty in the
    SIMULATOR rather than in the candidate. It can veto an explicit ``via litellm`` (that route
    has a working alternative), but it cannot veto a gateway-only model, which has none - there
    the requester is told what to fix before the run burns.
    """
    if overrides is None:
        overrides = icons.load_icon_overrides()
    warning_icon = icons.icon("route_downgraded", overrides)
    warnings: list[str] = []
    downgraded = False
    gateway_only = [r for r in resolutions if r.source == model_resolver.SOURCE_GATEWAY and r.slug]
    downgradable = [
        r
        for r in resolutions
        if r.slug in availability and r.source != model_resolver.SOURCE_GATEWAY
    ]
    models_unconfirmed = not all(availability[r.slug].reachable for r in downgradable)
    simulator_unconfirmed = simulator is not None and not simulator.reachable
    # Named in a message only when the catalog was READ and does not cover it. With no readable
    # gateway there is nothing specific to say about one model, and pointing at the simulator
    # would send someone checking a name that is fine.
    simulator_absent = simulator is not None and simulator.status == gateway_catalog.STATUS_ABSENT
    if requested_route == gateway_catalog.ROUTE_GATEWAY and (
        models_unconfirmed or simulator_unconfirmed
    ):
        requested_route = None
        downgraded = True
        if models_unconfirmed or not simulator_absent:
            warnings.append(
                f"{warning_icon} I could not confirm the gateway serves every model above "
                f"(see the notes), so the models OpenRouter carries run over "
                f"*{gateway_catalog.DEFAULT_ROUTE}*. Add the model to the gateway (or check its "
                "secrets) and re-request."
            )
    if requested_route == gateway_catalog.ROUTE_OPENROUTER and gateway_only:
        names = ", ".join(f"`{r.slug}`" for r in gateway_only)
        warnings.append(
            f"{warning_icon} {names} is not on OpenRouter, so `via openrouter` cannot be "
            f"honoured for it; it runs over *{gateway_catalog.ROUTE_GATEWAY}*."
        )
    routes = {
        r.slug: model_resolver.route_for(r, requested_route)
        for r in resolutions
        if r.status == "resolved" and r.slug
    }
    gateway_pinned = gateway_catalog.ROUTE_GATEWAY in routes.values()
    if simulator_absent and (downgraded or gateway_pinned):
        # ONE warning for one fact with two consequences: a model OpenRouter carries falls back,
        # a gateway-only model cannot. Two warnings here read as the bot repeating itself, and a
        # gateway-only model needs saying either way - the run would report a user-simulator
        # failure that looks like nothing to do with the gateway.
        consequences = []
        if downgraded:
            consequences.append(
                f"the models OpenRouter carries run over *{gateway_catalog.DEFAULT_ROUTE}*"
            )
        if gateway_pinned:
            consequences.append(
                "a model only the gateway carries still runs there, so observe may go "
                "infra-dirty in the simulator rather than in the candidate"
            )
        # Two consequences point in opposite directions - one model falls back, another cannot -
        # so they go on their own lines rather than into one sentence.
        detail = (
            f" {consequences[0]}."
            if len(consequences) == 1
            else "\n" + "\n".join(f"    • {clause}" for clause in consequences)
        )
        warnings.append(
            f"{warning_icon} I could not confirm the gateway serves the wire probes' user "
            f"simulator (`{gateway_catalog.USER_SIMULATOR_SLUG}`), which the integration run "
            f"proxies too:{detail}\nAdd it to the gateway (or check its secrets)."
        )
    return RoutePlan(
        routes=routes,
        warnings=tuple(warnings),
        requested_route=requested_route,
        downgraded=downgraded,
    )


def resolved_slugs(resolutions: list[model_resolver.Resolution]) -> list[str]:
    """The slugs to integrate (status == resolved), in request order, de-duplicated. A slug whose
    charset is not a plain OpenRouter id is dropped (see ``_SAFE_SLUG_RE``) - it must never reach
    the shell."""
    seen: set[str] = set()
    out: list[str] = []
    for resolution in resolutions:
        slug = resolution.slug
        if resolution.status != "resolved" or not slug or slug in seen:
            continue
        if not _SAFE_SLUG_RE.match(slug):
            slack._log(f"dropping resolved slug with unexpected charset: {slug!r}")
            continue
        seen.add(slug)
        out.append(slug)
    return out


def demote_unsafe_slug(resolution: model_resolver.Resolution) -> model_resolver.Resolution:
    """Guard the reply against confirming a slug the shell can never receive. A slug that resolved
    but is not a plain OpenRouter id (a ':free' / ':nitro' variant - ``_SAFE_SLUG_RE`` rejects the
    ':') would be confirmed to the requester and then silently dropped by ``resolved_slugs``,
    leaving the request un-run AND un-retryable (the reply is the dedup marker). Demote it to a
    clarify-with-the-base-slug reply BEFORE it is confirmed, so the requester re-requests the base.
    """
    if (
        resolution.status != "resolved"
        or not resolution.slug
        or _SAFE_SLUG_RE.match(resolution.slug)
    ):
        return resolution
    base = resolution.slug.split(":", 1)[0]
    return dataclasses.replace(resolution, status="ambiguous", slug=None, candidates=(base,))


# --- Slack transport not covered by slack.py (never raises out) ----------------


def _auth_test(token: str) -> str | None:
    """The bot's own user-id, so mention-detection + dedup need no hardcoded id."""
    result = slack._post("auth.test", token, {})
    return result.get("user_id") if result else None


def _replies(channel: str, ts: str, token: str) -> list[dict]:
    """Thread replies for a parent ts (empty on any failure)."""
    result = slack._get(
        "conversations.replies", token, {"channel": channel, "ts": ts, "limit": "50"}
    )
    return result.get("messages", []) if result else []


def _already_handled(channel: str, message: dict, ts: str, bot_id: str, token: str) -> bool:
    """Has this request been handled? Cheap gate first: a parent with no replies cannot have a
    bot reply, so skip the extra API call for the common brand-new request. Only when replies
    exist (and the bot is not already listed in ``reply_users``) do we fetch to confirm."""
    if not message.get("reply_count"):
        return False
    if bot_id in message.get("reply_users", []):
        return True
    return bot_replied(_replies(channel, ts, token), bot_id, ts)


def _write_plan(out_path: str, plan: list[dict]) -> None:
    pathlib.Path(out_path).write_text(json.dumps(plan, indent=2) + "\n")


# --- orchestration -------------------------------------------------------------


def run(channel: str, allowed_users: str | None, out_path: str, window_hours: float = 48.0) -> int:
    """Scan the channel (last ``window_hours`` of history), resolve each ``@bot integrate``
    request, reply in-thread, and write the integration plan to ``out_path``. Always returns 0
    (a poll must never fail the workflow)."""
    plan: list[dict] = []
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token or not channel:
        slack._log("no SLACK_BOT_TOKEN or channel - dry-run no-op")
        _write_plan(out_path, plan)
        return 0
    bot_id = _auth_test(token)
    if not bot_id:
        slack._log("auth.test failed - cannot identify the bot; no-op")
        slack._note_failure("Slack auth.test failed (poller cannot identify the bot)")
        _write_plan(out_path, plan)
        return 0
    oldest = history_oldest(time.time(), window_hours)
    messages = slack._history(channel, token, oldest=oldest or None)
    if messages is None:
        slack._log("history unavailable (missing channels:history / not in channel?)")
        slack._note_failure("Slack history unavailable (poller could not read the channel)")
        _write_plan(out_path, plan)
        return 0

    allowed = parse_allowed(allowed_users)
    catalog: list[str] | None = None
    # Distinct from None: None is a *fetched* answer meaning "no gateway info".
    gateway_models: list[str] | None = _UNFETCHED
    for message in messages:
        if not is_request(message, bot_id):
            continue
        ts = message.get("ts", "")
        requester = message.get("user", "")
        if _already_handled(channel, message, ts, bot_id, token):
            continue
        if not is_allowed(requester, allowed):
            # One threaded refusal (also the processed-marker, so it is not repeated every poll).
            slack._post_message(
                channel,
                f"<@{requester}> sorry, you are not on the integration allowlist for this "
                "channel, so I will not start an integration. Ask a maintainer to add you.",
                token,
                thread_ts=ts,
            )
            continue
        if catalog is None:  # fetch once, lazily - only when there is real work to resolve
            catalog = model_resolver.fetch_openrouter_catalog()
        if gateway_models is _UNFETCHED:
            # Fetched BEFORE resolution, not after: resolution falls back to this catalog for a
            # phrase OpenRouter cannot match, which is the only way a gateway-only model
            # (`azure_ai/...`) can be requested at all.
            gateway_models = gateway_catalog.fetch_configured_catalog()
        text = message.get("text", "")
        resolutions = model_resolver.resolve_all(text, catalog, ALIASES, gateway_models)
        if not resolutions:  # mention + "integrate" but no parseable model phrase
            continue
        # Charset-guard BEFORE the reply: a resolved-but-unsafe slug (a ':variant') must become a
        # clarify reply here, not be confirmed and then dropped by resolved_slugs() below.
        resolutions = [demote_unsafe_slug(r) for r in resolutions]
        requested_route = model_resolver.parse_route(text)
        # Gateway lookup for the reply: advisory for an OpenRouter-sourced model, and simply
        # confirmation for a gateway-sourced one (it came out of this catalog).
        availability = {
            r.slug: gateway_catalog.lookup(r.slug, gateway_models)
            for r in resolutions
            if r.status == "resolved" and r.slug
        }
        # Never promise a route the deployment cannot serve. With no readable catalog the run
        # would fall back to OpenRouter anyway (integrate-model.yml), and with the model absent
        # every probe would fail against a gateway we already know does not serve it -- either
        # way a reply saying "via litellm" would misrecord the serving path. Refuse in the
        # reply instead of dispatching a run whose outcome would read as a model failure.
        plan_routes = route_plan(
            resolutions,
            availability,
            requested_route,
            simulator=gateway_catalog.lookup(gateway_catalog.USER_SIMULATOR_SLUG, gateway_models),
        )
        reply = model_resolver.format_resolution_reply(
            requester,
            resolutions,
            availability,
            plan_routes.requested_route,
            gateway_searched=gateway_models is not None,
            route_downgraded=plan_routes.downgraded,
        )
        if plan_routes.warnings:
            reply = "\n\n".join([reply, *plan_routes.warnings])
        if not slack._post_message(channel, reply, token, thread_ts=ts):
            # No durable processed-marker landed => do NOT add to the plan; retry next poll.
            # (Dispatching without a marker would double-run on the following poll.)
            slack._log(f"reply post failed for ts={ts}; not dispatching (will retry next poll)")
            slack._note_failure("Slack reply failed; integration not dispatched (will retry)")
            continue
        for slug in resolved_slugs(resolutions):
            plan.append(
                {
                    "slug": slug,
                    "requester": requester,
                    "message_ts": ts,
                    "route": plan_routes.routes.get(slug, gateway_catalog.DEFAULT_ROUTE),
                    "gateway": (
                        gateway_catalog.as_dict(availability[slug])
                        if slug in availability
                        else None
                    ),
                }
            )

    _write_plan(out_path, plan)
    slack._log(f"poll complete: {len(plan)} model(s) queued for integration")
    return 0


# --- typer command (registered on the root app in cli.py) ----------------------


def cli(
    channel: str = typer.Option(..., "--channel", help="the automation channel id"),
    out: str = typer.Option("plan.json", "--out", help="path to write the integration plan JSON"),
    allowed_users: str | None = typer.Option(
        None,
        "--allowed-users",
        help="comma-separated Slack user-ids allowed to trigger; empty = anyone in the channel",
    ),
    window_hours: float = typer.Option(
        48.0, "--window-hours", help="only scan Slack messages from the last N hours (0 = no bound)"
    ),
) -> None:
    """Scan the channel for @bot integrate requests, reply with the resolution, emit a plan."""
    try:
        code = run(channel, allowed_users, out, window_hours=window_hours)
    except Exception as exc:
        # Slack-transport degradations are handled (exit 0) inside run(); anything reaching
        # here is a bug and must fail the run loudly - a green schedule that silently drops
        # every request is worse than a red one. Still write an empty plan so a downstream
        # step reading plan.json fails on "0 requests", not on a missing file.
        slack._note_failure(f"poller crashed: {exc!r}")
        try:
            _write_plan(out, [])
        except Exception:
            pass
        raise
    raise typer.Exit(code)
