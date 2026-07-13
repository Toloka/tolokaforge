#!/usr/bin/env python
"""Slack thread notifications for the model auto-integration pipeline.

One Slack thread per integration PR: a PR-unique root message
(``Auto-integration: <model> (PR #<N>)``) plus threaded replies for each
pipeline milestone. The root ts is NOT persisted GitHub-side; it is
rediscovered from Slack via ``conversations.history`` (bounded scan, client-side
match on the ``(PR #<N>)`` token), so the OBSERVE and RESOLVE stages (separate
workflow runs) thread together, and repeated rounds on the same PR reuse the same
thread. The PR number is the thread key, not the model - the same model in two
different PRs is two threads.

Bot-token + ``chat.postMessage`` only (never an incoming webhook: a webhook
returns no ts and can neither thread nor read history). The token is read from
``SLACK_BOT_TOKEN``; an absent token or channel makes every command a logged
no-op (a fork PR gets no secrets, a repo without the secret configured degrades
cleanly). Nothing here exits non-zero on a Slack failure - the caller guards with
``|| true`` and a notification must never fail the job. Only stdlib is used so the
workflow can run it with the system ``python3`` before ``uv sync``.

Subcommands:
  ensure-root --channel <id> --model <name> --pr <N>
      Find (or post) the thread root; print its ts to stdout. Idempotent: a
      re-trigger reuses the existing root instead of opening a new one.
  reply --channel <id> --pr <N> --text <msg> [--model <name>] [--mention]
      Reply into the PR's thread (creating the root first if missing). With
      ``--mention`` the configured ``SLACK_MENTIONS`` users are prefixed so they
      get pinged; used for the terminal / needs-human / error notifications.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

_API = "https://slack.com/api/"
_TIMEOUT = 15
_HISTORY_SCAN = 200  # bounded scan: newest N top-level messages


def _log(message: str) -> None:
    print(f"[slack_notify] {message}", file=sys.stderr)


# --- pure helpers (unit-tested) ------------------------------------------------


def build_root_text(model: str, pr: int, pr_url: str | None = None) -> str:
    """The PR-unique thread root. Always carries the word ``Auto-integration`` and a
    ``#<pr>`` token bounded by a non-digit - :func:`root_matches` keys on both. When the
    PR URL is known the number renders as a Slack hyperlink ``<url|#<pr>>`` so the PR is
    one click away and there is no bare URL line; otherwise a plain ``#<pr>``."""
    label = f"`{model}` " if model else ""
    pr_ref = f"<{pr_url}|#{pr}>" if pr_url else f"#{pr}"
    return f"Auto-integration: {label}(PR {pr_ref})"


def root_matches(text: str, pr: int) -> bool:
    """Does this message identify the root for ``pr``? Matches both the plain ``#<pr>``
    and the hyperlinked ``<url|#<pr>>`` root forms; the negative lookahead keeps ``#20``
    from matching ``#209`` (self-delimiting on a trailing non-digit)."""
    return "Auto-integration" in text and re.search(rf"#{pr}(?!\d)", text) is not None


def find_root_ts(messages: list[dict], pr: int) -> str | None:
    """Return the oldest matching root ts among ``messages`` (history is
    newest-first; oldest is picked so a rare duplicate root stays stable)."""
    matches = [m["ts"] for m in messages if m.get("ts") and root_matches(m.get("text", ""), pr)]
    return min(matches, key=float) if matches else None


def build_mention_prefix(raw: str | None) -> str:
    """Turn the comma-separated ``SLACK_MENTIONS`` value into a mrkdwn ping prefix.
    Empty / unset -> no mention. Tolerates ``U123``, ``@U123`` and ``<@U123>``."""
    if not raw:
        return ""
    ids = []
    for token in raw.split(","):
        cleaned = token.strip().strip("<>").lstrip("@").strip()
        if cleaned:
            ids.append(f"<@{cleaned}>")
    return " ".join(ids) + " " if ids else ""


def append_footer(text: str, pr_comment: str = "", pr_url: str = "", run_url: str = "") -> str:
    """Append a ' · '-separated footer of Slack ``<url|label>`` links INLINE, on the same
    line as ``text`` (no newline - the Run log / PR links sit right after the status).
    Prefers a specific PR-comment deep-link over the generic PR link; skips any empty
    url. Formatting lives here (not in the workflow) so a format tweak is one place."""
    refs = []
    if pr_comment:
        refs.append(f"<{pr_comment}|PR comment>")
    elif pr_url:
        refs.append(f"<{pr_url}|PR>")
    if run_url:
        refs.append(f"<{run_url}|Run log>")
    return f"{text} · {' · '.join(refs)}" if refs else text


# --- Slack transport (never raises out) ----------------------------------------


def _format_api_error(payload: dict) -> str:
    """Slack's error plus its missing_scope diagnostics (needed / provided), so a
    scope misconfig is legible straight from the log - the difference between
    ``missing_scope`` and ``missing_scope (needed='channels:history', provided=...)``."""
    detail = payload.get("error") or "unknown"
    needed, provided = payload.get("needed"), payload.get("provided")
    if needed or provided:
        detail += f" (needed={needed!r}, provided={provided!r})"
    return detail


