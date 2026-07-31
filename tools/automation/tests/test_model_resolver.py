"""Unit tests for the deterministic model-slug resolver (Slack-triggered integration).

Two load-bearing properties:
  * VERSION discipline - a requested version must match a slug's version DIGIT-KEY (digits,
    dots dropped), so "Grok 4.5" (45) never resolves to grok-4 (4) / grok-4.3 (43) / 4.53 (453),
    yet the dotless arena config style ("deepseek_v32_exp", "glm_51", "GPT5.6") still matches the
    dotted slug ("v3.2", "glm-5.1", "gpt-5.6").
  * boundary splitting - glued names ("GPT5.6"), underscores ("minimax_m3"), and full slugs all
    tokenize the same way.

The catalog is a fixture (no network); fetch_openrouter_catalog is exercised separately. Opaque
internal shortnames ("hy3" = Hunyuan) are unreachable by string match and resolve only via a
curated alias map.
"""

from __future__ import annotations

import automation.model_resolver as model_resolver
import pytest
from automation import gateway_catalog

pytestmark = pytest.mark.unit

resolve = model_resolver.resolve
parse_command = model_resolver.parse_command

# Sibling versions + variants + the real slug shapes for the models under test.
CATALOG = [
    "anthropic/claude-fable-5",
    "anthropic/claude-sonnet-5",
    "anthropic/claude-opus-4.8",
    "x-ai/grok-4",
    "x-ai/grok-4.1",
    "x-ai/grok-4.3",
    "x-ai/grok-4.5",
    "x-ai/grok-code-fast-1",
    "openai/gpt-5.5",
    "openai/gpt-5.6-luna",
    "openai/gpt-5.6-luna-pro",
    "openai/gpt-5.6-terra",
    "openai/gpt-5.6-terra-pro",
    "openai/gpt-5.6-sol",
    "openai/gpt-5.6-sol-pro",
    "openai/gpt-oss-120b",
    "deepseek/deepseek-v3.2-exp",
    "deepseek/deepseek-v4-flash",
    "minimax/minimax-m3",
    "minimax/minimax-m2.7",
    "z-ai/glm-5.1",
    "z-ai/glm-5.2",
    "moonshotai/kimi-k2.7-code",
    "mistralai/mistral-medium-3.5",
    "nvidia/nemotron-3-super",
    "nvidia/nemotron-3-ultra-550b-a55b",
    "google/gemma-4-31b",
    "tencent/hunyuan-3",
    "tencent/hunyuan-3-preview",
]

GPT_56 = tuple(sorted(s for s in CATALOG if s.startswith("openai/gpt-5.6-")))


# The user's real request list: free-text / config-shortname / slug -> the one right slug.
RESOLVE_CASES = [
    ("deepseek_v4_flash", "deepseek/deepseek-v4-flash"),
    ("deepseek_v32_exp", "deepseek/deepseek-v3.2-exp"),  # dotless "v32" -> "v3.2"
    ("anthropic/claude-fable-5", "anthropic/claude-fable-5"),  # explicit slug
    ("minimax_m3", "minimax/minimax-m3"),
    ("z-ai/glm-5.2", "z-ai/glm-5.2"),  # explicit slug
    ("glm_51", "z-ai/glm-5.1"),  # dotless "51" -> "5.1", not 5.2
    ("gemma_4_31b", "google/gemma-4-31b"),
    ("gpt_oss_120b", "openai/gpt-oss-120b"),
    ("mistral_medium_35", "mistralai/mistral-medium-3.5"),  # dotless "35" -> "3.5"
    ("nemotron_3_super", "nvidia/nemotron-3-super"),
    ("nvidia/nemotron-3-ultra-550b-a55b", "nvidia/nemotron-3-ultra-550b-a55b"),  # explicit slug
    ("moonshotai/kimi-k2.7-code", "moonshotai/kimi-k2.7-code"),  # explicit slug
    ("minimax_m2.7", "minimax/minimax-m2.7"),
    ("Sonnet 5", "anthropic/claude-sonnet-5"),
    ("grok 4.5", "x-ai/grok-4.5"),
    ("GPT5.6 sol", "openai/gpt-5.6-sol"),  # glued "GPT5.6" splits to gpt + 5.6
    ("GPT5.6 terra", "openai/gpt-5.6-terra"),
]


