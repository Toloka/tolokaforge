"""Unit tests for the Slack-triggered integration poller's pure parts.

The Slack HTTP calls (auth.test / history / replies / postMessage) are validated live on
the automation channel; here we cover the deterministic request-detection, allowlist, dedup,
and the resolve -> slug plan over the real request ("integrate GPT 5.6, Grok 4.5 and Hy3")
against a fixture catalog, so a resolver regression that would dispatch the wrong model fails
here without a network call.
"""

from __future__ import annotations

import automation.model_resolver as mr
import automation.poller as poller
import pytest
from automation import gateway_catalog

pytestmark = pytest.mark.unit

BOT = "UBOT001"

# Mirrors the live OpenRouter catalog for the three requested models plus near-version
# siblings, so the version-discipline (4.5 != 4 != 4.3) is exercised by the poller's plan.
CATALOG = [
    "openai/gpt-5.6-luna",
    "openai/gpt-5.6-luna-pro",
    "openai/gpt-5.6-sol",
    "openai/gpt-5.6-sol-pro",
    "openai/gpt-5.6-terra",
    "openai/gpt-5.6-terra-pro",
    "x-ai/grok-4",
    "x-ai/grok-4.3",
    "x-ai/grok-4.5",
    "tencent/hy3",
    "tencent/hunyuan-a13b-instruct",
    "anthropic/claude-opus-4.8",
]

REAL_REQUEST = f"<@{BOT}|delivery-tech-bot> Integrate GPT 5.6, Grok 4.5 and Hy3"


class TestMentionsBot:
    def test_bare_mention(self):
        assert poller.mentions_bot(f"<@{BOT}> hi", BOT)

    def test_labelled_mention(self):
        assert poller.mentions_bot(f"<@{BOT}|delivery-tech-bot> hi", BOT)

    def test_other_user_mention_is_not_us(self):
        assert not poller.mentions_bot("<@USOMEONE> hi", BOT)

    def test_no_mention(self):
        assert not poller.mentions_bot("integrate grok 4.5", BOT)


class TestIsRequest:
    def test_plain_integrate_request(self):
        assert poller.is_request({"user": "UHUMAN", "text": f"<@{BOT}> integrate grok 4.5"}, BOT)

    def test_labelled_mention_request(self):
        assert poller.is_request({"user": "UHUMAN", "text": REAL_REQUEST}, BOT)

    def test_bots_own_message_ignored(self):
        assert not poller.is_request({"user": BOT, "text": f"<@{BOT}> integrate x"}, BOT)

    def test_bot_authored_message_ignored(self):
        assert not poller.is_request({"bot_id": "B1", "text": f"<@{BOT}> integrate x"}, BOT)

    def test_join_event_ignored(self):
        assert not poller.is_request(
            {"subtype": "channel_join", "text": f"<@{BOT}> has joined"}, BOT
        )

    def test_mention_without_integrate_ignored(self):
        assert not poller.is_request({"user": "UHUMAN", "text": f"<@{BOT}> hello there"}, BOT)

    def test_integrate_without_mention_ignored(self):
        assert not poller.is_request({"user": "UHUMAN", "text": "integrate grok 4.5"}, BOT)


class TestAllowlist:
    def test_parse_tolerates_forms(self):
        assert poller.parse_allowed("<@U1>, @U2 , U3") == frozenset({"U1", "U2", "U3"})

    def test_parse_empty(self):
        assert poller.parse_allowed(None) == frozenset()
        assert poller.parse_allowed("") == frozenset()

    def test_empty_allowlist_allows_anyone(self):
        assert poller.is_allowed("UANY", frozenset())

    def test_non_empty_allowlist_gates(self):
        allowed = frozenset({"U1"})
        assert poller.is_allowed("U1", allowed)
        assert not poller.is_allowed("U2", allowed)


class TestHistoryWindow:
    def test_oldest_is_now_minus_window(self):
        # 6h before a fixed 'now' (seconds), formatted as a Slack Unix-ts string.
        assert poller.history_oldest(1_000_000.0, 6) == f"{1_000_000.0 - 6 * 3600:.6f}"

    def test_zero_or_negative_window_is_unbounded(self):
        assert poller.history_oldest(1_000_000.0, 0) == ""
        assert poller.history_oldest(1_000_000.0, -1) == ""


