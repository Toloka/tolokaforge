"""Which name a model answers to on the gateway, and what happens when it does not.

The rules pinned here were each derived from a live failure; see
``docs/LLM_LAYER.md`` § "Speaking to the gateway".
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm.gateway_route import (
    GatewayRouteError,
    resolve_gateway_route,
)

pytestmark = pytest.mark.unit


class TestRouteName:
    def test_the_bare_name_wins_when_the_gateway_serves_the_upstream_itself(self) -> None:
        """A gateway-only model: the engine prefix is not part of its route name."""
        catalog = frozenset({"azure_ai/cohere-command-a-plus-05-2026"})
        assert (
            resolve_gateway_route("openrouter/azure_ai/cohere-command-a-plus-05-2026", catalog)
            == "azure_ai/cohere-command-a-plus-05-2026"
        )

    def test_the_full_string_wins_when_the_gateway_proxies_the_provider(self) -> None:
        """A model reached through the gateway's own OpenRouter route keeps the prefix."""
        catalog = frozenset({"openrouter/anthropic/claude-sonnet-4.6"})
        assert (
            resolve_gateway_route("openrouter/anthropic/claude-sonnet-4.6", catalog)
            == "openrouter/anthropic/claude-sonnet-4.6"
        )

    def test_a_model_the_gateway_does_not_serve_returns_none(self) -> None:
        assert (
            resolve_gateway_route("openrouter/anthropic/claude-opus-4.8", frozenset({"x/y"}))
            is None
        )

    def test_an_unreadable_catalog_disables_routing_rather_than_failing(self) -> None:
        """``None`` is "no information": an unreachable gateway must not fail a run."""
        assert resolve_gateway_route("openrouter/anthropic/claude-sonnet-4.6", None) is None

    def test_a_provider_less_model_string_has_one_candidate(self) -> None:
        assert resolve_gateway_route("solo", frozenset({"solo"})) == "solo"

    def test_an_exact_hit_reports_its_kind(self) -> None:
        route = resolve_gateway_route(
            "openrouter/anthropic/claude-sonnet-4.6",
            frozenset({"openrouter/anthropic/claude-sonnet-4.6"}),
        )
        assert route.kind == "exact"


class TestNamespaceWildcard:
    """``<ns>/*`` routes a ``provider: <ns>`` model - opt-in, exact wins."""

    catalog = frozenset({"openrouter/*", "anthropic/*"})

    def test_off_by_default(self) -> None:
        assert resolve_gateway_route("openrouter/minimax/minimax-m3", self.catalog) is None

    def test_the_models_own_namespace_wildcard_routes_it_untranslated(self) -> None:
        route = resolve_gateway_route(
            "openrouter/minimax/minimax-m3",
            self.catalog,
            trust_namespace_wildcards=True,
        )
        assert route == "openrouter/minimax/minimax-m3"
        assert route.kind == "wildcard"

    def test_a_foreign_namespace_wildcard_is_not_a_route(self) -> None:
        """``anthropic/*`` for an openrouter model is another upstream."""
        assert (
            resolve_gateway_route(
                "openrouter/minimax/minimax-m3",
                frozenset({"anthropic/*"}),
                trust_namespace_wildcards=True,
            )
            is None
        )

    def test_an_exact_entry_wins_over_the_wildcard(self) -> None:
        catalog = frozenset({"openrouter/*", "anthropic/claude-sonnet-4.6"})
        route = resolve_gateway_route(
            "openrouter/anthropic/claude-sonnet-4.6",
            catalog,
            trust_namespace_wildcards=True,
        )
        assert route == "anthropic/claude-sonnet-4.6"
        assert route.kind == "exact"

    def test_a_bare_global_star_is_not_trusted(self) -> None:
        """Only the namespace form carries meaning; ``*`` says nothing."""
        assert (
            resolve_gateway_route(
                "openrouter/minimax/minimax-m3",
                frozenset({"*"}),
                trust_namespace_wildcards=True,
            )
            is None
        )


class TestAmbiguityIsRefused:
    """Two names for one model can be two different upstreams, so guessing is refused."""

    both = frozenset({"openrouter/anthropic/claude-sonnet-4.6", "anthropic/claude-sonnet-4.6"})

    def test_two_matches_without_a_preference_raise(self) -> None:
        with pytest.raises(GatewayRouteError) as excinfo:
            resolve_gateway_route("openrouter/anthropic/claude-sonnet-4.6", self.both)
        message = str(excinfo.value)
        assert "LLM_PROXY_PREFERRED_ROUTE" in message
        for name in self.both:
            assert name in message

    @pytest.mark.parametrize(
        ("preference", "expected"),
        [
            ("openrouter/", "openrouter/anthropic/claude-sonnet-4.6"),
            ("anthropic/", "anthropic/claude-sonnet-4.6"),
        ],
    )
    def test_the_preference_picks_one(self, preference: str, expected: str) -> None:
        assert (
            resolve_gateway_route("openrouter/anthropic/claude-sonnet-4.6", self.both, preference)
            == expected
        )

    def test_a_preference_matching_neither_still_raises(self) -> None:
        """Silently ignoring the preference would pick a serving path nobody asked for."""
        with pytest.raises(GatewayRouteError):
            resolve_gateway_route("openrouter/anthropic/claude-sonnet-4.6", self.both, "gemini/")

    def test_a_preference_is_ignored_when_there_is_nothing_to_disambiguate(self) -> None:
        catalog = frozenset({"azure_ai/cohere-command-a-plus-05-2026"})
        assert (
            resolve_gateway_route(
                "openrouter/azure_ai/cohere-command-a-plus-05-2026", catalog, "gemini/"
            )
            == "azure_ai/cohere-command-a-plus-05-2026"
        )


class TestUnreadableIsNotAbsent:
    """An unreadable catalog must not silently take the run off the gateway.

    Leaving the gateway is the unattributed-spend outcome the transport exists to
    prevent, so a catalog that cannot be read keeps the gateway and the untranslated
    name; only a catalog that *answers* and omits the model sends it direct. The
    client owns that split; this pins the resolver half of it.
    """

    def test_no_catalog_and_no_match_are_both_none_at_this_layer(self) -> None:
        assert resolve_gateway_route("openrouter/x/y", None) is None
        assert resolve_gateway_route("openrouter/x/y", frozenset({"other"})) is None

    def test_an_empty_catalog_is_treated_as_no_information(self) -> None:
        """A gateway that answers with zero models is broken, not authoritative."""
        assert resolve_gateway_route("openrouter/x/y", frozenset()) is None
