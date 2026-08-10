"""``{name, params}`` slot value shape — bare-name compat + nested-key validation.

Locks the invariant that every ``_POLICY_REGISTRIES`` slot dispatches
uniformly:

* bare ``name`` still resolves (one-release compat window),
* ``{name, params: {}}`` resolves identically to bare ``name``,
* ``{name, params: {kwarg: value}}`` reaches the constructor,
* unknown nested keys fail loud with ``difflib.get_close_matches``
  suggestions,
* a dict without a ``name`` key raises rather than silently no-op-ing.

Also guards Blocker rule 3 (kill the ``if reasoning_name == 'gemini'``
model-name conditional) by asserting that Gemini's
``drop_placeholder_signature`` reaches the codec via ordinary params flow.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.core.llm.params_policy import GenerationParams
from tolokaforge.core.llm.presets import (
    _POLICY_REGISTRIES,
    _load_overlay_file,
    _normalize_slot_value,
    build_capabilities,
)
from tolokaforge.core.llm.reasoning_codec import GeminiReasoningCodec

pytestmark = pytest.mark.unit


class TestNormalizeSlotValue:
    def test_bare_name_returns_empty_params(self) -> None:
        name, params = _normalize_slot_value("openai", slot="content_policy", where="w")
        assert name == "openai"
        assert params == {}

    def test_dict_with_name_returns_params(self) -> None:
        name, params = _normalize_slot_value(
            {"name": "gemini", "params": {"drop_placeholder_signature": False}},
            slot="reasoning_codec",
            where="w",
        )
        assert name == "gemini"
        assert params == {"drop_placeholder_signature": False}

    def test_dict_with_name_and_missing_params_returns_empty(self) -> None:
        name, params = _normalize_slot_value({"name": "gemini"}, slot="reasoning_codec", where="w")
        assert name == "gemini"
        assert params == {}

    def test_dict_missing_name_raises(self) -> None:
        with pytest.raises(ValueError, match=r"missing the required 'name' key"):
            _normalize_slot_value(
                {"params": {"drop_placeholder_signature": True}},
                slot="reasoning_codec",
                where="preset foo",
            )

    def test_non_dict_non_str_raises(self) -> None:
        with pytest.raises(ValueError, match=r"expected a bare name string or"):
            _normalize_slot_value(["gemini"], slot="reasoning_codec", where="preset foo")

    def test_dict_with_non_mapping_params_raises(self) -> None:
        with pytest.raises(ValueError, match=r"params.*expected a mapping"):
            _normalize_slot_value(
                {"name": "gemini", "params": "not-a-dict"},
                slot="reasoning_codec",
                where="preset foo",
            )


class TestOverlaySlotValueShape:
    """The overlay validator accepts both slot value shapes symmetrically."""

    @pytest.mark.parametrize("slot", sorted(_POLICY_REGISTRIES))
    def test_bare_name_still_accepted(self, slot: str, tmp_path: Path) -> None:
        registry = _POLICY_REGISTRIES[slot]
        first_name = sorted(registry)[0]
        path = tmp_path / "bare.yaml"
        path.write_text(f"presets:\n  legacy:\n    match: ['x/*']\n    {slot}: {first_name}\n")
        _load_overlay_file(str(path))

    @pytest.mark.parametrize("slot", sorted(_POLICY_REGISTRIES))
    def test_name_params_empty_accepted(self, slot: str, tmp_path: Path) -> None:
        registry = _POLICY_REGISTRIES[slot]
        first_name = sorted(registry)[0]
        path = tmp_path / "shape.yaml"
        path.write_text(
            f"presets:\n  extended:\n    match: ['x/*']\n"
            f"    {slot}:\n      name: {first_name}\n      params: {{}}\n"
        )
        _load_overlay_file(str(path))

    def test_reasoning_codec_gemini_with_params(self, tmp_path: Path) -> None:
        path = tmp_path / "gemini_params.yaml"
        path.write_text(
            "presets:\n"
            "  gem:\n"
            "    match: ['x/*']\n"
            "    reasoning_codec:\n"
            "      name: gemini\n"
            "      params:\n"
            "        drop_placeholder_signature: false\n"
        )
        _load_overlay_file(str(path))

    def test_reasoning_codec_gemini_typo_in_nested_params_fails_with_hint(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "gemini_typo.yaml"
        # ``drop_placeholder_signaure`` (missing 't'); difflib should suggest the
        # correct spelling.
        path.write_text(
            "presets:\n"
            "  gem:\n"
            "    match: ['x/*']\n"
            "    reasoning_codec:\n"
            "      name: gemini\n"
            "      params:\n"
            "        drop_placeholder_signaure: false\n"
        )
        with pytest.raises(ValueError) as excinfo:
            _load_overlay_file(str(path))
        msg = str(excinfo.value)
        assert "drop_placeholder_signaure" in msg
        assert "did you mean" in msg
        assert "drop_placeholder_signature" in msg

    def test_slot_missing_name_key_fails_loud(self, tmp_path: Path) -> None:
        path = tmp_path / "no_name.yaml"
        path.write_text(
            "presets:\n"
            "  half:\n"
            "    match: ['x/*']\n"
            "    reasoning_codec:\n"
            "      params:\n"
            "        drop_placeholder_signature: false\n"
        )
        with pytest.raises(ValueError, match=r"missing the required 'name' key"):
            _load_overlay_file(str(path))

    def test_unknown_policy_name_in_new_shape_fails(self, tmp_path: Path) -> None:
        path = tmp_path / "bogus.yaml"
        path.write_text(
            "presets:\n"
            "  wrong:\n"
            "    match: ['x/*']\n"
            "    response_policy:\n"
            "      name: definitely_not_registered\n"
            "      params: {}\n"
        )
        with pytest.raises(
            ValueError, match=r"unknown response_policy 'definitely_not_registered'"
        ):
            _load_overlay_file(str(path))


class TestParamsPolicyMutex:
    """MUTEX: top-level ``params:`` and slot-nested ``params_policy: {...}``
    must not coexist. Silently merging would produce hard-to-debug drift
    between the fingerprint and the actual constructor kwargs."""

    def test_both_present_raises_naming_both_keys(self, tmp_path: Path) -> None:
        path = tmp_path / "mutex.yaml"
        path.write_text(
            "presets:\n"
            "  conflict:\n"
            "    match: ['x/*']\n"
            "    params:\n"
            "      fixed_temperature: 0.0\n"
            "    params_policy:\n"
            "      name: generation_params\n"
            "      params:\n"
            "        supports_seed: false\n"
        )
        with pytest.raises(ValueError) as excinfo:
            _load_overlay_file(str(path))
        msg = str(excinfo.value)
        assert "'params'" in msg
        assert "'params_policy'" in msg
        assert "mutually exclusive" in msg or "conflicting" in msg

    def test_top_level_params_only_accepted(self, tmp_path: Path) -> None:
        # Every shipped preset uses this shape; MUTEX must not fire when only
        # one source is populated.
        path = tmp_path / "legacy.yaml"
        path.write_text(
            "presets:\n  legacy:\n    match: ['x/*']\n    params:\n      fixed_temperature: 0.0\n"
        )
        _load_overlay_file(str(path))

    def test_slot_shape_only_accepted(self, tmp_path: Path) -> None:
        path = tmp_path / "new_shape.yaml"
        path.write_text(
            "presets:\n"
            "  modern:\n"
            "    match: ['x/*']\n"
            "    params_policy:\n"
            "      name: generation_params\n"
            "      params:\n"
            "        supports_seed: false\n"
        )
        _load_overlay_file(str(path))


class TestBuildCapabilitiesDispatch:
    """``build_capabilities`` must route both slot shapes to the same class,
    with construction kwargs threaded through.
    """

    def test_gemini_codec_gets_drop_placeholder_signature_via_override(
        self,
    ) -> None:
        # The Gemini model-name conditional at ``presets.py:~643`` is DELETED
        # in Stage 2 — the params flow is the only path. This asserts the
        # override still reaches the codec.
        caps_default = build_capabilities("google/gemini-3.1-pro-preview", provider="openrouter")
        assert isinstance(caps_default.reasoning_codec, GeminiReasoningCodec)
        assert caps_default.reasoning_codec._drop_placeholder_signature is True

        caps_disabled = build_capabilities(
            "google/gemini-3.1-pro-preview",
            provider="openrouter",
            overrides={"gemini_drop_placeholder_signature": False},
        )
        assert isinstance(caps_disabled.reasoning_codec, GeminiReasoningCodec)
        assert caps_disabled.reasoning_codec._drop_placeholder_signature is False

    def test_generation_params_reads_top_level_params_shorthand(self) -> None:
        caps = build_capabilities("anthropic/claude-opus-4.7-preview", provider="anthropic")
        assert isinstance(caps.params_policy, GenerationParams)
        # anthropic_claude_4_7 preset carries ``params.supports_seed: false``
        assert caps.params_policy._supports_seed is False


class TestNoModelNameConditionalInBuildCapabilities:
    """Blocker rule 3 grep-guard: no ``reasoning_name == 'gemini'`` /
    ``provider == '...'`` / ``model_id == '...'`` conditional inside
    ``presets.py``. Only registry lookups, no dispatch on identifier
    strings.
    """

    def test_presets_py_has_no_model_name_conditional(self) -> None:
        from tolokaforge.core.llm import presets as presets_module

        source = Path(presets_module.__file__).read_text()
        # These substrings are exactly the shapes Blocker rule 3 bans:
        # a runtime dispatch decision keyed on a model or provider identifier.
        # ``_normalize_slot_value``'s ``type(value) is str`` checks route on
        # the SHAPE of the config value, not on any model name — no match.
        banned = (
            'reasoning_name == "gemini"',
            "reasoning_name == 'gemini'",
            'provider == "',
            "provider == '",
            'model_id == "',
            "model_id == '",
            'model_name == "',
            "model_name == '",
        )
        for needle in banned:
            assert needle not in source, (
                f"presets.py contains a model/provider-name conditional {needle!r} "
                f"— AGENTS.md Blocker rule 3 forbids dispatch on identifier strings. "
                f"Route the branch through {{name, params}} slot shape instead."
            )