class TestBotReplied:
    def test_bot_reply_present(self):
        replies = [{"user": "UHUMAN", "ts": "100"}, {"user": BOT, "ts": "101"}]
        assert poller.bot_replied(replies, BOT, "100")

    def test_only_human_replies(self):
        replies = [{"user": "UHUMAN", "ts": "100"}, {"user": "UOTHER", "ts": "101"}]
        assert not poller.bot_replied(replies, BOT, "100")

    def test_parent_only(self):
        assert not poller.bot_replied([{"user": "UHUMAN", "ts": "100"}], BOT, "100")

    def test_parent_authored_by_bot_is_not_a_reply(self):
        # The parent carries parent_ts; even if authored by the bot it is not a reply-marker.
        assert not poller.bot_replied([{"user": BOT, "ts": "100"}], BOT, "100")


class TestResolvedSlugsPlan:
    def _resolutions(self):
        return mr.resolve_all(REAL_REQUEST, CATALOG, poller.ALIASES)

    def test_real_request_resolves_two_starts_one_ambiguous(self):
        res = self._resolutions()
        by_status = {r.status for r in res}
        assert by_status == {"resolved", "ambiguous"}
        assert poller.resolved_slugs(res) == ["x-ai/grok-4.5", "tencent/hy3"]

    def test_version_discipline_no_sibling_collapse(self):
        # Grok 4.5 must not pull in grok-4 / grok-4.3.
        slugs = poller.resolved_slugs(self._resolutions())
        assert "x-ai/grok-4" not in slugs
        assert "x-ai/grok-4.3" not in slugs

    def test_ambiguous_is_not_dispatched(self):
        # GPT 5.6 (6 candidates) must NOT appear as a start - it needs an exact slug.
        slugs = poller.resolved_slugs(self._resolutions())
        assert not any(s.startswith("openai/gpt-5.6") for s in slugs)

    def test_reply_lists_starts_and_asks_to_clarify(self):
        reply = mr.format_resolution_reply("UHUMAN", self._resolutions())
        assert "<@UHUMAN>" in reply
        assert "x-ai/grok-4.5" in reply and "tencent/hy3" in reply
        assert "ambiguous" in reply
        # every candidate the user must choose between is offered
        assert "openai/gpt-5.6-sol" in reply and "openai/gpt-5.6-terra" in reply

    def test_dedup_of_repeated_slug(self):
        res = [
            mr.Resolution("a", "resolved", slug="x/y"),
            mr.Resolution("b", "resolved", slug="x/y"),
            mr.Resolution("c", "resolved", slug="x/z"),
        ]
        assert poller.resolved_slugs(res) == ["x/y", "x/z"]

    def test_unsafe_charset_slug_dropped(self):
        # Defence-in-depth: a slug with a shell metacharacter never reaches the plan (so it can
        # never be interpolated into the bash bootstrap), even if the catalog somehow yielded it.
        res = [
            mr.Resolution("a", "resolved", slug="x-ai/grok-4.5"),
            mr.Resolution("b", "resolved", slug="evil/$(rm -rf /)"),
            mr.Resolution("c", "resolved", slug="also/`whoami`"),
        ]
        assert poller.resolved_slugs(res) == ["x-ai/grok-4.5"]

    def test_demote_unsafe_slug_variant_becomes_clarify(self):
        # A ':free' / ':nitro' variant resolves but ':' fails _SAFE_SLUG_RE. It must be demoted to
        # a clarify-with-the-base-slug reply BEFORE it is confirmed - not confirmed-then-dropped
        # (which would leave the request un-run and un-retryable behind its own dedup marker).
        variant = mr.Resolution("deepseek free", "resolved", slug="deepseek/deepseek-v3.2:free")
        out = poller.demote_unsafe_slug(variant)
        assert out.status == "ambiguous"
        assert out.slug is None
        assert out.candidates == ("deepseek/deepseek-v3.2",)
        # A plain resolved slug (and any non-resolved one) is returned untouched.
        plain = mr.Resolution("grok", "resolved", slug="x-ai/grok-4.5")
        assert poller.demote_unsafe_slug(plain) is plain
        amb = mr.Resolution("gpt", "ambiguous", candidates=("a/b", "a/c"))
        assert poller.demote_unsafe_slug(amb) is amb
        # A demoted variant is excluded from the plan (status != resolved).
        assert poller.resolved_slugs([out]) == []


