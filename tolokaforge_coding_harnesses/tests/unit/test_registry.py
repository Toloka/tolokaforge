"""What the shipped registry data must satisfy to load at all.

``data/harnesses.yaml`` and ``data/registry_meta.yaml`` are the source of truth
for the six shipped harnesses, and both are validated at import — so a typo is a
load-time error naming the file and the key rather than a trial-time failure.
"""

import pytest

pytestmark = pytest.mark.unit


class TestHarnessRegistryMeta:
    """``data/registry_meta.yaml`` — the registry-wide catalogs (OpenRouter
    vendor namespaces + the provider env-var allow-list). Data, so adding a
    namespace or a key is a YAML edit; fail-loud, so malformed data is refused
    at import rather than at trial time."""

    def test_shipped_file_populates_the_module_globals(self):
        from tolokaforge_coding_harnesses._registry import _VENDOR_NAMESPACE_PREFIXES

        from tolokaforge_coding_harnesses import PROVIDER_ENV_KEYS, SHIPPED_REGISTRY_META_FILE

        assert SHIPPED_REGISTRY_META_FILE.is_file()
        assert _VENDOR_NAMESPACE_PREFIXES  # non-empty
        assert PROVIDER_ENV_KEYS  # non-empty
        # Existing shipped harnesses' provider_env keys all belong to the
        # allow-list.
        from tolokaforge_coding_harnesses import HARNESSES

        for spec in HARNESSES.values():
            for key in spec.provider_env:
                assert key in PROVIDER_ENV_KEYS, (
                    f"harness ships provider_env key {key!r} outside PROVIDER_ENV_KEYS — "
                    "either add the key to registry_meta.yaml or the harness is broken."
                )

    def test_missing_file_names_the_path(self, tmp_path):
        from tolokaforge_coding_harnesses._registry import _load_registry_meta

        with pytest.raises(FileNotFoundError, match="registry_meta.yaml"):
            _load_registry_meta(tmp_path / "does_not_exist.yaml")

    def test_non_mapping_yaml_is_refused(self, tmp_path):
        from tolokaforge_coding_harnesses._registry import _load_registry_meta

        target = tmp_path / "registry_meta.yaml"
        target.write_text("- one\n- two\n")  # a list at the top level
        with pytest.raises(ValueError, match="must be a YAML mapping"):
            _load_registry_meta(target)

    def test_unknown_top_level_key_is_refused(self, tmp_path):
        from tolokaforge_coding_harnesses._registry import _load_registry_meta

        target = tmp_path / "registry_meta.yaml"
        target.write_text(
            "openrouter_vendor_namespaces: [foo/]\nprovider_env_keys: [A_KEY]\nextra_key: hi\n"
        )
        with pytest.raises(ValueError):
            _load_registry_meta(target)

    def test_empty_namespaces_list_is_refused(self, tmp_path):
        from tolokaforge_coding_harnesses._registry import _load_registry_meta

        target = tmp_path / "registry_meta.yaml"
        target.write_text("openrouter_vendor_namespaces: []\nprovider_env_keys: [A_KEY]\n")
        with pytest.raises(ValueError, match="openrouter_vendor_namespaces must not be empty"):
            _load_registry_meta(target)

    def test_empty_env_keys_list_is_refused(self, tmp_path):
        from tolokaforge_coding_harnesses._registry import _load_registry_meta

        target = tmp_path / "registry_meta.yaml"
        target.write_text("openrouter_vendor_namespaces: [foo/]\nprovider_env_keys: []\n")
        with pytest.raises(ValueError, match="provider_env_keys must not be empty"):
            _load_registry_meta(target)

    def test_duplicate_namespace_entry_is_refused(self, tmp_path):
        from tolokaforge_coding_harnesses._registry import _load_registry_meta

        target = tmp_path / "registry_meta.yaml"
        target.write_text(
            "openrouter_vendor_namespaces: [foo/, foo/]\nprovider_env_keys: [A_KEY]\n"
        )
        with pytest.raises(ValueError, match="duplicates"):
            _load_registry_meta(target)

    def test_blank_entry_is_refused(self, tmp_path):
        from tolokaforge_coding_harnesses._registry import _load_registry_meta

        target = tmp_path / "registry_meta.yaml"
        target.write_text('openrouter_vendor_namespaces: ["foo/"]\nprovider_env_keys: [" "]\n')
        with pytest.raises(ValueError, match="non-blank"):
            _load_registry_meta(target)


