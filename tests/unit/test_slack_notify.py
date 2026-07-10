"""Unit tests for the auto-integration Slack notifier's pure parts.

The Slack HTTP calls (thread ts flow) are not mockable in a meaningful way and are
validated live on a probe PR; here we cover the deterministic string-building and
root-matching that decide which thread a message lands in.

``scripts/integration`` is not an importable package, so the module is loaded from
its file path (same approach as the tool-factory import-path tests).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "integration" / "slack_notify.py"
_spec = importlib.util.spec_from_file_location("slack_notify", _MODULE_PATH)
slack_notify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(slack_notify)


class TestBuildRootText:
    def test_contains_match_tokens(self):
        text = slack_notify.build_root_text("qwen/qwen3.6-plus", 42)
        assert "Auto-integration" in text
        assert "(PR #42)" in text
        assert "`qwen/qwen3.6-plus`" in text

    def test_appends_pr_url(self):
        text = slack_notify.build_root_text("x/y", 42, "https://github.com/o/r/pull/42")
        assert text.endswith("\nhttps://github.com/o/r/pull/42")

    def test_no_url_when_absent(self):
        assert "\n" not in slack_notify.build_root_text("x/y", 42)

    def test_empty_model_still_matches(self):
        text = slack_notify.build_root_text("", 42)
        assert slack_notify.root_matches(text, 42)
        assert "`" not in text  # no empty code span


class TestRootMatches:
    def test_positive(self):
        assert slack_notify.root_matches("Auto-integration: `m` (PR #42)", 42)

    def test_wrong_pr(self):
        assert not slack_notify.root_matches("Auto-integration: `m` (PR #42)", 7)

    def test_pr_number_is_self_delimiting(self):
        # (PR #4) must not match against a (PR #42) message and vice versa.
        assert not slack_notify.root_matches("Auto-integration: `m` (PR #42)", 4)
        assert not slack_notify.root_matches("Auto-integration: `m` (PR #4)", 42)

    def test_requires_auto_integration_marker(self):
        # A human message that merely mentions the PR number is not a root.
        assert not slack_notify.root_matches("see (PR #42) for details", 42)


class TestFindRootTs:
    def test_single_match(self):
        msgs = [{"ts": "100.1", "text": "Auto-integration: `m` (PR #42)"}]
        assert slack_notify.find_root_ts(msgs, 42) == "100.1"

    def test_no_match_returns_none(self):
        msgs = [{"ts": "100.1", "text": "unrelated chatter"}]
        assert slack_notify.find_root_ts(msgs, 42) is None

    def test_picks_oldest_on_duplicates(self):
        msgs = [
            {"ts": "200.2", "text": "Auto-integration: `m` (PR #42)"},
            {"ts": "100.1", "text": "Auto-integration: `m` (PR #42)"},
        ]
        assert slack_notify.find_root_ts(msgs, 42) == "100.1"

    def test_skips_messages_without_ts_or_text(self):
        msgs = [
            {"text": "Auto-integration: `m` (PR #42)"},  # no ts
            {"ts": "50.0"},  # no text
            {"ts": "100.1", "text": "Auto-integration: `m` (PR #42)"},
        ]
        assert slack_notify.find_root_ts(msgs, 42) == "100.1"

    def test_ignores_join_events(self):
        msgs = [{"ts": "10.0", "text": "<@U1> has joined the channel"}]
        assert slack_notify.find_root_ts(msgs, 42) is None


class TestFormatApiError:
    def test_plain_error(self):
        assert slack_notify._format_api_error({"error": "channel_not_found"}) == "channel_not_found"

    def test_missing_scope_surfaces_needed_and_provided(self):
        # The exact signal that would have short-circuited this debugging round.
        detail = slack_notify._format_api_error(
            {
                "error": "missing_scope",
                "needed": "channels:history",
                "provided": "chat:write,incoming-webhook",
            }
        )
        assert "missing_scope" in detail
        assert "channels:history" in detail
        assert "chat:write,incoming-webhook" in detail

    def test_unknown_when_no_error_key(self):
        assert slack_notify._format_api_error({}) == "unknown"


class TestBuildMentionPrefix:
    def test_empty(self):
        assert slack_notify.build_mention_prefix("") == ""
        assert slack_notify.build_mention_prefix(None) == ""

    def test_single(self):
        assert slack_notify.build_mention_prefix("U0B1AN4QYMR") == "<@U0B1AN4QYMR> "

    def test_multiple_comma_separated(self):
        assert slack_notify.build_mention_prefix("U1, U2 ,U3") == "<@U1> <@U2> <@U3> "

    def test_tolerates_at_and_bracket_forms(self):
        assert slack_notify.build_mention_prefix("@U1") == "<@U1> "
        assert slack_notify.build_mention_prefix("<@U1>") == "<@U1> "

    def test_drops_empty_tokens(self):
        assert slack_notify.build_mention_prefix("U1,,  ,U2") == "<@U1> <@U2> "

    def test_only_empty_tokens_yields_no_prefix(self):
        assert slack_notify.build_mention_prefix(" , , ") == ""