@pytest.mark.parametrize("query,slug", RESOLVE_CASES, ids=[c[0] for c in RESOLVE_CASES])
def test_resolves_to_slug(query, slug):
    r = resolve(query, CATALOG)
    assert r.status == "resolved", f"{query!r} -> {r}"
    assert r.slug == slug


class TestVersionDiscipline:
    def test_lower_version_does_not_match_higher(self):
        assert resolve("Grok 4", CATALOG).slug == "x-ai/grok-4"  # never grok-4.1/4.3/4.5

    def test_each_point_release_is_distinct(self):
        assert resolve("grok 4.1", CATALOG).slug == "x-ai/grok-4.1"
        assert resolve("grok 4.3", CATALOG).slug == "x-ai/grok-4.3"

    def test_unknown_version_is_none_not_nearest(self):
        assert resolve("grok 4.53", CATALOG).status == "none"  # 453 must not snap to 45
        assert resolve("grok 4.4", CATALOG).status == "none"

    def test_gpt_5_5_and_5_6_do_not_bleed(self):
        assert resolve("gpt 5.5", CATALOG).slug == "openai/gpt-5.5"
        assert "openai/gpt-5.5" not in resolve("GPT 5.6", CATALOG).candidates

    def test_dotless_config_version_still_pins(self):
        # glm_51 must be 5.1, not 5.2; deepseek v32 must be v3.2, not v4.
        assert resolve("glm_51", CATALOG).slug == "z-ai/glm-5.1"
        assert resolve("deepseek_v32_exp", CATALOG).slug == "deepseek/deepseek-v3.2-exp"


class TestAmbiguityAndVariants:
    def test_family_and_version_without_variant_is_ambiguous(self):
        r = resolve("GPT 5.6", CATALOG)
        assert r.status == "ambiguous"
        assert r.candidates == GPT_56  # all six sol/terra/luna x normal/pro

    def test_exact_variant_beats_pro_superset(self):
        assert resolve("gpt 5.6 sol", CATALOG).slug == "openai/gpt-5.6-sol"
        assert resolve("gpt 5.6 sol pro", CATALOG).slug == "openai/gpt-5.6-sol-pro"

    def test_bare_family_is_ambiguous_across_versions(self):
        r = resolve("grok", CATALOG)
        assert r.status == "ambiguous"
        assert set(r.candidates) == {s for s in CATALOG if s.startswith("x-ai/grok")}


class TestUnreachableWithoutHelp:
    def test_opaque_shortname_needs_alias(self):
        # "hy3" has no string overlap with "hunyuan"; only a curated alias reaches it.
        assert resolve("hy3", CATALOG).status == "none"
        aliases = {"hy3": "tencent/hunyuan-3", "hy3_preview": "tencent/hunyuan-3-preview"}
        assert resolve("hy3", CATALOG, aliases).slug == "tencent/hunyuan-3"
        assert resolve("hy3_preview", CATALOG, aliases).slug == "tencent/hunyuan-3-preview"

    def test_cohere_a_plus_not_on_openrouter(self):
        # Cohere A+ is Azure-only. Even with other cohere models on OpenRouter, "a+" must NOT
        # collapse to "a" and false-match command-a; the "+" is preserved as its own token, so
        # the honest answer stays "none" (not on OpenRouter) rather than a wrong resolve.
        with_cohere = CATALOG + ["cohere/command-a", "cohere/command-r"]
        assert resolve("cohere a+", with_cohere).status == "none"
        # Sanity: the catalog IS live for cohere - a plain "command a" resolves.
        assert resolve("cohere command a", with_cohere).slug == "cohere/command-a"

    def test_stray_annotation_blocks_confident_match(self):
        # A trailing note ("RERUN") is an unknown token; the poller's grammar must strip it.
        assert resolve("minimax_m2.7", CATALOG).slug == "minimax/minimax-m2.7"
        assert resolve("minimax_m2.7 RERUN", CATALOG).status == "none"


