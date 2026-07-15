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

from automation import model_resolver, slack

# Opaque internal shortnames whose OpenRouter slug shares NO substring with the phrase go here
# (lowercased phrase -> exact slug). Intentionally empty: almost everything resolves by token
# match against the live catalog - even "hy3" -> ``tencent/hy3`` - so an alias is only needed
# for a genuinely opaque codename, added here when one appears.
ALIASES: dict[str, str] = {}

# The charset a real OpenRouter slug ever uses. A resolved slug is always a live catalog id so it
# passes, but the catalog is an EXTERNAL untrusted source and slugs flow into shell (branch name,
# PR title/body, `gh workflow run -f model=`); a slug with a shell metacharacter is dropped, never
# interpolated. Defence-in-depth behind the resolver's own "only ever returns a catalog entry".
_SAFE_SLUG_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


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
    clarify-with-the-base-slug reply BEFORE it is confirmed, so the requester re-requests the base."""
    if resolution.status != "resolved" or not resolution.slug or _SAFE_SLUG_RE.match(resolution.slug):
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
        resolutions = model_resolver.resolve_all(message.get("text", ""), catalog, ALIASES)
        if not resolutions:  # mention + "integrate" but no parseable model phrase
            continue
        # Charset-guard BEFORE the reply: a resolved-but-unsafe slug (a ':variant') must become a
        # clarify reply here, not be confirmed and then dropped by resolved_slugs() below.
        resolutions = [demote_unsafe_slug(r) for r in resolutions]
        reply = model_resolver.format_resolution_reply(requester, resolutions)
        if not slack._post_message(channel, reply, token, thread_ts=ts):
            # No durable processed-marker landed => do NOT add to the plan; retry next poll.
            # (Dispatching without a marker would double-run on the following poll.)
            slack._log(f"reply post failed for ts={ts}; not dispatching (will retry next poll)")
            slack._note_failure("Slack reply failed; integration not dispatched (will retry)")
            continue
        for slug in resolved_slugs(resolutions):
            plan.append({"slug": slug, "requester": requester, "message_ts": ts})

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
