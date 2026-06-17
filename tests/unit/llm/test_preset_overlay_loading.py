"""Overlay loading: merge with bundled, precedence, default/provider merge.

ADR 0002 introduces an operator-pointed preset overlay. These tests pin:

- a freshly-installed overlay's presets are reachable via the public lookup
  helpers (``resolve_effective_preset``, ``build_capabilities``);
- bundled presets remain reachable;
- overlay entries are *prepended* to iteration order so first-match-wins gives
  the overlay priority when ``match:`` patterns overlap;
- ``default:`` and ``providers:`` blocks merge shallowly, with nested
  ``params`` merging deeply;
- ``set_overlay_path`` invalidates the cached merged registry, so swapping
  overlays mid-process is visible.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.llm.presets import (
    _load_presets,
    _match_preset,
    build_capabilities,
    resolve_effective_preset,
    resolve_policy_names,
    set_overlay_path,
)

pytestmark = pytest.mark.unit


class TestOverlayInstall:
    def test_no_overlay_matches_bundled_data(self, overlay_isolation) -> None:
        # Baseline: with no overlay installed, _load_presets returns the
        # bundled registry verbatim.
        set_overlay_path(None)
        data = _load_presets()
        assert "presets" in data and len(data["presets"]) > 0
        # A bundled preset still resolves.
        assert resolve_effective_preset("anthropic/claude-opus-4.8") == "anthropic_claude_4_8"

    def test_overlay_preset_resolves_via_public_helpers(
        self, write_overlay, overlay_isolation
    ) -> None:
        path = write_overlay(
            {
                "presets": {
                    "custom_lab_model": {
                        "match": ["customlab/*"],
                        "response_policy": "array_dict_map",
                        "reasoning_codec": "openai",
                    }
                }
            }
        )
        set_overlay_path(path)
        assert resolve_effective_preset("customlab/foo-1") == "custom_lab_model"
        caps = build_capabilities("customlab/foo-1")
        names = resolve_policy_names(caps)
        assert names["response_policy"] == "array_dict_map"
        assert names["reasoning_codec"] == "openai"

    def test_overlay_does_not_remove_bundled_presets(
        self, write_overlay, overlay_isolation
    ) -> None:
        path = write_overlay(
            {"presets": {"custom_x": {"match": ["customx/*"], "response_policy": "standard"}}}
        )
        set_overlay_path(path)
        # Bundled preset still routes.
        assert resolve_effective_preset("anthropic/claude-opus-4.8") == "anthropic_claude_4_8"


class TestOverlayPrecedence:
    def test_overlay_preset_wins_first_match(self, write_overlay, overlay_isolation) -> None:
        # The bundled file matches "anthropic/claude-opus-4.8" via the
        # anthropic_claude_4_8 preset. An overlay declaring an earlier match
        # for the same model name must win.
        path = write_overlay(
            {
                "presets": {
                    "anthropic_lab_override": {
                        "match": ["anthropic/claude-opus-4.8*"],
                        "response_policy": "json_coerce",
                    }
                }
            }
        )
        set_overlay_path(path)
        assert resolve_effective_preset("anthropic/claude-opus-4.8") == "anthropic_lab_override"

    def test_overlay_replaces_bundled_entry_with_same_name(
        self, write_overlay, overlay_isolation, caplog
    ) -> None:
        # Replacing a bundled preset name with overlay content emits an INFO
        # log so the replacement is visible.
        import logging

        path = write_overlay(
            {
                "presets": {
                    "anthropic_claude_4_8": {
                        "match": ["anthropic/claude-opus-4.8*"],
                        "response_policy": "json_coerce",
                    }
                }
            }
        )
        with caplog.at_level(logging.INFO, logger="tolokaforge.core.llm.presets"):
            set_overlay_path(path)
            _load_presets()  # force merge
        assert any(
            "shadows bundled preset" in r.message for r in caplog.records
        ), f"expected shadow log; got: {[r.message for r in caplog.records]}"
        # And the overlay version wins for resolution.
        merged = _match_preset("anthropic/claude-opus-4.8", "")
        assert merged["response_policy"] == "json_coerce"


class TestOverlayDefaultAndProviders:
    def test_default_block_overlay_wins_with_params_deep_merge(
        self, write_overlay, overlay_isolation
    ) -> None:
        path = write_overlay(
            {
                "default": {
                    "schema_sanitizer": "strict",
                    "params": {"supports_seed": False},
                }
            }
        )
        set_overlay_path(path)
        merged = _load_presets()
        assert merged["default"]["schema_sanitizer"] == "strict"
        # Bundled default 'params' (if any) merges with the overlay's params
        # — the overlay key supports_seed must be present.
        assert merged["default"]["params"]["supports_seed"] is False

    def test_provider_block_overlay_wins(self, write_overlay, overlay_isolation) -> None:
        path = write_overlay(
            {
                "providers": {
                    "openrouter": {
                        "params": {"supports_seed": False},
                    }
                }
            }
        )
        set_overlay_path(path)
        merged = _load_presets()
        # Provider 'openrouter' params should now carry supports_seed=False
        assert merged["providers"]["openrouter"]["params"]["supports_seed"] is False


class TestOverlayCacheInvalidation:
    def test_swapping_overlay_reflects_in_subsequent_lookup(
        self, write_overlay, overlay_isolation
    ) -> None:
        path_a = write_overlay(
            {"presets": {"a": {"match": ["custom-a/*"], "response_policy": "json_coerce"}}},
            name="a.yaml",
        )
        path_b = write_overlay(
            {"presets": {"b": {"match": ["custom-b/*"], "response_policy": "json_coerce"}}},
            name="b.yaml",
        )
        set_overlay_path(path_a)
        assert resolve_effective_preset("custom-a/x") == "a"
        assert resolve_effective_preset("custom-b/x") == "default"

        # Swap overlays — second lookup must see the new file, not a stale cache.
        set_overlay_path(path_b)
        assert resolve_effective_preset("custom-a/x") == "default"
        assert resolve_effective_preset("custom-b/x") == "b"

        # Clearing the overlay restores bundled-only behaviour.
        set_overlay_path(None)
        assert resolve_effective_preset("custom-a/x") == "default"
        assert resolve_effective_preset("custom-b/x") == "default"
