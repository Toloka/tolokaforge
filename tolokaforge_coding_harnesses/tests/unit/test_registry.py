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
            pytest.param(
                "harnesses:\n  gemini-cli:\n    install_source: '@google/gemini-cli'\n"
                "    version: '1'\n    argv_prefix: [gemini]\n    argv_suffix: []\n"
                "    container_env:\n"
                "      GOOGLE_GEMINI_BASE_URL: 'https://gateway.invalid/gemini'\n",
                "provider_env",
                id="container-env-key-shadows-the-provider-envelope",
            ),
            pytest.param(
                "harnesses:\n  gemini-cli:\n    install_source: '@google/gemini-cli'\n"
                "    version: '1'\n    argv_prefix: [gemini]\n    argv_suffix: []\n"
                "    container_env:\n      FOO: '${secret:LITELLM_BASE_URL}'\n",
                "docker interpolates",
                id="container-env-value-carries-a-secret-reference",
            ),
            pytest.param(
                "harnesses:\n  gemini-cli:\n    install_source: '@google/gemini-cli'\n"
                "    version: '1'\n    argv_prefix: [gemini]\n    argv_suffix: []\n"
                "    container_env:\n      FOO: '$LITELLM_BASE_URL'\n",
                "docker interpolates",
                id="container-env-value-carries-a-bare-shell-variable",
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


class TestAlternativeGatewayCatalog:
    """``registry_meta.alternative_gateways`` — the closed set of gateways a
    ``gateway_route`` may name. Data this package never reads the values of:
    the catalog's job is to make a route's ``gateway:`` key checkable at load,
    for a consumer that lives in another repo and can check nothing itself."""

    def test_shipped_file_declares_the_litellm_gateway(self):
        from tolokaforge_coding_harnesses import ALTERNATIVE_GATEWAYS, PROVIDER_ENV_KEYS

        gateway = ALTERNATIVE_GATEWAYS["toloka_litellm"]
        assert gateway.base_url_env == "LITELLM_BASE_URL"
        assert gateway.credential_env == "LITELLM_API_KEY"
        assert gateway.supports == ("protocol_translation", "provider_pinning")
        # Both names are already forwardable, so a route reaching this gateway
        # needs no widening of the provider-env allow-list.
        assert {gateway.base_url_env, gateway.credential_env} <= PROVIDER_ENV_KEYS

    def test_the_shipped_catalog_refuses_a_gateway_registered_at_runtime(self):
        """The closed set is what a route's ``gateway:`` key is checked against,
        so an insertion before ``load_harness_registry`` would let any harness
        name a gateway the shipped data never declares."""
        from tolokaforge_coding_harnesses import ALTERNATIVE_GATEWAYS, RuntimeGateway

        with pytest.raises(TypeError):
            ALTERNATIVE_GATEWAYS["injected"] = RuntimeGateway(  # type: ignore[index]
                base_url_env="U", credential_env="K"
            )

    def test_a_write_to_the_loaded_dict_does_not_show_through(self):
        """The proxy is over a copy: over the loaded dict it would forward every
        write made through that dict, which is the same escape hatch one
        attribute away."""
        from tolokaforge_coding_harnesses._registry import _REGISTRY_META

        from tolokaforge_coding_harnesses import ALTERNATIVE_GATEWAYS, RuntimeGateway

        _REGISTRY_META.alternative_gateways["backdoor"] = RuntimeGateway(
            base_url_env="U", credential_env="K"
        )
        try:
            assert "backdoor" not in ALTERNATIVE_GATEWAYS
        finally:
            del _REGISTRY_META.alternative_gateways["backdoor"]

    def test_a_meta_file_declaring_no_gateway_loads(self, tmp_path):
        """The catalog is optional: an operator's own registry_meta shape stays
        valid without it, and a harness declaring no route needs none."""
        from tolokaforge_coding_harnesses._registry import _load_registry_meta

        target = tmp_path / "registry_meta.yaml"
        target.write_text("openrouter_vendor_namespaces: [foo/]\nprovider_env_keys: [A_KEY]\n")
        assert _load_registry_meta(target).alternative_gateways == {}

    _WELL_FORMED = 'base_url_env: "U"\n      credential_env: "K"\n'

    @pytest.mark.parametrize(
        ("name", "gateway", "expected"),
        [
            pytest.param(
                "some_gateway",
                'base_url_env: ""\n      credential_env: "K"\n',
                "base_url_env",
                id="blank-base-url-name",
            ),
            pytest.param(
                "some_gateway",
                'base_url_env: "U"\n      credential_env: " K "\n',
                "credential_env",
                id="padded-credential-name",
            ),
            pytest.param(
                "some_gateway",
                'base_url_env: "U"\n      credential_env: "K"\n'
                '      supports: ["", "provider_pinning"]\n',
                "supports",
                id="blank-capability-tag",
            ),
            pytest.param(
                "some_gateway",
                'base_url_env: "U"\n      credential_env: "K"\n'
                '      supports: ["provider_pinning", "provider_pinning"]\n',
                "duplicates",
                id="repeated-capability-tag",
            ),
            pytest.param(
                '""',
                _WELL_FORMED,
                "alternative_gateways key",
                id="blank-gateway-name",
            ),
            pytest.param(
                '" padded_gateway "',
                _WELL_FORMED,
                "alternative_gateways key",
                id="padded-gateway-name",
            ),
        ],
    )
    def test_malformed_gateway_is_refused_naming_the_field(self, tmp_path, name, gateway, expected):
        """The name a gateway is filed under is checked too: a route naming the
        trimmed value is refused by ``HarnessSpec`` with an accepted-set listing
        the padded one, which reads as a bug in the check rather than the data."""
        from tolokaforge_coding_harnesses._registry import _load_registry_meta

        target = tmp_path / "registry_meta.yaml"
        target.write_text(
            "openrouter_vendor_namespaces: [foo/]\n"
            "provider_env_keys: [A_KEY]\n"
            "alternative_gateways:\n"
            f"  {name}:\n"
            f"      {gateway}"
        )
        with pytest.raises(ValueError, match=expected):
            _load_registry_meta(target)


class TestGatewayRoute:
    """``HarnessSpec.gateway_route`` — the per-harness recipe for reaching a
    catalog gateway.

    Every rule below is checked at load because no in-repo caller ever reads a
    route: the consumer is a second runtime in another repo, so a value this
    package accepts is a value nothing else will question until a trial 404s.
    """

    @staticmethod
    def _registry_document(route: str) -> str:
        return (
            "harnesses:\n"
            "  gemini-cli:\n"
            "    install_source: '@google/gemini-cli'\n"
            "    version: '1'\n"
            "    argv_prefix: [gemini]\n"
            "    argv_suffix: []\n"
            "    gateway_route:\n"
            f"{route}"
        )

    def test_undeclared_gateway_is_refused_naming_the_accepted_set(self, tmp_path):
        """The typo check the cross-repo consumer cannot make for itself. The
        message also has to say the catalog is shipped-only, or an operator
        reading it has no next step."""
        from tolokaforge_coding_harnesses import load_harness_registry

        path = tmp_path / "harnesses.yaml"
        path.write_text(self._registry_document("      gateway: 'nope'\n"))
        with pytest.raises(ValueError) as excinfo:
            load_harness_registry(path)
        message = str(excinfo.value)
        assert "'nope'" in message
        assert "toloka_litellm" in message
        assert "shipped-only" in message
        assert str(path) in message

    def test_a_route_level_error_surfaces_naming_the_file_and_the_harness(self, tmp_path):
        """A route is validated inside the harness entry, so its message has to
        carry the same file + key context every other registry typo does."""
        from tolokaforge_coding_harnesses import load_harness_registry

        path = tmp_path / "harnesses.yaml"
        path.write_text(
            self._registry_document(
                "      gateway: 'toloka_litellm'\n      passthrough_path: 'gemini'\n"
            )
        )
        with pytest.raises(ValueError) as excinfo:
            load_harness_registry(path)
        message = str(excinfo.value)
        assert "passthrough_path" in message
        assert "gemini-cli" in message
        assert str(path) in message

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            pytest.param(
                {"provider_env": {"KIMI_MODEL_NAME": "{model}-pinned"}},
                ["KIMI_MODEL_NAME", "gateway_route.provider_env"],
                id="provider-env-key-outside-the-allow-list",
            ),
            pytest.param(
                {"container_env": {"GOOGLE_GEMINI_BASE_URL": "https://gateway.invalid"}},
                ["GOOGLE_GEMINI_BASE_URL", "gateway_route.provider_env"],
                id="container-env-key-shadows-the-provider-envelope",
            ),
            pytest.param(
                {"container_env": {"CLI_ENDPOINT": "${gateway.base_url}"}},
                ["CLI_ENDPOINT", "literals only"],
                id="container-env-value-carries-a-token",
            ),
            pytest.param(
                {"config_files": {"settings.json": "{}"}},
                ["settings.json", "is relative"],
                id="config-file-path-is-relative",
            ),
            pytest.param(
                {"config_files": {"${HOME}/.gemini/settings.json": '{"model": "{{ model }}"}'}},
                ["settings.json", "literal file content"],
                id="config-file-content-is-a-jinja-template",
            ),
            pytest.param(
                {"config_files": {"/etc/cli.conf": "{% if model %}x{% endif %}"}},
                ["cli.conf", "literal file content"],
                id="config-file-content-is-a-jinja-block",
            ),
            pytest.param(
                {"passthrough_path": "gemini"},
                ["'gemini'", "start with `/`"],
                id="passthrough-path-is-not-rooted",
            ),
            pytest.param(
                {"model_alias_pattern": "kimi-k2-moonshotai-pinned"},
                ["'kimi-k2-moonshotai-pinned'", "{model}", "single gateway alias"],
                id="model-alias-pattern-has-no-model-placeholder",
            ),
        ],
    )
    def test_invalid_route_is_refused_naming_the_offending_key(self, overrides, expected):
        from tolokaforge_coding_harnesses import GatewayRoute

        with pytest.raises(ValueError) as excinfo:
            GatewayRoute(gateway="toloka_litellm", **overrides)
        message = str(excinfo.value)
        for fragment in expected:
            assert fragment in message, f"{fragment!r} missing from {message!r}"

    def test_a_route_carrying_every_shape_loads(self):
        """The positive case the validators exist to let through: literal
        config content, a rooted passthrough path, and provider-env values
        whose ``${gateway.*}`` / ``${secret:NAME}`` tokens this package stores
        opaque for the runtime that expands them."""
        from tolokaforge_coding_harnesses import GatewayRoute

        route = GatewayRoute(
            gateway="toloka_litellm",
            passthrough_path="/gemini",
            model_alias_pattern="{model}-moonshotai-pinned",
            config_files={
                "${HOME}/.gemini/settings.json": '{"security":{"auth":{"useExternal":true}}}'
            },
            container_env={"GEMINI_CLI_TRUST_WORKSPACE": "true"},
            provider_env={
                "GOOGLE_GEMINI_BASE_URL": "${gateway.base_url}${gateway.passthrough_path}",
                "GEMINI_API_KEY": "${secret:LITELLM_API_KEY}",
            },
        )
        assert route.provider_env["GOOGLE_GEMINI_BASE_URL"] == (
            "${gateway.base_url}${gateway.passthrough_path}"
        )