def _call(method: str, request: urllib.request.Request) -> dict | None:
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _log(f"{method}: transport error: {exc}")
        return None
    if not payload.get("ok"):
        _log(f"{method}: api error: {_format_api_error(payload)}")
        return None
    return payload


def _post(method: str, token: str, body: dict) -> dict | None:
    request = urllib.request.Request(
        _API + method,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    return _call(method, request)


def _get(method: str, token: str, params: dict) -> dict | None:
    request = urllib.request.Request(
        _API + method + "?" + urllib.parse.urlencode(params),
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    return _call(method, request)


def _post_message(channel: str, text: str, token: str, thread_ts: str | None = None) -> str | None:
    body = {"channel": channel, "text": text}
    if thread_ts:
        body["thread_ts"] = thread_ts
    result = _post("chat.postMessage", token, body)
    return result.get("ts") if result else None


def _history(channel: str, token: str) -> list[dict] | None:
    result = _get("conversations.history", token, {"channel": channel, "limit": str(_HISTORY_SCAN)})
    return result.get("messages", []) if result is not None else None


def _pr_url(pr: int) -> str | None:
    """PR permalink from the standard GitHub Actions env (not a credential)."""
    server = os.environ.get("GITHUB_SERVER_URL")
    repo = os.environ.get("GITHUB_REPOSITORY")
    return f"{server}/{repo}/pull/{pr}" if server and repo else None


def _find_or_create_root(channel: str, model: str, pr: int, token: str) -> str | None:
    """Find the PR's thread root, or post one. A failed history read (e.g. the bot
    lacks ``channels:history``) is treated as no-match: we post a root and log it,
    so the notification still lands (degraded, not silent)."""
    messages = _history(channel, token)
    if messages is None:
        _log("history unavailable (missing scope / not in channel?) - posting a fresh root")
    else:
        existing = find_root_ts(messages, pr)
        if existing:
            return existing
    return _post_message(channel, build_root_text(model, pr, _pr_url(pr)), token)


# --- commands ------------------------------------------------------------------


def _note_failure(what: str) -> None:
    """Surface a genuinely-failed post as a GitHub Actions warning annotation so it
    is not a silent green step. The stderr ``[slack_notify]`` lines carry the detail
    (including which scope is missing); this just makes the run visibly flag it."""
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::warning title=Slack notification skipped::{what} (see [slack_notify] logs)")


def _ready(channel: str) -> str | None:
    """Return the bot token if a real post is possible, else None (dry-run)."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token or not channel:
        _log("no SLACK_BOT_TOKEN or channel - dry-run no-op")
        return None
    return token


def cmd_ensure_root(channel: str, model: str, pr: int) -> None:
    token = _ready(channel)
    if not token:
        return
    ts = _find_or_create_root(channel, model, pr, token)
    if ts:
        print(ts)
    else:
        _note_failure("could not create or find the thread root")


def cmd_reply(
    channel: str,
    pr: int,
    text: str,
    model: str,
    mention: bool,
    pr_comment: str = "",
    pr_url: str = "",
    run_url: str = "",
) -> None:
    token = _ready(channel)
    if not token:
        return
    thread_ts = _find_or_create_root(channel, model, pr, token)
    if not thread_ts:
        _log("no thread root and root post failed - dropping reply")
        _note_failure("could not post the thread reply (no root)")
        return
    prefix = build_mention_prefix(os.environ.get("SLACK_MENTIONS")) if mention else ""
    body = append_footer(text, pr_comment=pr_comment, pr_url=pr_url, run_url=run_url)
    if not _post_message(channel, prefix + body, token, thread_ts=thread_ts):
        _note_failure("could not post the thread reply")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Slack thread notifications for auto-integration.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    root = sub.add_parser("ensure-root", help="find or post the PR thread root; print its ts")
    root.add_argument("--channel", required=True)
    root.add_argument("--model", default="")
    root.add_argument("--pr", required=True, type=int)

    reply = sub.add_parser("reply", help="reply into the PR thread (create root if missing)")
    reply.add_argument("--channel", required=True)
    reply.add_argument("--pr", required=True, type=int)
    reply.add_argument("--text", required=True)
    reply.add_argument("--model", default="")
    reply.add_argument("--mention", action="store_true")
    reply.add_argument("--pr-comment", default="", help="deep-link to the PR comment with details")
    reply.add_argument("--pr-url", default="", help="fallback PR link when no comment url is known")
    reply.add_argument("--run-url", default="", help="Actions run URL, rendered as a Run log link")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "ensure-root":
            cmd_ensure_root(args.channel, args.model, args.pr)
        else:
            cmd_reply(
                args.channel,
                args.pr,
                args.text,
                args.model,
                args.mention,
                pr_comment=args.pr_comment,
                pr_url=args.pr_url,
                run_url=args.run_url,
            )
    except Exception as exc:  # a notification must never fail the job
        _log(f"unexpected error (ignored): {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
