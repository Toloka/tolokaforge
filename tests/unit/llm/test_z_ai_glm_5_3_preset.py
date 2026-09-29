"""Preset routing — ``z_ai_glm_5_3`` (the routing half, PR #1277).

The entry is declared BEFORE the shared ``openrouter_dict_stringify_recovery``
preset, whose ``z-ai/glm-5*`` glob would otherwise claim the route. That order
is the whole reason ``z-ai/glm-5.3`` gets ``openai_summary_replay`` instead of
the plain ``openai`` codec, and nothing else pins it: a maintainer sorting
``presets:`` alphabetically, or broadening the shared glob, would silently
revert the codec and reintroduce the 0/15 ``UNSIGNED_THINKING_REPLAY`` failure
while every offline suite stayed green. These tests lock four invariants:

1. the measured shapes (bare route + gateway-prefixed) resolve to the overlay;
2. unmeasured point releases / variants do NOT — the globs carry no trailing
   ``*`` (``fnmatch``'s ``*`` also matches ``.`` and ``-``);
3. the 5.1 / 5.2 / 5 siblings stay on the shared preset (a future
   ``glm-5*`` broadening of the overlay would poach them);
4. the overlay is a full copy of the shared recovery stack — ``_match_preset``
   does first-match-wins with no fallback merge, so the re-declared
   ``passthrough`` / ``json_coerce`` / ``dict_map_hints`` lines are
   load-bearing, not duplicates.

The codec that this overlay was carved out to supply is now on the shared
preset as well: a 2026-09-30 probe of every preset found the plain ``openai``
codec discarding readable reasoning on deepseek-v4-pro, mimo-v2.5-pro and both
nemotron-3 sizes, the same defect 5.3 hit as an 0/15 ``UNSIGNED_THINKING_REPLAY``
failure. The narrow fix here was right and too narrow.
"""

from __future__ import annotations

import pytest
from tolokaforge_models.policies.deepseek import OpenAISummaryReplayReasoningCodec

from tolokaforge.core.llm import (
    DictMapHints,
    JsonCoerceResponse,
    OpenAIContent,
    PassthroughSchema,
    build_capabilities,
)
from tolokaforge.core.llm.presets import resolve_effective_preset

pytestmark = pytest.mark.unit

PRESET = "z_ai_glm_5_3"
FAMILY_PRESET = "openrouter_dict_stringify_recovery"


@pytest.mark.parametrize(
    "model",
    ["z-ai/glm-5.3", "openrouter/z-ai/glm-5.3", "litellm_proxy/z-ai/glm-5.3"],
)
def test_measured_shapes_route_to_the_overlay(model: str) -> None:
    """First-match-wins: the overlay is declared before the family glob."""
    assert resolve_effective_preset(model, "openrouter") == PRESET


@pytest.mark.parametrize(
    "model",
    [
        "z-ai/glm-5.3-turbo",
        "z-ai/glm-5.3-fast",
        "z-ai/glm-5.30",
        "z-ai/glm-5.3.1",
        "z-ai/glm-5.3:exacto",
        "openrouter/z-ai/glm-5.3-fast",
    ],
)
def test_unmeasured_point_releases_are_not_claimed(model: str) -> None:
    """No trailing ``*`` on the overlay globs: a release nobody reprobed must
    fall through to the shared preset, never inherit this codec silently."""
    assert resolve_effective_preset(model, "openrouter") == FAMILY_PRESET


@pytest.mark.parametrize(
    "model",
    ["z-ai/glm-5.1", "z-ai/glm-5.2", "z-ai/glm-5", "z-ai/glm-5-turbo", "z-ai/glm-5v-turbo"],
)
def test_the_rest_of_the_family_stays_on_the_shared_preset(model: str) -> None:
    """The siblings keep routing to the shared preset, whatever it carries.

    What that preset carries has since changed: a fleet-wide probe found the
    plain ``openai`` codec discarding readable reasoning on four of its members,
    so the shared stack now replays too. This assertion is about *routing* —
    the overlay must not poach siblings — and no longer about the codec, which
    the family and the overlay now agree on.
    """
    assert resolve_effective_preset(model, "openrouter") == FAMILY_PRESET
    assert isinstance(
        build_capabilities(model, "openrouter").reasoning_codec,
        OpenAISummaryReplayReasoningCodec,
    )


def test_overlay_is_the_shared_stack_plus_the_codec_swap() -> None:
    """The overlay is a full copy of the shared stack, on every axis.

    It was carved out for the codec alone, when the shared preset still dropped
    reasoning on replay; the shared preset has since been fixed the same way, so
    the two now agree there. The overlay still earns its place: it pins 5.3's
    route explicitly, and ``_match_preset`` is first-match-wins with no fallback
    merge, so every line it restates is load-bearing.
    """
    glm53 = build_capabilities("z-ai/glm-5.3", "openrouter")
    glm52 = build_capabilities("z-ai/glm-5.2", "openrouter")
    for caps in (glm53, glm52):
        assert isinstance(caps.schema_sanitizer, PassthroughSchema)
        assert isinstance(caps.response_policy, JsonCoerceResponse)
        assert isinstance(caps.prompt_policy, DictMapHints)
        assert isinstance(caps.content_policy, OpenAIContent)
        assert isinstance(caps.reasoning_codec, OpenAISummaryReplayReasoningCodec)