class TestParseCommand:
    def test_extracts_phrases_after_integrate(self):
        assert parse_command("<@U0AB1948PC0> integrate Grok 4.5 and GPT 5.6") == [
            "Grok 4.5",
            "GPT 5.6",
        ]

    def test_multiple_connectors(self):
        assert parse_command("please integrate grok 4.5, GPT5.6 sol & sonnet 5") == [
            "grok 4.5",
            "GPT5.6 sol",
            "sonnet 5",
        ]

    def test_not_an_integrate_request(self):
        assert parse_command("<@U0AB1948PC0> what's the weather") == []


class TestFormatReply:
    def test_reply_pings_requester_and_lists_outcomes(self):
        resolutions = model_resolver.resolve_all("<@U1> integrate Grok 4.5 and GPT 5.6", CATALOG)
        text = model_resolver.format_resolution_reply("U1", resolutions)
        assert text.startswith("<@U1>")
        assert "`x-ai/grok-4.5`" in text  # the resolved one
        assert "is ambiguous (6 matches)" in text  # the GPT 5.6 one
        for slug in GPT_56:
            assert f"`{slug}`" in text


# --- the live incident -----------------------------------------------------------------------
#
# Request: "@arena-automation Integrate azure_ai/cohere-command-a-plus-05-2026". Two independent
# defects hit the same message, and each on its own made it unanswerable:
#   1. `\bplus\b` split the phrase INSIDE the slug (a hyphen is a regex word boundary), so the
#      resolver was handed 'azure_ai/cohere-command-a-' and '-05-2026';
#   2. only the OpenRouter catalog was searched, so a model served solely by the deployment's
#      gateway could not resolve however it was spelled.

#: A gateway route id that OpenRouter does not carry - the shape at the heart of the incident.
GATEWAY_ONLY = "azure_ai/cohere-command-a-plus-05-2026"

#: A realistic gateway catalog: the gateway-only route, a passthrough wildcard, an OpenRouter slug
#: under a route prefix, and a plain duplicate of an OpenRouter slug.
GATEWAY_ENTRIES = [
    GATEWAY_ONLY,
    "x-ai/*",
    "openrouter/x-ai/grok-4.5",
    "anthropic/claude-sonnet-5",
]


class TestConnectorInsideAModelName:
    """A connector word must be a word BETWEEN phrases, never a syllable inside one."""

    def test_plus_inside_a_slug_is_not_a_connector(self):
        assert parse_command(f"<@U1> Integrate {GATEWAY_ONLY}") == [GATEWAY_ONLY]

    def test_trailing_plus_is_kept_not_dropped(self):
        # The worse half of the bug: no error, just a silently DIFFERENT model name, which could
        # then match something real.
        assert parse_command("<@U1> integrate cohere/command-a-plus") == ["cohere/command-a-plus"]

    def test_and_inside_a_slug_is_not_a_connector(self):
        assert parse_command("<@U1> integrate vendor/command-and-conquer-2") == [
            "vendor/command-and-conquer-2"
        ]

    @pytest.mark.parametrize(
        "text",
        [
            "<@U1> integrate grok 4.5 and gpt 5.6 sol",
            "<@U1> integrate grok 4.5 plus gpt 5.6 sol",
            "<@U1> integrate grok 4.5, gpt 5.6 sol",
            "<@U1> integrate grok 4.5,gpt 5.6 sol",
            "<@U1> integrate grok 4.5 & gpt 5.6 sol",
            # Punctuation carrying a word connector: how English writes a list, and the form a
            # whitespace-only word rule silently swallows into the model phrase, since the comma
            # has already consumed the space to the left of "and".
            "<@U1> integrate grok 4.5, and gpt 5.6 sol",
            "<@U1> integrate grok 4.5, plus gpt 5.6 sol",
            "<@U1> integrate grok 4.5 & and gpt 5.6 sol",
        ],
    )
    def test_real_connectors_still_split(self, text):
        assert parse_command(text) == ["grok 4.5", "gpt 5.6 sol"]

    def test_an_oxford_comma_list_keeps_every_phrase(self):
        assert parse_command("<@U1> integrate grok 4.5, gpt 5.6 sol, and sonnet 5") == [
            "grok 4.5",
            "gpt 5.6 sol",
            "sonnet 5",
        ]

    @pytest.mark.parametrize("tail", ["and", "and ", "plus", ","])
    def test_a_dangling_connector_is_not_a_model_phrase(self, tail):
        assert parse_command(f"<@U1> integrate grok 4.5 {tail}") == ["grok 4.5"]


