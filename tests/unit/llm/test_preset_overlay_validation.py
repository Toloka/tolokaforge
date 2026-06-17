"""Overlay validation — host-boundary guard for the preset registry.

Mirrors the in-tree ``ensure_registered_adapter()`` pattern from
``tolokaforge.adapters`` (see PR #61): validate at the layer where the
registry is authoritative, and fail loudly on every recognised
misconfiguration. The error message must always name the overlay path so the
operator can find the offending file.

Also pins the validator-versus-registry sync invariant: every in-engine
policy registry must be referenced by ``_validate_overlay``. Adding a new
registry without extending the validator becomes a test failure rather than a
silent runtime hole.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from tolokaforge.core.llm.presets import (
    _CACHE_POLICIES,
    _CONTENT_POLICIES,
    _PROMPT_POLICIES,
    _REASONING_CODECS,
    _RESPONSE_POLICIES,
    _SCHEMA_SANITIZERS,
    _load_overlay_file,
    _load_presets,
    _validate_overlay,
    set_overlay_path,
)

pytestmark = pytest.mark.unit


class TestOverlayLoadErrors:
    def test_missing_file_raises_with_path(self, tmp_path: Path) -> None:
        path = str(tmp_path / "does_not_exist.yaml")
        with pytest.raises(FileNotFoundError, match=r"does_not_exist\.yaml"):
            _load_overlay_file(path)

    def test_malformed_yaml_raises_with_path(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("this:\n  is:\n - not\n  - balanced\n: yaml")
        with pytest.raises(ValueError, match=r"bad\.yaml.*failed to parse"):
            _load_overlay_file(str(path))

    def test_top_level_must_be_mapping(self, tmp_path: Path) -> None:
        path = tmp_path / "list.yaml"
        path.write_text("- not\n- a\n- mapping")
        with pytest.raises(ValueError, match=r"must be a YAML mapping"):
            _load_overlay_file(str(path))

    def test_empty_file_is_allowed(self, tmp_path: Path) -> None:
        # An empty overlay file is equivalent to "no presets to add" and
        # should not raise — operators may bootstrap an empty file before
        # populating it.
        path = tmp_path / "empty.yaml"
        path.write_text("")
        data = _load_overlay_file(str(path))
        assert data == {"default": {}, "presets": {}, "providers": {}}

    def test_unknown_top_level_key_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "typo.yaml"
        path.write_text("preests:\n  foo: {}\n")  # 'preests' typo
        with pytest.raises(ValueError, match=r"unknown top-level keys.*preests"):
            _load_overlay_file(str(path))


class TestOverlayPolicyNameValidation:
    @pytest.mark.parametrize(
        "slot,registry_keys",
        [
            ("schema_sanitizer", list(_SCHEMA_SANITIZERS)),
            ("prompt_policy", list(_PROMPT_POLICIES)),
            ("content_policy", list(_CONTENT_POLICIES)),
            ("response_policy", list(_RESPONSE_POLICIES)),
            ("reasoning_codec", list(_REASONING_CODECS)),
            ("cache_policy", list(_CACHE_POLICIES)),
        ],
    )
    def test_unknown_policy_name_in_preset_raises(
        self, slot, registry_keys, tmp_path: Path
    ) -> None:
        path = tmp_path / "bad_policy.yaml"
        bogus = "definitely_not_in_registry_xyz"
        path.write_text(f"presets:\n  bad:\n    match: ['x/*']\n    {slot}: {bogus}\n")
        with pytest.raises(ValueError) as excinfo:
            _load_overlay_file(str(path))
        msg = str(excinfo.value)
        assert "bad_policy.yaml" in msg
        assert slot in msg
        assert bogus in msg
        # The error also lists the available names so the operator can fix it.
        for name in registry_keys:
            assert name in msg

    def test_unknown_policy_in_default_block_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad_default.yaml"
        path.write_text("default:\n  response_policy: not_a_real_policy\n")
        with pytest.raises(ValueError, match=r"unknown response_policy 'not_a_real_policy'"):
            _load_overlay_file(str(path))

    def test_unknown_policy_in_provider_block_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad_provider.yaml"
        path.write_text("providers:\n  openrouter:\n    response_policy: not_a_real_policy\n")
        with pytest.raises(ValueError, match=r"providers\.openrouter.*response_policy"):
            _load_overlay_file(str(path))


class TestOverlayParamsValidation:
    def test_unknown_params_key_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "bad_params.yaml"
        path.write_text(
            "presets:\n  bad:\n    match: ['x/*']\n    params:\n      not_a_real_kwarg: 1\n"
        )
        with pytest.raises(ValueError, match=r"params.*unknown keys.*not_a_real_kwarg"):
            _load_overlay_file(str(path))

    def test_params_must_be_mapping(self, tmp_path: Path) -> None:
        path = tmp_path / "bad_params_type.yaml"
        path.write_text("presets:\n  bad:\n    match: ['x/*']\n    params: not_a_dict\n")
        with pytest.raises(ValueError, match=r"params.*expected a mapping"):
            _load_overlay_file(str(path))


class TestValidatorRegistrySync:
    """Pin the invariant: every in-engine policy registry must be referenced
    by ``_validate_overlay``. Adding a new registry without updating the
    validator → silent overlay-validation gap. Catching it here saves a
    runtime debugging session.
    """

    def test_validator_references_every_registry(self) -> None:
        source = inspect.getsource(_validate_overlay)
        # Each registry constant name must appear in the validator's source.
        for registry_name in [
            "_SCHEMA_SANITIZERS",
            "_PROMPT_POLICIES",
            "_CONTENT_POLICIES",
            "_RESPONSE_POLICIES",
            "_REASONING_CODECS",
            "_CACHE_POLICIES",
        ]:
            assert registry_name in source, (
                f"_validate_overlay must reference {registry_name!r}; if a new "
                f"registry was added, extend the validator to check it."
            )

    def test_validator_references_every_policy_slot_key(self) -> None:
        source = inspect.getsource(_validate_overlay)
        for slot in [
            "schema_sanitizer",
            "prompt_policy",
            "content_policy",
            "response_policy",
            "reasoning_codec",
            "cache_policy",
        ]:
            assert (
                f'"{slot}"' in source or f"'{slot}'" in source
            ), f"_validate_overlay must check the {slot!r} slot."


class TestSetOverlayPathDefersFileRead:
    """``set_overlay_path`` must not read the file — only ``_load_presets`` does.

    This lets the CLI install an overlay path that's known to exist, while
    deferring the file-read (and any validation error) to the first capability
    lookup. Important for tests that swap overlays cheaply.
    """

    def test_set_overlay_path_does_not_raise_on_missing_file(
        self, tmp_path: Path, overlay_isolation
    ) -> None:
        # No read happens here, so no FileNotFoundError yet.
        set_overlay_path(str(tmp_path / "not_there.yaml"))

    def test_load_presets_raises_lazily_on_missing_file(
        self, tmp_path: Path, overlay_isolation
    ) -> None:
        set_overlay_path(str(tmp_path / "not_there.yaml"))
        with pytest.raises(FileNotFoundError, match=r"not_there\.yaml"):
            _load_presets()
