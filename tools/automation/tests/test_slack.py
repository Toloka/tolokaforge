"""Unit tests for the automation Slack notifier's pure parts.

The Slack HTTP calls (thread ts flow) are not mockable in a meaningful way and are
validated live on a probe PR; here we cover the deterministic string-building and
root-matching that decide which thread a message lands in.
"""

from __future__ import annotations

import automation.slack as slack
import pytest

pytestmark = pytest.mark.unit


class TestBuildRootText:
    def test_contains_match_tokens(self):
        text = slack.build_root_text("qwen/qwen3.6-plus", 42)
        assert "Auto-integration" in text
        assert "(PR #42)" in text
        assert "`qwen/qwen3.6-plus`" in text

    def test_hyperlinks_pr_when_url_given(self):
        text = slack.build_root_text("x/y", 42, "https://github.com/o/r/pull/42")
        assert "<https://github.com/o/r/pull/42|#42>" in text  # clickable #42, not a bare URL
        assert chr(10) not in text
        assert slack.root_matches(text, 42)

    def test_no_url_is_single_line_plain_ref(self):
        text = slack.build_root_text("x/y", 42)
        assert chr(10) not in text
        assert "(PR #42)" in text

    def test_empty_model_still_matches(self):
        text = slack.build_root_text("", 42)
        assert slack.root_matches(text, 42)
        assert "`" not in text  # no empty code span


class TestRootMatches:
    def test_positive(self):
        assert slack.root_matches("Auto-integration: `m` (PR #42)", 42)

    def test_wrong_pr(self):
        assert not slack.root_matches("Auto-integration: `m` (PR #42)", 7)

    def test_pr_number_is_self_delimiting(self):
        # (PR #4) must not match against a (PR #42) message and vice versa.
        assert not slack.root_matches("Auto-integration: `m` (PR #42)", 4)
        assert not slack.root_matches("Auto-integration: `m` (PR #4)", 42)

    def test_requires_automation_marker(self):
        # A human message that merely mentions the PR number is not a root.
        assert not slack.root_matches("see (PR #42) for details", 42)

    def test_matches_hyperlinked_form(self):
        # The root renders #<pr> as a Slack link <url|#42>; matching must survive that,
        # and the #4-vs-#42 boundary must still hold inside the link label.
        linked = slack.build_root_text("m", 42, "https://github.com/o/r/pull/42")
        assert slack.root_matches(linked, 42)
        assert not slack.root_matches(linked, 4)


class TestFindRootTs:
    def test_single_match(self):
        msgs = [{"ts": "100.1", "text": "Auto-integration: `m` (PR #42)"}]
        assert slack.find_root_ts(msgs, 42) == "100.1"

    def test_no_match_returns_none(self):
        msgs = [{"ts": "100.1", "text": "unrelated chatter"}]
        assert slack.find_root_ts(msgs, 42) is None

    def test_picks_oldest_on_duplicates(self):
        msgs = [
            {"ts": "200.2", "text": "Auto-integration: `m` (PR #42)"},
            {"ts": "100.1", "text": "Auto-integration: `m` (PR #42)"},
        ]
        assert slack.find_root_ts(msgs, 42) == "100.1"

    def test_skips_messages_without_ts_or_text(self):
        msgs = [
            {"text": "Auto-integration: `m` (PR #42)"},  # no ts
            {"ts": "50.0"},  # no text
            {"ts": "100.1", "text": "Auto-integration: `m` (PR #42)"},
        ]
        assert slack.find_root_ts(msgs, 42) == "100.1"

    def test_ignores_join_events(self):
        msgs = [{"ts": "10.0", "text": "<@U1> has joined the channel"}]
        assert slack.find_root_ts(msgs, 42) is None


class TestAppendFooter:
    def test_no_links_returns_text_unchanged(self):
        assert slack.append_footer("hi") == "hi"

    def test_run_url_only(self):
        assert slack.append_footer("hi", run_url="R") == "hi · <R|Run log>"

    def test_pr_comment_and_run(self):
        assert (
            slack.append_footer("hi", pr_comment="C", run_url="R")
            == "hi · <C|PR comment> · <R|Run log>"
        )

    def test_pr_comment_preferred_over_pr_url(self):
        # Workflow passes both (comment url may be empty if gh failed); comment wins.
        assert slack.append_footer("hi", pr_comment="C", pr_url="P") == "hi · <C|PR comment>"

    def test_pr_url_fallback_when_no_comment(self):
        assert slack.append_footer("hi", pr_comment="", pr_url="P") == "hi · <P|PR>"

    def test_empty_urls_skipped(self):
        assert slack.append_footer("hi", pr_comment="", pr_url="", run_url="") == "hi"


class TestFormatApiError:
    def test_plain_error(self):
        assert slack._format_api_error({"error": "channel_not_found"}) == "channel_not_found"

    def test_missing_scope_surfaces_needed_and_provided(self):
        # The exact signal that would have short-circuited this debugging round.
        detail = slack._format_api_error(
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
        assert slack._format_api_error({}) == "unknown"


class TestBuildMentionPrefix:
    def test_empty(self):
        assert slack.build_mention_prefix("") == ""
        assert slack.build_mention_prefix(None) == ""

    def test_single(self):
        assert slack.build_mention_prefix("U0B1AN4QYMR") == "<@U0B1AN4QYMR> "

    def test_multiple_comma_separated(self):
        assert slack.build_mention_prefix("U1, U2 ,U3") == "<@U1> <@U2> <@U3> "

    def test_tolerates_at_and_bracket_forms(self):
        assert slack.build_mention_prefix("@U1") == "<@U1> "
        assert slack.build_mention_prefix("<@U1>") == "<@U1> "

    def test_drops_empty_tokens(self):
        assert slack.build_mention_prefix("U1,,  ,U2") == "<@U1> <@U2> "

    def test_only_empty_tokens_yields_no_prefix(self):
        assert slack.build_mention_prefix(" , , ") == ""


class TestMentionSuffix:
    def test_configured_mentions_render_a_trailing_notifying_line(self):
        sep = chr(10) * 2
        assert slack.build_mention_suffix("U0B1AN4QYMR") == sep + "Notifying: <@U0B1AN4QYMR>"
        assert slack.build_mention_suffix("U1, U2") == sep + "Notifying: <@U1> <@U2>"

    def test_no_mentions_notes_none_configured(self):
        expected = chr(10) * 2 + "No reviewers configured to notify."
        assert slack.build_mention_suffix("") == expected
        assert slack.build_mention_suffix(None) == expected
        assert slack.build_mention_suffix(" , , ") == expected