class TestGatewayFallback:
    """OpenRouter first, the gateway only for what OpenRouter cannot match at all."""

    def test_gateway_only_model_resolves(self):
        r = resolve(GATEWAY_ONLY, CATALOG, gateway_entries=GATEWAY_ENTRIES)
        assert (r.status, r.slug, r.source) == (
            "resolved",
            GATEWAY_ONLY,
            model_resolver.SOURCE_GATEWAY,
        )

    def test_a_loose_phrase_reaches_a_gateway_route_by_token_match(self):
        # Resolution only: through a real request this spelling is two phrases, because ` plus `
        # between words IS a connector. See TestConnectorInsideAModelName for that half.
        r = resolve("cohere command a 05 2026", CATALOG, gateway_entries=GATEWAY_ENTRIES)
        assert (r.slug, r.source) == (GATEWAY_ONLY, model_resolver.SOURCE_GATEWAY)

    def test_a_space_separated_plus_is_still_a_connector_in_a_request(self):
        # The honest end-to-end outcome for the spaced spelling: two phrases, one of which is a
        # bare version and resolves to nothing. Typing the slug is what works.
        assert parse_command("<@U1> integrate cohere command a plus 05 2026") == [
            "cohere command a",
            "05 2026",
        ]

    def test_only_real_model_names_become_candidates(self):
        # A gateway catalog is a routing table: a wildcard is not a model, and an OpenRouter slug
        # (bare or under a route prefix) is not a NEW model - keeping either would let a phrase
        # resolve to a second name for a model that already resolved, or to a passthrough.
        assert model_resolver._gateway_candidates(GATEWAY_ENTRIES, set(CATALOG)) == [GATEWAY_ONLY]

    def test_an_openrouter_hit_is_never_disturbed(self):
        r = resolve("sonnet 5", CATALOG, gateway_entries=GATEWAY_ENTRIES)
        assert (r.slug, r.source) == (
            "anthropic/claude-sonnet-5",
            model_resolver.SOURCE_OPENROUTER,
        )

    def test_openrouter_ambiguity_does_not_fall_through(self):
        # Falling through on "ambiguous" would turn a clarify reply into a wrong resolution.
        assert resolve("GPT 5.6", CATALOG, gateway_entries=GATEWAY_ENTRIES).status == "ambiguous"

    @pytest.mark.parametrize("entries", [None, [], ["x-ai/*"]])
    def test_without_a_usable_gateway_the_answer_is_unchanged(self, entries):
        assert resolve(GATEWAY_ONLY, CATALOG, gateway_entries=entries).status == "none"

    @pytest.mark.parametrize(
        "entry",
        [
            "claude sonnet 5 (azure)",  # a LiteLLM model_name alias: spaces and parentheses
            "vendor/model:free",  # a variant suffix the shell must never receive
            "vendor/model;rm -rf",  # a shell metacharacter
        ],
    )
    def test_an_unusable_route_id_is_not_offered_as_a_candidate(self, entry):
        # A candidate outside the safe charset cannot be integrated, and offering it back as
        # "re-request with this exact slug" is a clarification nobody can satisfy: the same
        # request would resolve to the same unusable id every time.
        assert model_resolver._gateway_candidates([entry], set(CATALOG)) == []

    def test_an_alias_to_a_gateway_route_is_routed_there(self):
        r = resolve(
            "cohere-a-plus",
            CATALOG,
            aliases={"cohere-a-plus": GATEWAY_ONLY},
            gateway_entries=GATEWAY_ENTRIES,
        )
        assert (r.slug, r.source) == (GATEWAY_ONLY, model_resolver.SOURCE_GATEWAY)


