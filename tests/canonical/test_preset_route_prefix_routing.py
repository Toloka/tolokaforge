"""Canonical test — bundled presets claim their models under any route prefix.

A gateway or router reaches a model as ``<route>/<vendor>/<model>`` or
``<route>/<model>``. The same weights get the same preset whichever way
they are named: for every bundled ``match`` glob G that does not start with
``*``, ``<route>/G`` resolves to the preset that owns G, through an anchored
``*/G`` sibling or a leading-``*`` glob of the same preset that already
covers it. A leading-``*`` glob is anchored on a ``/`` boundary unless it is
one of the anywhere-matching globs shipped today. A model-specific preset
declared ahead of its family preset also claims the vendor-dropped form of
its model names, unless its reasoning codec is tied to the OpenRouter route.

Presets that route by ``match_provider`` are exempt: their name prefix is
a litellm provider namespace, not a vendor segment.

The structural rows are parametrized ``preset:glob``, so a glob that breaks
an invariant fails naming itself. The population rows run the same
invariants over every slug in the bundled ``pricing.json``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import yaml

from tolokaforge.core.llm import build_capabilities
from tolokaforge.core.llm.presets import (
    resolve_effective_preset,
    resolve_policy_names,
    set_overlay_path,
)
from tolokaforge.core.model_data import bundled_presets_path, bundled_pricing_path

pytestmark = pytest.mark.canonical

_ROUTES = (
    ("openrouter", "openrouter"),
    ("litellm_proxy", "openai"),
    ("self-hosted", "openai"),
)


def _bundled_presets() -> dict[str, dict[str, Any]]:
    return yaml.safe_load(bundled_presets_path().read_text())["presets"]


_OPENROUTER_TIED_REPLAY_PRESETS = frozenset(
    {"xai_grok_4_6", "z_ai_glm_5_3", "deepseek_v4_flash_0731_resolve"}
)
"""``openai_summary_replay`` presets measured on the OpenRouter route alone. The codec
rebuilds OpenRouter's ``reasoning_details`` envelope, and a vendor-dropped name is never
an OpenRouter slug, so these presets do not claim one."""

_ANYWHERE_GLOBS = frozenset(
    {
        "cohere_command_a_plus_05_2026:*cohere-command-a-plus-05-2026*",
        "cohere_command_a_plus:*cohere-command-a-plus*",
        "anthropic_claude_4_8:*claude-opus-4.8*",
        "anthropic_claude_4_8:*claude-sonnet-4.8*",
        "anthropic_claude_4_7:*claude-opus-4.7*",
        "anthropic_claude_4_7:*claude-sonnet-4.7*",
        "anthropic_claude_opus_5:*claude-opus-5*",
        "anthropic_claude_fable_5_1:*claude-fable-5.1*",
        "anthropic:*claude*",
        "openai_gpt5:*gpt-5*",
        "openai_gpt6:*gpt-6*",
        "qwen3_8_max_unsigned_reasoning_replay:*qwen3.8-max",
        "openrouter_dict_stringify_recovery:*mimo-v2*",
        "openrouter_dict_stringify_recovery:*deepseek-v4*",
        "openrouter_dict_stringify_recovery:*glm-5*",
        "openrouter_dict_stringify_recovery:*hy3-preview*",
        "openrouter_dict_stringify_recovery:*nvidia-nemotron-*",
        "openrouter_dict_stringify_recovery:*nvidia.nemotron-*",
        "moonshot_kimi_k2:*kimi-k2*",
        "moonshot_kimi_k3:*kimi-k3*",
        "deepseek_v32:*deepseek-v3.2-exp*",
        "aws_nova_openrouter:*amazon/nova*",
        "gemini:*gemini-3*",
        "gemini:*gemini-2.5*",
        "gpt_oss:*gpt-oss*",
        "gemma:*gemma-*",
        "minimax:*minimax-m3*",
    }
)
"""``preset:glob`` for the leading-``*`` globs that match anywhere in a name. Frozen:
a new leading-``*`` glob starts with ``*/``."""


def _match_globs() -> list[tuple[str, str]]:
    return [
        (preset_name, glob)
        for preset_name, preset in _bundled_presets().items()
        for glob in preset.get("match", [])
    ]


def _leading_star_globs() -> list[tuple[str, str]]:
    return [(preset, glob) for preset, glob in _match_globs() if glob.startswith("*")]


def _anchored_globs() -> list[tuple[str, str]]:
    return [
        (preset_name, glob)
        for preset_name, preset in _bundled_presets().items()
        if not preset.get("match_provider")
        for glob in preset.get("match", [])
        if not glob.startswith("*")
    ]


def _vendor_anchored_globs(*, openrouter_tied: bool) -> list[tuple[str, str]]:
    return [
        (preset_name, glob)
        for preset_name, glob in _anchored_globs()
        if "/" in glob
        and glob.rsplit("/", 1)[-1] != "*"
        and (preset_name in _OPENROUTER_TIED_REPLAY_PRESETS) is openrouter_tied
    ]


def _provider_routed_presets() -> frozenset[str]:
    return frozenset(
        name for name, preset in _bundled_presets().items() if preset.get("match_provider")
    )


def _priced_slugs() -> list[str]:
    return list(json.loads(bundled_pricing_path().read_text())["models"])


@pytest.fixture(autouse=True)
def _bundled_table_only() -> None:
    set_overlay_path(None)


@pytest.mark.parametrize(
    ("preset_name", "glob"),
    _anchored_globs(),
    ids=[f"{preset}:{glob}" for preset, glob in _anchored_globs()],
)
def test_anchored_glob_is_claimed_under_every_route_prefix(preset_name: str, glob: str) -> None:
    assert resolve_effective_preset(glob) == preset_name, (
        f"control: {glob!r} no longer resolves to its own preset {preset_name!r}; "
        "an earlier preset shadows it"
    )
    routed = {route: resolve_effective_preset(f"{route}/{glob}") for route, _ in _ROUTES}
    assert routed == dict.fromkeys(routed, preset_name), (
        f"{preset_name!r} does not claim {glob!r} under a route prefix: {routed}. "
        f"Add the anchored sibling '*/{glob}' to its match list."
    )


@pytest.mark.parametrize(
    ("preset_name", "glob"),
    _leading_star_globs(),
    ids=[f"{preset}:{glob}" for preset, glob in _leading_star_globs()],
)
def test_a_leading_star_glob_is_anchored_on_a_route_boundary(preset_name: str, glob: str) -> None:
    assert glob.startswith("*/") or f"{preset_name}:{glob}" in _ANYWHERE_GLOBS, (
        f"{preset_name!r} glob {glob!r} matches anywhere in a name, so it also claims "
        f"foreign models that merely contain {glob.strip('*')!r}. Anchor it as "
        f"'*/{glob.lstrip('*')}'."
    )


def test_the_anywhere_glob_allowlist_names_only_shipped_globs() -> None:
    shipped = {f"{preset}:{glob}" for preset, glob in _leading_star_globs()}
    assert shipped >= _ANYWHERE_GLOBS, f"stale entries: {sorted(_ANYWHERE_GLOBS - shipped)}"


@pytest.mark.parametrize(
    ("preset_name", "glob"),
    _vendor_anchored_globs(openrouter_tied=False),
    ids=[f"{preset}:{glob}" for preset, glob in _vendor_anchored_globs(openrouter_tied=False)],
)
def test_vendor_dropped_name_never_lands_in_another_preset(preset_name: str, glob: str) -> None:
    model = glob.rsplit("/", 1)[-1]
    bare = resolve_effective_preset(model)
    routed = {route: resolve_effective_preset(f"{route}/{model}") for route, _ in _ROUTES}
    assert bare in (preset_name, "default") and routed == dict.fromkeys(routed, bare), (
        f"{glob!r} belongs to {preset_name!r}, but its vendor-dropped form {model!r} "
        f"resolves to {bare!r} bare and to {routed} routed. Add '{model}' and "
        f"'*/{model}' to {preset_name!r}'s match list."
    )


@pytest.mark.parametrize(
    ("preset_name", "glob"),
    _vendor_anchored_globs(openrouter_tied=True),
    ids=[f"{preset}:{glob}" for preset, glob in _vendor_anchored_globs(openrouter_tied=True)],
)
def test_an_openrouter_tied_replay_codec_skips_the_vendor_dropped_name(
    preset_name: str, glob: str
) -> None:
    model = glob.rsplit("/", 1)[-1]
    names = [model, *(f"{route}/{model}" for route, _ in _ROUTES)]
    claimed = [name for name in names if resolve_effective_preset(name) == preset_name]
    assert not claimed, (
        f"{preset_name!r} rebuilds OpenRouter's reasoning_details envelope "
        f"(openai_summary_replay), but claims the vendor-dropped names {claimed}, "
        "which are never OpenRouter slugs"
    )


def test_route_prefix_does_not_change_the_preset_of_a_priced_model() -> None:
    exempt = _provider_routed_presets()
    violations = []
    for slug in _priced_slugs():
        own = resolve_effective_preset(slug, "openrouter")
        if own in exempt:
            continue
        for route, provider in _ROUTES:
            routed = resolve_effective_preset(f"{route}/{slug}", provider)
            if routed != own:
                violations.append((slug, route, own, routed))
    assert not violations, f"{len(violations)} (slug, route, own, routed) violations: {violations}"


def test_vendor_dropped_route_matches_the_bare_model_name() -> None:
    exempt = _provider_routed_presets()
    violations = []
    for slug in _priced_slugs():
        model = slug.rsplit("/", 1)[-1]
        bare = resolve_effective_preset(model, "openai")
        if bare in exempt:
            continue
        routed = resolve_effective_preset(f"self-hosted/{model}", "openai")
        if routed != bare:
            violations.append((slug, bare, routed))
    assert not violations, f"{len(violations)} (slug, bare, routed) violations: {violations}"


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        ("openai", "self-hosted/qwen3.6-35b-a3b", "qwen"),
        ("openrouter", "openrouter/qwen/qwen3-coder-plus", "qwen"),
        ("openrouter", "openrouter/x-ai/grok-4.3", "xai_grok"),
    ],
)
def test_gateway_named_model_resolves_to_its_family_preset(
    provider: str, model: str, expected: str
) -> None:
    assert resolve_effective_preset(model, provider) == expected


@pytest.mark.parametrize(
    "model",
    [
        "self-hosted/tolokaforge-canary-unclaimed",
        "self-hosted/foo-qwen3-bar",
        "self-hosted/foo-grok-bar",
    ],
)
def test_route_prefix_globs_do_not_claim_an_unknown_model(model: str) -> None:
    assert resolve_effective_preset(model, "openai") == "default"


def test_gateway_route_gets_the_policy_axes_of_the_same_weights() -> None:
    gateway = resolve_policy_names(build_capabilities("self-hosted/qwen3.6-35b-a3b", "openai"))
    vendor = resolve_policy_names(build_capabilities("qwen/qwen3.6-35b-a3b", "openai"))
    assert gateway == vendor


class TestNemotronLine:
    """The shared preset claims NVIDIA's Nemotron line, not Llama-based fine-tunes."""

    @pytest.mark.parametrize(
        ("provider", "model"),
        [
            ("openrouter", "nvidia/nemotron-3-super-120b-a12b"),
            ("openrouter", "openrouter/nvidia/nemotron-3-super-120b-a12b"),
            ("openai", "nemotron-3-super-120b-a12b"),
            ("openai", "self-hosted/nemotron-3.5-lightning"),
            ("openai", "self-hosted/nvidia/NVIDIA-Nemotron-3-Super-120B-A12B"),
            ("fireworks_ai", "fireworks_ai/accounts/fireworks/models/nvidia-nemotron-nano-9b-v2"),
            ("bedrock", "bedrock/nvidia.nemotron-super-3-120b"),
            ("azure_ai", "azure_ai/FW-Nemotron-Lightning-3.5-30B-A3B"),
        ],
    )
    def test_nemotron_model_routes_to_the_shared_preset(self, provider: str, model: str) -> None:
        assert resolve_effective_preset(model, provider) == "openrouter_dict_stringify_recovery"

    @pytest.mark.parametrize(
        "model",
        [
            "nvidia/llama-3.1-nemotron-70b-instruct",
            "nvidia/llama-3.3-nemotron-super-49b-v1.5",
            "deepinfra/nvidia/Llama-3.3-Nemotron-Super-49B-v1.5",
        ],
    )
    def test_llama_nemotron_fine_tune_falls_through_to_default(self, model: str) -> None:
        assert resolve_effective_preset(model, "openrouter") == "default"
