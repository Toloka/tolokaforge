"""Tests for the advisory gateway-availability lookup and the route directive.

Two behaviours carry real risk and are pinned hardest:

* **OpenRouter stays the default.** A request that does not name a route must not
  change serving path, because the leaderboard is calibrated on OpenRouter.
* **A wildcard is weaker evidence than an explicit entry.** Reporting a
  passthrough match as if someone had configured the model would invite a human
  to pick a route that then 404s at eval time.
"""

from __future__ import annotations

import pytest
from automation import gateway_catalog, model_resolver

_CATALOG = [
    "anthropic/claude-sonnet-4-5",
    "azure_ai/cohere-command-a-plus-05-2026",
    "openrouter/*",
    "openrouter/anthropic/claude-opus-4.7",
]


class TestLookup:
    def test_exact_prefixed_entry_wins(self) -> None:
        found = gateway_catalog.lookup("anthropic/claude-opus-4.7", _CATALOG)
        assert found.status == gateway_catalog.STATUS_EXACT
        assert found.route == "openrouter/anthropic/claude-opus-4.7"
        assert found.reachable

    def test_bare_slug_entry_also_counts_as_exact(self) -> None:
        found = gateway_catalog.lookup("azure_ai/cohere-command-a-plus-05-2026", _CATALOG)
        assert found.status == gateway_catalog.STATUS_EXACT
        assert found.route == "azure_ai/cohere-command-a-plus-05-2026"

    def test_wildcard_is_reported_separately_from_exact(self) -> None:
        """A passthrough means 'probably', and the reply must not overstate it."""
        found = gateway_catalog.lookup("x-ai/grok-4.5", _CATALOG)
        assert found.status == gateway_catalog.STATUS_WILDCARD
        assert found.route == "openrouter/x-ai/grok-4.5"
        assert found.reachable
        assert "passthrough" in gateway_catalog.describe(found)

    def test_absent_when_catalog_covers_nothing(self) -> None:
        found = gateway_catalog.lookup("x-ai/grok-4.5", ["anthropic/claude-sonnet-4-5"])
        assert found.status == gateway_catalog.STATUS_ABSENT
        assert not found.reachable
        assert found.route is None

    def test_unfetched_catalog_is_unknown_not_absent(self) -> None:
        """No gateway configured must not read as 'the model is missing'."""
        found = gateway_catalog.lookup("x-ai/grok-4.5", None)
        assert found.status == gateway_catalog.STATUS_UNKNOWN
        assert not found.reachable
        assert gateway_catalog.describe(found) == ""


class TestFetchDegradesQuietly:
    def test_no_base_url_returns_none(self) -> None:
        assert gateway_catalog.fetch_gateway_catalog(None, "sk-x") is None
        assert gateway_catalog.fetch_gateway_catalog("   ", "sk-x") is None

    def test_unreachable_gateway_returns_none_rather_than_raising(self) -> None:
        """A notification path must never break the poll."""
        assert (
            gateway_catalog.fetch_gateway_catalog("http://127.0.0.1:9/v1", "sk-x", timeout=1)
            is None
        )


class TestRouteDirective:
    @pytest.mark.parametrize(
        "text",
        [
            "<@U1> integrate Grok 4.5 via litellm",
            "<@U1> integrate Grok 4.5 via LiteLLM proxy",
            "<@U1> integrate Grok 4.5 through the gateway",
            "<@U1> integrate Grok 4.5 using the proxy",
        ],
    )
    def test_gateway_route_is_recognised(self, text: str) -> None:
        assert model_resolver.parse_route(text) == gateway_catalog.ROUTE_GATEWAY

    @pytest.mark.parametrize(
        "text",
        ["<@U1> integrate Grok 4.5 via openrouter", "<@U1> integrate Grok 4.5 via OR"],
    )
    def test_openrouter_route_is_recognised(self, text: str) -> None:
        assert model_resolver.parse_route(text) == gateway_catalog.ROUTE_OPENROUTER

    def test_no_directive_is_none_not_the_default(self) -> None:
        """The caller must distinguish 'chose OpenRouter' from 'did not ask'."""
        assert model_resolver.parse_route("<@U1> integrate Grok 4.5") is None

    def test_default_route_is_openrouter(self) -> None:
        assert gateway_catalog.DEFAULT_ROUTE == gateway_catalog.ROUTE_OPENROUTER

    @pytest.mark.parametrize(
        "text",
        [
            "<@U1> integrate Grok 4.5 via litellm",
            "<@U1> integrate Grok 4.5 via openrouter",
        ],
    )
    def test_directive_is_stripped_from_the_model_phrase(self, text: str) -> None:
        """Otherwise 'Grok 4.5 via litellm' becomes the phrase and resolves to nothing."""
        assert model_resolver.parse_command(text) == ["Grok 4.5"]

    def test_directive_stripped_with_several_models(self) -> None:
        phrases = model_resolver.parse_command(
            "<@U1> integrate Grok 4.5 and GPT 5.6 via the gateway"
        )
        assert phrases == ["Grok 4.5", "GPT 5.6"]

    def test_a_model_named_or_is_not_mistaken_for_a_route(self) -> None:
        """The directive needs a via/through/over/using lead-in, so plain names are safe."""
        assert model_resolver.parse_route("<@U1> integrate openrouter/x-ai/grok-4.5") is None
        assert model_resolver.parse_command("<@U1> integrate openrouter/x-ai/grok-4.5") == [
            "openrouter/x-ai/grok-4.5"
        ]


