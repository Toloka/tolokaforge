"""Unit tests for the automation Slack notifier's pure parts.

The Slack HTTP calls (thread ts flow) are not mockable in a meaningful way and are
validated live on a probe PR; here we cover the deterministic string-building and
root-matching that decide which thread a message lands in.
"""

from __future__ import annotations

import automation.slack as slack
import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_ambient_mention_env(monkeypatch):
    """`resolve_mentions` reads the environment, so a developer shell exporting either variable
    would leak into every test. Scrub both; the tests that WANT one set it explicitly."""
    monkeypatch.delenv(slack.REQUESTED_BY_ENV, raising=False)
    monkeypatch.delenv("SLACK_MENTIONS", raising=False)


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


class TestResolveMentions:
    """WHO a `--mention` notification pages: the Slack requester alone when the run came in through
    the channel, else the standing `SLACK_MENTIONS` list (a by-hand label add / manual dispatch has
    a GitHub actor but no Slack identity).
    """

    def test_the_requester_wins_over_the_standing_list(self, monkeypatch):
        monkeypatch.setenv("SLACK_MENTIONS", "U_OPS1,U_OPS2")
        monkeypatch.setenv(slack.REQUESTED_BY_ENV, "U0B1AN4QYMR")
        assert slack.resolve_mentions() == "U0B1AN4QYMR"

    def test_no_requester_keeps_the_standing_list(self, monkeypatch):
        monkeypatch.setenv("SLACK_MENTIONS", "U_OPS1")
        assert slack.resolve_mentions() == "U_OPS1"

    def test_no_requester_and_no_list_is_none(self):
        assert slack.resolve_mentions() is None

    def test_a_wrapped_id_is_normalised(self, monkeypatch):
        # The poller hands over a bare id, but tolerate the mention-shaped forms the rest of the
        # module already accepts.
        monkeypatch.setenv(slack.REQUESTED_BY_ENV, "<@U0B1AN4QYMR>")
        assert slack.resolve_mentions() == "U0B1AN4QYMR"

    @pytest.mark.parametrize(
        "junk", ["not-a-user", "u0lowercase", "U12", "rm -rf /", "U0B1 AN4QYMR", "*<!channel>*"]
    )
    def test_junk_is_dropped_not_rendered(self, junk, monkeypatch):
        """The value crossed from Slack metadata through a workflow input and comes back OUT as a
        mention - anything not shaped like a user id must fall back to the list, never interpolate.
        """
        monkeypatch.setenv("SLACK_MENTIONS", "U_OPS1")
        monkeypatch.setenv(slack.REQUESTED_BY_ENV, junk)
        assert slack.resolve_mentions() == "U_OPS1"

    def test_junk_with_no_list_is_none_not_the_junk(self, monkeypatch):
        monkeypatch.setenv(slack.REQUESTED_BY_ENV, "not-a-user")
        assert slack.resolve_mentions() is None


class TestLooksLikeSlackUserId:
    @pytest.mark.parametrize("good", ["U0B1AN4QYMR", "W12345", "<@U0B1AN4QYMR>", "@U12345"])
    def test_accepts_user_ids_and_their_wrappings(self, good):
        assert slack.looks_like_slack_user_id(good) is True

    @pytest.mark.parametrize("bad", ["", None, "not-a-user", "u0lowercase", "U12", "U1,U2"])
    def test_rejects_everything_else(self, bad):
        assert slack.looks_like_slack_user_id(bad) is False


class TestTheReplyPingsTheRequesterAlone:
    """`cmd_reply --mention` must page the resolved audience, not the raw standing list."""

    def _capture(self, monkeypatch):
        sent: list[str] = []
        monkeypatch.setattr(slack, "_ready", lambda *_a, **_k: "tok")
        monkeypatch.setattr(slack, "_find_or_create_root", lambda *_a, **_k: "1.0")
        monkeypatch.setattr(
            slack,
            "_post_message",
            lambda ch, text, tok, thread_ts=None: sent.append(text) or "9.9",
        )
        return sent

    def test_requester_present_pages_them_alone(self, monkeypatch):
        sent = self._capture(monkeypatch)
        monkeypatch.setenv("SLACK_MENTIONS", "U_OPS1,U_OPS2")
        monkeypatch.setenv(slack.REQUESTED_BY_ENV, "U0B1AN4QYMR")
        slack.cmd_reply("C", 7, "needs a human.", "m", mention=True)
        assert "<@U0B1AN4QYMR>" in sent[0]
        assert "U_OPS1" not in sent[0] and "U_OPS2" not in sent[0]

    def test_no_requester_pages_the_standing_list(self, monkeypatch):
        sent = self._capture(monkeypatch)
        monkeypatch.setenv("SLACK_MENTIONS", "U_OPS1,U_OPS2")
        slack.cmd_reply("C", 7, "needs a human.", "m", mention=True)
        assert "<@U_OPS1>" in sent[0] and "<@U_OPS2>" in sent[0]

    def test_without_mention_no_one_is_paged(self, monkeypatch):
        sent = self._capture(monkeypatch)
        monkeypatch.setenv("SLACK_MENTIONS", "U_OPS1")
        monkeypatch.setenv(slack.REQUESTED_BY_ENV, "U0B1AN4QYMR")
        slack.cmd_reply("C", 7, "observe started", "m", mention=False)
        assert "U0B1AN4QYMR" not in sent[0] and "U_OPS1" not in sent[0]
