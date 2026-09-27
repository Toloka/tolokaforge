"""Unit guard for the Moonshot Kimi presets' match-glob routing.

The two Kimi lines carry different accommodations and must not be
confused for one another:

* ``moonshot_kimi_k2`` — the K2 line (K2.6, K2.7-code, …). Carries the
  ``openrouter_dict_stringify_recovery`` policy quartet verbatim plus
  ``default_max_turns: 90`` and ``empty_retry_count: 1``.
* ``moonshot_kimi_k3`` — the K3 line. Carries the empty-assistant filler
  and the Moonshot routing pin, which K2 deliberately does not.

Matching is first-match-wins over whole entries, so a preset that gains
an overlapping glob silently takes the other line's knobs with it. The
K2 globs moved out of ``openrouter_dict_stringify_recovery`` when
``moonshot_kimi_k2`` was introduced; these tests hold that split in
place, and hold the five models still in the generic preset to the
policies they had.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm.presets import _match_preset, resolve_effective_preset

pytestmark = pytest.mark.unit


_K2_MODELS = (
    "moonshotai/kimi-k2.7-code",
    "moonshotai/kimi-k2.6",
    "openrouter/moonshotai/kimi-k2.7-code",
)

_K3_MODELS = (
    "moonshotai/kimi-k3",
    "openrouter/moonshotai/kimi-k3",
)

#: Everything else that shares the policy quartet with K2. These stayed in
#: ``openrouter_dict_stringify_recovery`` when the K2 globs moved out.
_STRINGIFY_SIBLINGS = (
    "xiaomi/mimo-v2-pro",
    "deepseek/deepseek-v4",
    "z-ai/glm-5.1",
    "tencent/hy3-preview",
    "nvidia/nemotron-3-super",
)

#: What ``moonshot_kimi_k2`` adds on top of the shared recipe. Everything
#: else it declares must still match the generic preset key for key.
_K2_ADDITIONS = frozenset({"default_max_turns", "empty_retry_count"})


@pytest.mark.parametrize("model", _K2_MODELS)
def test_k2_routes_to_its_own_preset(model: str) -> None:
    name = resolve_effective_preset(model, "openrouter")
    assert name == "moonshot_kimi_k2", (
        f"Expected {model!r} to route to 'moonshot_kimi_k2', got {name!r}. "
        "A preset with an overlapping glob was added ahead of it, or the K2 "
        "globs were returned to openrouter_dict_stringify_recovery."
    )


@pytest.mark.parametrize("model", _K2_MODELS)
def test_k2_keeps_the_wire_shape_recovery_it_had(model: str) -> None:
    """The turn budget is additive — K2 must not lose stringify recovery.

    ``moonshot_kimi_k2`` restates the shared recipe rather than inheriting
    it, because only one preset applies per model. A restatement that
    drifts silently changes how every K2 tool call is decoded.

    Compared over the union of both presets' keys rather than a hardcoded
    list, so a knob added to the generic preset later fails here instead
    of quietly passing K2 by.
    """
    k2 = _match_preset(model, "openrouter")
    generic = _match_preset("xiaomi/mimo-v2-pro", "openrouter")

    shared_keys = (set(k2) | set(generic)) - _K2_ADDITIONS
    drifted = {
        key: (generic.get(key), k2.get(key))
        for key in sorted(shared_keys)
        if k2.get(key) != generic.get(key)
    }
    assert not drifted, (
        "moonshot_kimi_k2 diverges from openrouter_dict_stringify_recovery on "
        f"{sorted(drifted)} (generic, k2): {drifted}. Either restate the new "
        "value on the K2 preset or add the key to _K2_ADDITIONS if the "
        "divergence is deliberate."
    )


@pytest.mark.parametrize("model", _K2_MODELS)
def test_k2_carries_the_turn_and_empty_retry_budgets(model: str) -> None:
    """The two knobs this preset exists for.

    K2 emits the finest-grained turns in the P0 set, so it converges in
    more steps than the 50-turn engine default allows.
    """
    cfg = _match_preset(model, "openrouter")
    assert cfg.get("default_max_turns") == 90
    assert cfg.get("empty_retry_count") == 1


@pytest.mark.parametrize("model", _K2_MODELS)
def test_k2_is_not_pinned_to_moonshot_direct(model: str) -> None:
    """The pin stays off K2 until #1286's filler repro lands.

    K3 pairs its Moonshot pin with an empty-assistant filler because
    Moonshot direct rejects empty-content assistant turns. Pinning K2
    without that filler would trade a routing concern for a 400 class.
    """
    assert _match_preset(model, "openrouter").get("openrouter_defaults") is None


@pytest.mark.parametrize("model", _K3_MODELS)
def test_k3_is_unaffected(model: str) -> None:
    """K2 slugs carry a dot before the version, so the globs stay disjoint."""
    assert resolve_effective_preset(model, "openrouter") == "moonshot_kimi_k3"
    cfg = _match_preset(model, "openrouter")
    assert cfg.get("openrouter_defaults") is not None, "K3 kept its routing pin"


@pytest.mark.parametrize("model", _STRINGIFY_SIBLINGS)
def test_the_generic_preset_siblings_are_unchanged(model: str) -> None:
    """Moving the K2 globs out must not give the rest of the lineup a budget."""
    assert resolve_effective_preset(model, "openrouter") == "openrouter_dict_stringify_recovery"
    cfg = _match_preset(model, "openrouter")
    assert cfg.get("default_max_turns") is None
    assert cfg.get("empty_retry_count") is None


def test_other_vendors_do_not_route_to_a_kimi_preset() -> None:
    for model, expected in (
        ("openai/gpt-5.6-sol", "openai_gpt5"),
        ("anthropic/claude-sonnet-4.6", "anthropic"),
        ("google/gemini-3.1-pro-preview", "gemini_31_pro_preview"),
    ):
        assert resolve_effective_preset(model, "openrouter") == expected