class TestTheShippedGatewayRecipes:
    """Which harnesses carry a route, and what each one claims."""

    def test_gemini_cli_routes_through_the_litellm_gemini_passthrough(self):
        from tolokaforge_coding_harnesses import HARNESSES

        route = HARNESSES["gemini-cli"].gateway_route
        assert route is not None
        assert route.gateway == "toloka_litellm"
        assert route.passthrough_path == "/gemini"
        assert route.config_files == {
            "${HOME}/.gemini/settings.json": (
                '{"security":{"auth":{"selectedType":"gateway","useExternal":true}},'
                '"tools":{"exclude":["google_web_search","web_fetch"]}}'
            )
        }
        assert route.container_env == {"GEMINI_CLI_TRUST_WORKSPACE": "true"}
        assert route.provider_env == {
            "GOOGLE_GEMINI_BASE_URL": "${gateway.base_url}${gateway.passthrough_path}",
            "GEMINI_API_KEY": "${secret:LITELLM_API_KEY}",
        }

    def test_kimi_code_carries_the_alias_pattern_and_no_model_name_key(self):
        """The alias is a model name, and ``provider_env`` is a closed
        allow-list of provider credential and endpoint names. The consuming
        runtime renders ``{model}`` and delivers it through
        ``env_model_vars``, which is where this CLI already reads its
        model name from."""
        from tolokaforge_coding_harnesses import HARNESSES

        spec = HARNESSES["kimi-code"]
        route = spec.gateway_route
        assert route is not None
        assert route.gateway == "toloka_litellm"
        assert route.model_alias_pattern == "{model}-moonshotai-pinned"
        assert route.provider_env == {
            "KIMI_MODEL_BASE_URL": "${gateway.base_url}",
            "KIMI_MODEL_API_KEY": "${secret:LITELLM_API_KEY}",
        }
        assert "KIMI_MODEL_NAME" not in route.provider_env
        assert "KIMI_MODEL_NAME" in spec.env_model_vars

    def test_a_route_may_coexist_with_a_request_middleware(self):
        """They are alternatives, not layers: a run reaching the gateway gets
        its provider pin from the gateway's own alias and boots no proxy. The
        ``config_files``/``request_middleware`` exclusion governs the default
        path only, so no cross-validator refuses this pairing."""
        from tolokaforge_coding_harnesses import HARNESSES

        spec = HARNESSES["kimi-code"]
        assert spec.request_middleware is not None
        assert spec.gateway_route is not None

    def test_opencode_carries_no_route_because_its_existing_map_is_the_fix(self):
        """The design's whole claim: a gateway consumer reads the fields a
        harness already declares, and only a harness whose recipe genuinely
        differs per gateway grows a second one."""
        from tolokaforge_coding_harnesses import HARNESSES

        assert HARNESSES["opencode"].gateway_route is None
        assert HARNESSES["opencode"].config_files

    def test_exactly_two_shipped_harnesses_declare_a_route(self):
        """A route ships credentials and an endpoint into a container, so make
        the shipped scope explicit — a copy-paste onto a third entry is caught
        here rather than discovered in a trial."""
        from tolokaforge_coding_harnesses import HARNESSES

        with_route = {name for name, spec in HARNESSES.items() if spec.gateway_route is not None}
        assert with_route == {"gemini-cli", "kimi-code"}
