"""Message icons addressed by ROLE, so a workspace can swap any one of them.

Every emoji the notifier puts in front of a message has a role name here, and
one JSON variable maps roles to whatever the workspace uploaded::

    ARENA_AUTOMATION_SLACK_ICON_OVERRIDE='{"observe_started": ":tf-observe-started:",
                              "integrated":      ":tf-integrated:"}'

Keyed on the ROLE, not on the standard emoji it defaults to, and that is the
point. Four different messages share ``:warning:`` today and three share
``:white_check_mark:``, so a map keyed on the standard name could not give any
of those separate icons - one entry would restyle them all.

The custom emoji themselves are never committed: they are uploaded to the
workspace, and this module only needs their names. Nothing here can check that
an icon exists - Slack renders a name it does not have as literal ``:name:``
text - so an unknown ROLE is reported loudly (that is a typo we CAN catch) while
the value is only charset-checked.
"""

from __future__ import annotations

import json
import os
import re
import sys

# One JSON object, role -> icon. Named for the ARENA_AUTOMATION_SLACK_* family it
# joins (bot token, channel, mentions), and the SAME name the tasks repo's
# eval-orchestrator notifier reads, so one value styles both flows; each side
# validates against its own role registry, so a role that only exists in the
# other one is reported where it was set.
ICON_OVERRIDES_ENV = "ARENA_AUTOMATION_SLACK_ICON_OVERRIDE"

# role -> the standard Slack shortcode used when nothing overrides it.
#
# These defaults reproduce exactly what the flow sends today, so an unset
# variable changes nothing. Note the pairs that share a glyph but not a meaning:
# `integrated` and `integrated_merged` differ by whether the PR was merged, and
# the four `needs_human_*` roles differ by what a human has to look at. Each is
# now settable on its own.
DEFAULT_ICONS: dict[str, str] = {
    # Pipeline stages
    "observe_started": ":arrow_forward:",
    "observe_clean": ":white_check_mark:",
    "resolve_started": ":wrench:",
    "integrated": ":white_check_mark:",
    "integrated_merged": ":white_check_mark:",
    "pr_opened": ":rocket:",
    # Attention states. `needs_human` is the ordinary gate; the agent-requested
    # one is raised by the integration agent itself, which is why it has its own
    # glyph today, and `pipeline_error` is an unexpected failure rather than a
    # judgement call.
    "needs_human": ":warning:",
    "needs_human_agent": ":raising_hand:",
    "pipeline_error": ":rotating_light:",
    "dispatch_failed": ":x:",
    # Request intake: the poller's in-thread reply to an "@bot integrate ..." message. These
    # address the REQUESTER, before any PR exists, which is why none of them reuses a
    # pipeline-stage role that happens to share a glyph today - `dispatch_failed` (:x:) is an
    # infra failure AFTER resolution succeeded, while `request_unresolved` is a name that matched
    # nothing, and a workspace will want to tell those apart.
    "request_resolved": ":white_check_mark:",
    "request_ambiguous": ":warning:",
    "request_unresolved": ":x:",
    # A `via <route>` directive that could not be honoured.
    "route_downgraded": ":warning:",
}

# Slack emoji names are lowercase letters, digits, and - _ +. Anything else
# cannot name an emoji, so an entry carrying it is dropped rather than sent: the
# message would show literal `:not a name:` text.
_ICON_NAME_RE = re.compile(r"^[a-z0-9_+-]+$")


def _log(message: str) -> None:
    print(f"[slack_notify] {message}", file=sys.stderr)


def known_roles() -> list[str]:
    """Every role a map may set, sorted (for help text and error messages)."""
    return sorted(DEFAULT_ICONS)


def load_icon_overrides(raw: str | None = None) -> dict[str, str]:
    """Parse the role -> icon map. Never raises.

    *raw* defaults to ``ARENA_AUTOMATION_SLACK_ICON_OVERRIDE``. Icon values are accepted with
    or without surrounding colons, so ``":tf-pass:"`` and ``"tf-pass"`` are the
    same value.

    Fail-soft, and per entry: unparseable JSON yields an empty map and a
    warning, while one unusable entry is dropped by name and the rest still
    apply. A notification must never fail the job it reports on, and one typo is
    not a reason to send every message unstyled.

    An UNKNOWN role is reported loudly rather than ignored. It is the one error
    detectable from here - the icon's existence in the workspace is not - and a
    silently-ignored role looks exactly like a working override that did nothing.
    """
    payload = os.environ.get(ICON_OVERRIDES_ENV, "") if raw is None else raw
    payload = (payload or "").strip()
    if not payload:
        return {}
    try:
        parsed = json.loads(payload)
    except ValueError as exc:
        _log(f"icons: ignoring unparseable {ICON_OVERRIDES_ENV} ({exc})")
        return {}
    if not isinstance(parsed, dict):
        _log(f"icons: {ICON_OVERRIDES_ENV} must be a JSON object, got {type(parsed).__name__}")
        return {}

    overrides: dict[str, str] = {}
    for raw_role, raw_icon in parsed.items():
        role = str(raw_role).strip().casefold()
        name = str(raw_icon).strip().strip(":").casefold()
        if role not in DEFAULT_ICONS:
            _log(
                f"icons: unknown role {raw_role!r} ignored; known roles are "
                f"{', '.join(known_roles())}"
            )
            continue
        if not _ICON_NAME_RE.match(name):
            _log(f"icons: dropping unusable icon for {role!r}: {raw_icon!r}")
            continue
        overrides[role] = f":{name}:"
    return overrides


def icon(role: str, overrides: dict[str, str] | None = None) -> str:
    """The icon for *role*, as ``:name:``.

    An unknown role raises: roles are written by this codebase, so a bad one is
    a bug here rather than a user's typo, and returning a blank or a placeholder
    would ship a message with a missing icon and no explanation.
    """
    if role not in DEFAULT_ICONS:
        raise ValueError(f"Unknown icon role {role!r}; known roles are {', '.join(known_roles())}.")
    if overrides is None:
        overrides = load_icon_overrides()
    return overrides.get(role, DEFAULT_ICONS[role])


def prefix(role: str, text: str, overrides: dict[str, str] | None = None) -> str:
    """``<icon> <text>``, or *text* unchanged when *role* is empty.

    An empty role is the "no icon wanted" case a CLI flag leaves unset, and it
    must not be an error - some call sites pass text that already carries a lead.
    """
    if not role:
        return text
    return f"{icon(role, overrides)} {text}".strip()
