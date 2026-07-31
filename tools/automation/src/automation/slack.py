"""Slack thread notifications for the model automation pipeline.

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
cleanly). Nothing here exits non-zero on a Slack failure - a notification must
never fail the job.

Subcommands (the ``slack`` sub-app):
  ensure-root --channel <id> --model <name> --pr <N>
      Find (or post) the thread root; print its ts to stdout. Idempotent: a
      re-trigger reuses the existing root instead of opening a new one.
  reply --channel <id> --pr <N> --text <msg> [--model <name>] [--mention]
      Reply into the PR's thread (creating the root first if missing). With
      ``--mention`` the configured ``SLACK_MENTIONS`` users are appended so they
      get pinged; used for the terminal / needs-human / error notifications.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

import typer

from . import icons

_API = "https://slack.com/api/"
_TIMEOUT = 15
_HISTORY_SCAN = 500  # bounded scan cap: newest N top-level messages (headroom for the 48h window)


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


def build_mention_suffix(raw: str | None) -> str:
    """Trailing reviewer line for a terminal notification: ping the configured
    ``SLACK_MENTIONS`` at the END of the message (under the footer links), or note that
    none are configured. Reuses :func:`build_mention_prefix` for id normalisation."""
    ids = build_mention_prefix(raw).strip()
    sep = chr(10) * 2  # blank line so the reviewer line sits below the links
    return f"{sep}Notifying: {ids}" if ids else f"{sep}No reviewers configured to notify."


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


def _history(channel: str, token: str, oldest: str | None = None) -> list[dict] | None:
    params = {"channel": channel, "limit": str(_HISTORY_SCAN)}
    if oldest:  # Unix-ts lower bound: only messages newer than this (the poller's 48h window)
        params["oldest"] = oldest
    result = _get("conversations.history", token, params)
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
    role: str = "",
) -> None:
    token = _ready(channel)
    if not token:
        return
    thread_ts = _find_or_create_root(channel, model, pr, token)
    if not thread_ts:
        _log("no thread root and root post failed - dropping reply")
        _note_failure("could not post the thread reply (no root)")
        return
    body = append_footer(
        icons.prefix(role, text), pr_comment=pr_comment, pr_url=pr_url, run_url=run_url
    )
    if mention:
        body += build_mention_suffix(os.environ.get("SLACK_MENTIONS"))
    if not _post_message(channel, body, token, thread_ts=thread_ts):
        _note_failure("could not post the thread reply")


# --- typer sub-app -------------------------------------------------------------

app = typer.Typer(
    help="Slack thread notifications for the automation pipeline.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command("ensure-root")
def ensure_root(
    channel: str = typer.Option(..., "--channel"),
    model: str = typer.Option("", "--model"),
    pr: int = typer.Option(..., "--pr"),
) -> None:
    """Find or post the PR thread root; print its ts."""
    try:
        cmd_ensure_root(channel, model, pr)
    except Exception as exc:  # a notification must never fail the job
        _log(f"unexpected error (ignored): {exc}")
    raise typer.Exit(0)


@app.command("post-thread")
def post_thread(
    channel: str = typer.Option(..., "--channel"),
    thread_ts: str = typer.Option(..., "--thread-ts", help="parent message ts to reply under"),
    text: str = typer.Option(..., "--text"),
) -> None:
    """Post a plain threaded reply under an arbitrary message ts. The poller workflow uses this to
    confirm a specific Slack request IN ITS OWN THREAD after the PR opens (with the PR link) or to
    report that starting it failed - the PR-keyed ``reply`` above threads on a different root."""
    try:
        token = _ready(channel)
        if token and not _post_message(channel, text, token, thread_ts=thread_ts):
            _note_failure("could not post the threaded follow-up")
    except Exception as exc:  # a notification must never fail the job
        _log(f"unexpected error (ignored): {exc}")
    raise typer.Exit(0)


@app.command("reply")
def reply(
    channel: str = typer.Option(..., "--channel"),
    pr: int = typer.Option(..., "--pr"),
    text: str = typer.Option(..., "--text"),
    model: str = typer.Option("", "--model"),
    mention: bool = typer.Option(False, "--mention"),
    pr_comment: str = typer.Option(
        "", "--pr-comment", help="deep-link to the PR comment with details"
    ),
    pr_url: str = typer.Option(
        "", "--pr-url", help="fallback PR link when no comment url is known"
    ),
    run_url: str = typer.Option(
        "", "--run-url", help="Actions run URL, rendered as a Run log link"
    ),
    icon: str = typer.Option(
        "",
        "--icon",
        help="Icon ROLE to prefix, e.g. observe_started. The role's emoji comes "
        "from `icons.DEFAULT_ICONS` unless TOLOKAFORGE_SLACK_ICONS overrides it. "
        "Empty = no icon, for text that already carries its own lead.",
    ),
) -> None:
    """Reply into the PR thread (create root if missing)."""
    try:
        cmd_reply(
            channel,
            pr,
            text,
            model,
            mention,
            pr_comment=pr_comment,
            pr_url=pr_url,
            run_url=run_url,
            role=icon,
        )
    except Exception as exc:  # a notification must never fail the job
        _log(f"unexpected error (ignored): {exc}")
    raise typer.Exit(0)