class TestRouteFor:
    def test_gateway_only_pins_the_gateway(self):
        r = resolve(GATEWAY_ONLY, CATALOG, gateway_entries=GATEWAY_ENTRIES)
        assert model_resolver.route_for(r) == gateway_catalog.ROUTE_GATEWAY
        # ... even against an explicit "via openrouter", which cannot be honoured at all.
        assert (
            model_resolver.route_for(r, gateway_catalog.ROUTE_OPENROUTER)
            == gateway_catalog.ROUTE_GATEWAY
        )

    def test_an_alias_off_openrouter_pins_the_gateway_even_when_it_is_unreadable(self):
        # Membership of the OPENROUTER catalog decides the route: a slug OpenRouter does not
        # carry cannot run there whatever the gateway says, so an unreadable gateway must not
        # send it down the OpenRouter path.
        r = resolve(
            "cohere-a-plus",
            CATALOG,
            aliases={"cohere-a-plus": GATEWAY_ONLY},
            gateway_entries=None,
        )
        assert (r.slug, r.source) == (GATEWAY_ONLY, model_resolver.SOURCE_GATEWAY)
        assert model_resolver.route_for(r) == gateway_catalog.ROUTE_GATEWAY

    def test_openrouter_model_keeps_the_calibrated_default(self):
        r = resolve("sonnet 5", CATALOG, gateway_entries=GATEWAY_ENTRIES)
        assert model_resolver.route_for(r) == gateway_catalog.DEFAULT_ROUTE
        assert (
            model_resolver.route_for(r, gateway_catalog.ROUTE_GATEWAY)
            == gateway_catalog.ROUTE_GATEWAY
        )


class TestFailureNamesWhatWasSearched:
    def test_gateway_unreadable_says_so(self):
        resolutions = model_resolver.resolve_all("<@U1> integrate nope/nothing-9", CATALOG)
        text = model_resolver.format_resolution_reply("U1", resolutions, gateway_searched=False)
        assert "no matching model on OpenRouter" in text
        assert "no gateway catalog was readable" in text

    def test_gateway_searched_names_both(self):
        resolutions = model_resolver.resolve_all(
            "<@U1> integrate nope/nothing-9", CATALOG, gateway_entries=GATEWAY_ENTRIES
        )
        text = model_resolver.format_resolution_reply("U1", resolutions, gateway_searched=True)
        assert "no matching model on OpenRouter or the gateway" in text
        assert "no gateway catalog was readable" not in text

    def test_gateway_only_resolution_says_where_it_runs(self):
        resolutions = model_resolver.resolve_all(
            f"<@U1> Integrate {GATEWAY_ONLY}", CATALOG, gateway_entries=GATEWAY_ENTRIES
        )
        text = model_resolver.format_resolution_reply(
            "U1", resolutions, gateway_searched=True, requested_route=None
        )
        assert f"`{GATEWAY_ONLY}`" in text
        assert f"via *{gateway_catalog.ROUTE_GATEWAY}*" in text
        # Deliberately a claim about the NAME: a gateway route can be the same model under
        # another vendor prefix, so "not on OpenRouter" would not always be true.
        assert "no OpenRouter slug matched this name" in text

    def test_the_live_request_resolves_end_to_end(self):
        resolutions = model_resolver.resolve_all(
            f"<@U0AB1948PC0> Integrate {GATEWAY_ONLY}", CATALOG, gateway_entries=GATEWAY_ENTRIES
        )
        assert [(r.status, r.slug) for r in resolutions] == [("resolved", GATEWAY_ONLY)]
