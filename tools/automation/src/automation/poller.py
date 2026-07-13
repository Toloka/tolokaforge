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

Never raises: a missing token / channel / scope degrades to a logged no-op with an empty plan,
so the poll step never fails the workflow (a notification path must never break the pipeline).
Only stdlib + the sibling modules are used.
"""

from __future__ import annotations

import json
import os
import pathlib
import re

import typer

from automation import model_resolver, slack

# Opaque internal shortnames whose OpenRouter slug shares NO substring with the phrase go here
# (lowercased phrase -> exact slug). Intentionally empty: almost everything resolves by token
# match against the live catalog - even "hy3" -> ``tencent/hy3`` - so an alias is only needed
# for a genuinely opaque codename, added here when one appears.
ALIASES: dict[str, str] = {}


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


def resolved_slugs(resolutions: list[model_resolver.Resolution]) -> list[str]:
    """The slugs to integrate (status == resolved), in request order, de-duplicated."""
    seen: set[str] = set()
    out: list[str] = []
    for resolution in resolutions:
        if resolution.status == "resolved" and resolution.slug and resolution.slug not in seen:
            seen.add(resolution.slug)
            out.append(resolution.slug)
    return out


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


def run(channel: str, allowed_users: str | None, out_path: str) -> int:
    """Scan the channel, resolve each ``@bot integrate`` request, reply in-thread, and write the
    integration plan to ``out_path``. Always returns 0 (a poll must never fail the workflow)."""
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
    messages = slack._history(channel, token)
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
) -> None:
    """Scan the channel for @bot integrate requests, reply with the resolution, emit a plan."""
    try:
        code = run(channel, allowed_users, out)
    except Exception as exc:  # a poll must never fail the workflow
        slack._log(f"unexpected error (ignored): {exc}")
        _write_plan(out, [])
        code = 0
    raise typer.Exit(code)