class TestHarnessSpecRegistry:
    """The shipped registry is packaged YAML data, loaded at import."""

    def test_shipped_file_declares_the_supported_harnesses(self):
        from tolokaforge_coding_harnesses import (
            HARNESSES,
            SHIPPED_REGISTRY_FILE,
            load_harness_registry,
        )

        assert SHIPPED_REGISTRY_FILE.is_file()
        assert list(HARNESSES) == [
            "claude-code",
            "codex",
            "gemini-cli",
            "kimi-code",
            "opencode",
            "grok-build",
        ]
        assert load_harness_registry(SHIPPED_REGISTRY_FILE) == HARNESSES

    def test_shipped_entries_install_methods(self):
        """Five entries install via npm; Grok Build installs via curl-bash
        (the first non-npm entry, exercising install-harness.sh dispatch)."""
        from tolokaforge_coding_harnesses import HARNESSES

        assert {
            name: (spec.install_method, spec.install_source) for name, spec in HARNESSES.items()
        } == {
            "claude-code": ("npm", "@anthropic-ai/claude-code"),
            "codex": ("npm", "@openai/codex"),
            "gemini-cli": ("npm", "@google/gemini-cli"),
            "kimi-code": ("npm", "@moonshot-ai/kimi-code"),
            "opencode": ("npm", "opencode-ai"),
            "grok-build": ("curl-bash", "https://x.ai/cli/install.sh"),
        }

    @pytest.mark.parametrize(
        ("document", "expected"),
        [
            pytest.param(
                "harnesses:\n"
                "  claude-code:\n"
                "    install_source: p\n"
                "    version: '1'\n"
                "    argv_prefix: [claude]\n"
                "    argv_suffix: []\n"
                "    typo_field: nope\n",
                "typo_field",
                id="unknown-field",
            ),
            pytest.param(
                "harnesses:\n  claude-code:\n    version: '1'\n    argv_prefix: [claude]\n",
                "install_source",
                id="missing-required-field",
            ),
            pytest.param(
                "harnesses:\n  claude-code:\n    install_source: p\n"
                "    version: '1'\n    argv_prefix: [claude]\n    argv_suffix: []\n"
                "defaults:\n  version: '2'\n",
                "defaults",
                id="unknown-top-level-key",
            ),
            pytest.param(
                "harnesses:\n  grok:\n    install_method: curl-bash\n"
                "    install_source: not-a-url\n"
                "    version: '1'\n    argv_prefix: [grok]\n    argv_suffix: []\n",
                "http:// or https:// URL",
                id="downloaded-source-is-not-a-url",
            ),
            pytest.param(
                "harnesses:\n  grok:\n    install_method: pip\n"
                "    install_source: 'https://harness.invalid/grok.tar.gz'\n"
                "    version: '1'\n    argv_prefix: [grok]\n    argv_suffix: []\n",
                "not a bare package name",
                id="named-source-is-a-url",
            ),
            pytest.param("harnesses: {}\n", "non-empty", id="no-harness-declared"),
            pytest.param("- claude-code\n", "must be a YAML mapping", id="not-a-mapping"),
            pytest.param("harnesses: [\n", "not valid YAML", id="malformed-yaml"),
        ],
    )
    def test_malformed_registry_is_refused(self, tmp_path, document, expected):
        """A registry typo has to name the file and the offending key: it is an
        operator's config error, and silently dropping the entry would surface
        much later as an unknown-harness or missing-flag trial failure."""
        from tolokaforge_coding_harnesses import load_harness_registry

        path = tmp_path / "harnesses.yaml"
        path.write_text(document)
        with pytest.raises(ValueError, match=expected) as excinfo:
            load_harness_registry(path)
        assert str(path) in str(excinfo.value)

    def test_missing_file_is_refused(self, tmp_path):
        from tolokaforge_coding_harnesses import load_harness_registry

        missing = tmp_path / "absent.yaml"
        with pytest.raises(ValueError, match="does not exist"):
            load_harness_registry(missing)
