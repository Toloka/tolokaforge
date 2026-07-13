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