class TestReply:
    def _resolved(self, slug: str) -> model_resolver.Resolution:
        return model_resolver.Resolution(query=slug, status="resolved", slug=slug)

    def test_reply_names_the_effective_route_and_the_gateway_note(self) -> None:
        slug = "anthropic/claude-opus-4.7"
        reply = model_resolver.format_resolution_reply(
            "U1",
            [self._resolved(slug)],
            {slug: gateway_catalog.lookup(slug, _CATALOG)},
            None,
        )
        assert "via *openrouter*" in reply
        assert "openrouter/anthropic/claude-opus-4.7" in reply
        # Unrequested-but-available => tell the requester how to ask for it.
        assert "via litellm" in reply

    def test_explicit_gateway_request_is_echoed(self) -> None:
        slug = "anthropic/claude-opus-4.7"
        reply = model_resolver.format_resolution_reply(
            "U1",
            [self._resolved(slug)],
            {slug: gateway_catalog.lookup(slug, _CATALOG)},
            gateway_catalog.ROUTE_GATEWAY,
        )
        assert "via *litellm*" in reply
        # No nudge when the requester already chose.
        assert "re-request with" not in reply.lower()

    def test_reply_without_availability_is_unchanged_shape(self) -> None:
        """Back-compat: the pre-existing two-argument call still works."""
        reply = model_resolver.format_resolution_reply("U1", [self._resolved("x-ai/grok-4.5")])
        assert "starting integration as `x-ai/grok-4.5`" in reply
        assert "gateway" not in reply


class TestPlanRows:
    """The plan is the contract with the workflow, so its new fields need pinning."""

    def _row(self, text: str, gateway: list[str] | None) -> dict:
        """Reproduce the poller's per-request plan row without Slack or the network."""
        from automation import poller

        catalog = ["x-ai/grok-4.5"]
        resolutions = model_resolver.resolve_all(text, catalog, poller.ALIASES)
        route = model_resolver.parse_route(text) or gateway_catalog.DEFAULT_ROUTE
        availability = {
            r.slug: gateway_catalog.lookup(r.slug, gateway)
            for r in resolutions
            if r.status == "resolved" and r.slug
        }
        slug = poller.resolved_slugs(resolutions)[0]
        return {
            "slug": slug,
            "requester": "U1",
            "message_ts": "1.0",
            "route": route,
            "gateway": gateway_catalog.as_dict(availability[slug])
            if slug in availability
            else None,
        }

    def test_unstated_route_defaults_to_openrouter(self) -> None:
        row = self._row("<@U1> integrate Grok 4.5", ["openrouter/*"])
        assert row["route"] == "openrouter"

    def test_explicit_gateway_route_reaches_the_plan(self) -> None:
        row = self._row("<@U1> integrate Grok 4.5 via litellm", ["openrouter/*"])
        assert row["route"] == "litellm"

    def test_gateway_verdict_is_carried_for_the_dispatch_log(self) -> None:
        row = self._row("<@U1> integrate Grok 4.5", ["openrouter/x-ai/grok-4.5"])
        assert row["gateway"] == {
            "slug": "x-ai/grok-4.5",
            "status": "exact",
            "route": "openrouter/x-ai/grok-4.5",
        }

    def test_no_gateway_configured_still_produces_a_dispatchable_row(self) -> None:
        row = self._row("<@U1> integrate Grok 4.5", None)
        assert row["route"] == "openrouter"
        assert row["gateway"]["status"] == "unknown"