class TestRoutePlan:
    """The route is decided PER MODEL, and a directive that cannot be honoured is stated.

    The two sides of one request can disagree: a gateway-only model can run nowhere but the
    gateway, while an OpenRouter model must keep the calibrated default, because moving a model's
    serving path is a leaderboard-comparability decision.
    """

    GATEWAY_ONLY = "azure_ai/cohere-command-a-plus-05-2026"

    def _resolutions(self, text, gateway_entries=None):
        return mr.resolve_all(text, CATALOG, gateway_entries=gateway_entries)

    def _availability(self, resolutions, catalog):
        return {
            r.slug: gateway_catalog.lookup(r.slug, catalog)
            for r in resolutions
            if r.status == "resolved" and r.slug
        }

    def test_mixed_request_routes_each_model_where_it_can_run(self):
        entries = [self.GATEWAY_ONLY]
        resolutions = self._resolutions(
            f"<@{BOT}> integrate grok 4.5 and {self.GATEWAY_ONLY}", gateway_entries=entries
        )
        plan = poller.route_plan(resolutions, self._availability(resolutions, entries), None)
        assert plan.routes == {
            "x-ai/grok-4.5": gateway_catalog.ROUTE_OPENROUTER,
            self.GATEWAY_ONLY: gateway_catalog.ROUTE_GATEWAY,
        }
        assert plan.warnings == ()

    def test_via_openrouter_cannot_be_honoured_for_a_gateway_only_model(self):
        entries = [self.GATEWAY_ONLY]
        resolutions = self._resolutions(
            f"<@{BOT}> integrate {self.GATEWAY_ONLY}", gateway_entries=entries
        )
        plan = poller.route_plan(
            resolutions,
            self._availability(resolutions, entries),
            gateway_catalog.ROUTE_OPENROUTER,
        )
        assert plan.routes == {self.GATEWAY_ONLY: gateway_catalog.ROUTE_GATEWAY}
        assert len(plan.warnings) == 1
        assert "cannot be honoured" in plan.warnings[0]
        assert self.GATEWAY_ONLY in plan.warnings[0]

    def test_via_litellm_is_downgraded_when_the_gateway_lacks_the_model(self):
        resolutions = self._resolutions(f"<@{BOT}> integrate grok 4.5")
        plan = poller.route_plan(
            resolutions,
            self._availability(resolutions, []),  # catalog read, model absent
            gateway_catalog.ROUTE_GATEWAY,
        )
        assert plan.routes == {"x-ai/grok-4.5": gateway_catalog.ROUTE_OPENROUTER}
        assert plan.requested_route is None  # the reply must report the route actually used
        assert "could not confirm the gateway serves every model" in plan.warnings[0]

    def test_a_gateway_only_model_does_not_trigger_the_downgrade(self):
        # It came OUT of the gateway catalog, so it is not evidence against the gateway; a
        # downgrade here would have reported OpenRouter for a model OpenRouter does not carry.
        entries = [self.GATEWAY_ONLY]
        resolutions = self._resolutions(
            f"<@{BOT}> integrate {self.GATEWAY_ONLY} via litellm", gateway_entries=entries
        )
        plan = poller.route_plan(
            resolutions,
            self._availability(resolutions, entries),
            gateway_catalog.ROUTE_GATEWAY,
        )
        assert plan.routes == {self.GATEWAY_ONLY: gateway_catalog.ROUTE_GATEWAY}
        assert plan.warnings == ()

    def test_a_downgrade_does_not_then_suggest_the_route_it_refused(self):
        # `requested_route` arrives as None both when nothing was asked and when a directive was
        # refused. Without that distinction the reply tells a requester who just asked for the
        # gateway to re-request with the gateway.
        import automation.model_resolver as resolver

        entries = ["x-ai/grok-4.5"]  # gateway has grok but not hy3
        resolutions = self._resolutions(
            f"<@{BOT}> integrate grok 4.5 and hy3 via litellm", gateway_entries=entries
        )
        availability = self._availability(resolutions, entries)
        plan = poller.route_plan(resolutions, availability, gateway_catalog.ROUTE_GATEWAY)
        assert plan.downgraded is True
        reply = resolver.format_resolution_reply(
            "U1",
            resolutions,
            availability,
            plan.requested_route,
            gateway_searched=True,
            route_downgraded=plan.downgraded,
        )
        assert "re-request with `via litellm`" not in reply
        # ... while the un-asked case still offers the option.
        plain = poller.route_plan(resolutions, availability, None)
        assert plain.downgraded is False
        assert "re-request with `via litellm`" in resolver.format_resolution_reply(
            "U1",
            resolutions,
            availability,
            plain.requested_route,
            gateway_searched=True,
            route_downgraded=plain.downgraded,
        )

    def test_the_downgrade_warning_does_not_speak_for_a_gateway_only_model(self):
        # A mixed request downgrades only what OpenRouter carries, so a warning saying the whole
        # message runs over openrouter would contradict the route written to plan.json.
        entries = [self.GATEWAY_ONLY]
        resolutions = self._resolutions(
            f"<@{BOT}> integrate grok 4.5 and {self.GATEWAY_ONLY} via litellm",
            gateway_entries=entries,
        )
        plan = poller.route_plan(
            resolutions,
            self._availability(resolutions, entries),
            gateway_catalog.ROUTE_GATEWAY,
        )
        assert plan.routes[self.GATEWAY_ONLY] == gateway_catalog.ROUTE_GATEWAY
        assert plan.routes["x-ai/grok-4.5"] == gateway_catalog.ROUTE_OPENROUTER
        assert plan.warnings, "grok is absent from the gateway, so the directive is refused"
        assert "the models OpenRouter carries run over" in plan.warnings[0]

    def test_one_simulator_warning_covers_both_of_its_consequences(self):
        # `via litellm` + a gateway-only model + a reachable OpenRouter model + an absent
        # simulator makes both the downgrade and the gateway-only risk true at once. One fact
        # gets one warning; two would read as the bot repeating itself.
        entries = [self.GATEWAY_ONLY, "x-ai/grok-4.5"]  # no user simulator
        text = f"<@{BOT}> integrate grok 4.5 and {self.GATEWAY_ONLY} via litellm"
        resolutions = self._resolutions(text, gateway_entries=entries)
        plan = poller.route_plan(
            resolutions,
            self._availability(resolutions, entries),
            mr.parse_route(text),
            simulator=gateway_catalog.lookup(gateway_catalog.USER_SIMULATOR_SLUG, entries),
        )
        assert len(plan.warnings) == 1
        warning = plan.warnings[0]
        assert gateway_catalog.USER_SIMULATOR_SLUG in warning
        assert "the models OpenRouter carries run over" in warning
        assert "infra-dirty in the simulator" in warning
        # ... and the routes still say what each model actually does.
        assert plan.routes == {
            "x-ai/grok-4.5": gateway_catalog.ROUTE_OPENROUTER,
            self.GATEWAY_ONLY: gateway_catalog.ROUTE_GATEWAY,
        }

    def test_an_unreadable_gateway_is_not_blamed_on_the_simulator(self):
        # "unknown" still refuses the route, but naming one model would send someone checking a
        # name that is fine: there is no readable catalog at all.
        text = f"<@{BOT}> integrate grok 4.5 via litellm"
        resolutions = self._resolutions(text)
        plan = poller.route_plan(
            resolutions,
            self._availability(resolutions, None),
            mr.parse_route(text),
            simulator=gateway_catalog.lookup(gateway_catalog.USER_SIMULATOR_SLUG, None),
        )
        assert len(plan.warnings) == 1
        assert "every model above" in plan.warnings[0]
        assert gateway_catalog.USER_SIMULATOR_SLUG not in plan.warnings[0]

    def test_unresolved_phrases_never_reach_the_plan(self):
        resolutions = self._resolutions(f"<@{BOT}> integrate nope/nothing-9 and grok 4.5")
        plan = poller.route_plan(resolutions, self._availability(resolutions, None), None)
        assert plan.routes == {"x-ai/grok-4.5": gateway_catalog.ROUTE_OPENROUTER}
