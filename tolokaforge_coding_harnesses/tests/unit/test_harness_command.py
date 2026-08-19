"""The shell command a harness trial runs.

``harness_command`` is the one place the registry data becomes an invocation:
config-file writes and env exports in the preamble, then the CLI argv with the
instruction on argv or stdin. Every token is shell-quoted because the command
reaches ``bash -c`` inside the task container, and it is recorded on the trial
artifact — so a credential must never appear in it.
"""

import subprocess

import pytest

pytestmark = pytest.mark.unit


class TestHarnessCommand:
    def test_claude_code_argv(self):
        """claude-code exports the model quartet, pipes the instruction via
        stdin, and drops ``--model`` because the env vars carry the model."""
        import shlex

        from tolokaforge_coding_harnesses import harness_command

        command = harness_command("claude-code", "fix the bug", "anthropic/claude-sonnet-4-6")
        preamble, sep, cli = command.partition(" && printf ")
        assert sep, "claude-code pipes instruction via printf on stdin"
        # Preamble: five model exports, matching the reference env quartet + subagent var.
        exports = [p.strip() for p in preamble.split(" && ")]
        assert exports == [
            "export ANTHROPIC_MODEL=anthropic/claude-sonnet-4-6",
            "export ANTHROPIC_DEFAULT_SONNET_MODEL=anthropic/claude-sonnet-4-6",
            "export ANTHROPIC_DEFAULT_OPUS_MODEL=anthropic/claude-sonnet-4-6",
            "export ANTHROPIC_DEFAULT_HAIKU_MODEL=anthropic/claude-sonnet-4-6",
            "export CLAUDE_CODE_SUBAGENT_MODEL=anthropic/claude-sonnet-4-6",
        ]
        # printf part: instruction on stdin, no positional argv arg.
        printf_prefix, _, cli_only = ("printf " + cli).partition(" | ")
        assert shlex.split(printf_prefix) == ["printf", "%s", "fix the bug"]
        assert shlex.split(cli_only) == [
            "claude",
            "--verbose",
            "--output-format=stream-json",
            "--permission-mode=bypassPermissions",
            "--print",
        ]

    def test_codex_argv_shape(self):
        """codex chains a config.toml + auth.json write before the CLI. The
        CLI portion, after the final ``&&``, is what has to match the pinned
        shape — instruction stays on positional argv."""
        import shlex

        from tolokaforge_coding_harnesses import harness_command

        _, _, cli = harness_command("codex", "do it", "openai/gpt-5-codex").rpartition(" && ")
        assert shlex.split(cli) == [
            "codex",
            "exec",
            "--model",
            "gpt-5-codex",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "-c",
            "model_reasoning_effort=high",
            "do it",
        ]

    def test_gemini_argv_shape(self):
        import shlex

        from tolokaforge_coding_harnesses import harness_command

        # Shipped gemini-cli takes the direct Google AI Studio path — pure
        # env, no ``config_files``, so no preamble. The command is the CLI
        # argv verbatim.
        assert shlex.split(harness_command("gemini-cli", "do it", "google/gemini-2.5-flash")) == [
            "gemini",
            "--model",
            "gemini-2.5-flash",
            "--yolo",
            "--prompt",
            "do it",
        ]

    def test_codex_writes_config_toml_and_auth_json_before_the_cli(self):
        """codex reads ``openai_base_url`` from ``$CODEX_HOME/config.toml`` and
        the API key from ``$CODEX_HOME/auth.json``. The env vars they mirror
        both land the CLI at 401s without the files (config.toml drop routes
        to api.openai.com; missing auth.json earns "No cookie auth credentials
        found" from OpenRouter)."""
        from tolokaforge_coding_harnesses import harness_command

        # codex still uses positional-argv instruction, so the CLI is chained
        # after " && " and everything before that is preamble + the CLI itself.
        preamble, sep, _ = harness_command("codex", "do it", "m").rpartition(" && ")
        assert sep, "codex must chain a preamble before the CLI"
        assert "config.toml" in preamble
        assert "openai_base_url" in preamble
        assert "auth.json" in preamble
        assert "OPENAI_API_KEY" in preamble

    def test_shipped_gemini_default_writes_no_settings_json(self):
        """The shipped default is the direct Google AI Studio route — pure
        env, no config-file writes. A settings.json write is exclusive to
        the operator overlay at
        ``examples/terminal_bench/gemini_litellm_overlay.yaml`` (whose
        resolution is locked by
        ``TestHarnessPresetsFileOverlay.test_shipped_gemini_litellm_overlay_resolves``).
        """
        from tolokaforge_coding_harnesses import harness_command

        command = harness_command("gemini-cli", "go", "google/gemini-2.5-flash")
        assert " && " not in command
        assert "settings.json" not in command
        assert "printf" not in command

    def test_instruction_is_one_shell_argument(self):
        import shlex

        from tolokaforge_coding_harnesses import harness_command

        instruction = "don't $EXPAND `me`;\nsecond line"
        _, _, cli = harness_command("codex", instruction, "m").rpartition(" && ")
        argv = shlex.split(cli)
        assert argv[-1] == instruction

    def test_engine_loop_has_no_command(self):
        from tolokaforge_coding_harnesses import ENGINE_LOOP, harness_command

        with pytest.raises(ValueError, match="runs no CLI"):
            harness_command(ENGINE_LOOP, "anything", "m")

    def test_unknown_harness_names_accepted_set(self):
        from tolokaforge_coding_harnesses import validate_harness

        with pytest.raises(ValueError, match="claude-code"):
            validate_harness("bogus")

    def test_terminus_2_is_not_an_accepted_harness(self):
        """This repo installs no Terminus-2 scaffold, so no trial may claim it."""
        from tolokaforge_coding_harnesses import (
            accepted_harnesses,
            validate_harness,
        )

        assert "terminus-2" not in accepted_harnesses()
        with pytest.raises(ValueError, match="not supported"):
            validate_harness("terminus-2")


class TestHarnessRequestMiddleware:
    """The ``HarnessSpec.request_middleware`` slot + its shipped kimi-code use.

    Locks the preamble shape so a refactor of ``_middleware_preamble`` cannot
    silently change what runs inside the trial container. Real-container
    end-to-end coverage lives in the matrix rerun; these tests exist so a
    plain unit run catches regressions before a trial is spent.
    """

    def test_shipped_kimi_code_pins_moonshotai_provider_via_body_injection(self):
        """The row that motivated the whole slot: kimi-k2.7-code on OpenRouter
        fans out across 14 providers, and only Moonshot AI first-party returns
        non-empty completions on tool-call continuation. If this test flips,
        we're back to deterministic 0.433/0.6 baselines."""
        from tolokaforge_coding_harnesses import HARNESSES

        mw = HARNESSES["kimi-code"].request_middleware
        assert mw is not None
        assert mw.upstream_env_key == "KIMI_MODEL_BASE_URL"
        assert mw.body_injections == {
            "provider": {"only": ["moonshotai"], "allow_fallbacks": False}
        }
        assert mw.path_filter == "/chat/completions"

    def test_no_other_shipped_harness_declares_a_middleware(self):
        """Middleware boots a proxy inside every trial container it's set on —
        make the shipped scope explicit so a copy-paste edit is caught."""
        from tolokaforge_coding_harnesses import HARNESSES

        with_middleware = {
            name for name, spec in HARNESSES.items() if spec.request_middleware is not None
        }
        assert with_middleware == {"kimi-code"}

    def test_middleware_preamble_boots_proxy_then_rewrites_env_before_cli(self):
        """The three-step preamble the CLI depends on:
        (1) daemon-mode proxy boot reading the ORIGINAL env-var value as
            upstream (so it forwards to the real provider);
        (2) env-var rewrite to localhost so the CLI reaches the proxy;
        (3) CLI invocation.
        A refactor that reorders these breaks either the forwarding chain
        (proxy hits localhost recursively) or the CLI's routing.
        """
        from tolokaforge_coding_harnesses import harness_command

        steps = harness_command("kimi-code", "do it", "openrouter/moonshotai/kimi-k2.7-code").split(
            " && "
        )
        # First non-config step boots the proxy against the ORIGINAL URL
        boot = next(s for s in steps if "middleware_proxy.py" in s)
        assert '"${KIMI_MODEL_BASE_URL}"' in boot
        assert '"only": ["moonshotai"]' in boot
        assert "/chat/completions" in boot
        assert boot.rstrip().endswith("--daemon")
        # Env rewrite MUST come after the boot (so the boot reads upstream)
        # and MUST come before the CLI (so the CLI reaches localhost)
        boot_idx = steps.index(boot)
        rewrite_idx = next(
            i for i, s in enumerate(steps) if s.startswith("export KIMI_MODEL_BASE_URL=")
        )
        cli_idx = next(i for i, s in enumerate(steps) if s.startswith("kimi "))
        assert boot_idx < rewrite_idx < cli_idx
        assert steps[rewrite_idx] == "export KIMI_MODEL_BASE_URL=http://127.0.0.1:8899"

    def test_a_spec_that_declares_both_middleware_and_config_files_is_refused(self):
        """The two features do not compose today: ``config_files`` templates
        interpolate provider_env at Python-assembly time, while the middleware
        rewrite happens at bash time. A CLI reading its endpoint from an
        on-disk config would bake in the upstream URL and bypass the proxy."""
        from tolokaforge_coding_harnesses import (
            HarnessSpec,
            RequestMiddleware,
        )

        with pytest.raises(Exception, match="request_middleware and config_files"):
            HarnessSpec(
                install_source="some-pkg",
                version="1.0.0",
                argv_prefix=("cli",),
                argv_suffix=(),
                config_files={"/etc/cli.conf": "endpoint={{ base_url }}"},
                request_middleware=RequestMiddleware(upstream_env_key="X_BASE_URL"),
            )

    def test_if_the_validator_ever_loosens_the_preamble_order_is_middleware_first(self):
        """Defensive positive test: the validator refuses the combo today, so
        the assembler's ordering of middleware boot → env rewrite →
        config_files write → CLI is unreachable in production. If the
        validator ever loosens (see the ADR-0033 sibling entry that names the
        two features), a CLI reading its endpoint from an on-disk config file
        MUST see the redirected localhost URL, not the upstream. Construct a
        spec with both fields via ``model_construct`` (which bypasses the
        validator) and assert that ordering, so a future loosening does not
        silently regress the proxy path."""
        from tolokaforge_coding_harnesses import (
            HarnessSpec,
            RequestMiddleware,
            harness_command,
        )

        # ``model_construct`` is Pydantic v2's documented escape hatch for
        # bypassing validators — the escape hatch exists for exactly this
        # kind of test.
        spec = HarnessSpec.model_construct(
            install_method="npm",
            install_source="fake-cli",
            version="1.0.0",
            argv_prefix=("fake",),
            argv_suffix=("--prompt",),
            config_files={"${HOME}/.fake/endpoint.conf": "endpoint={{ base_url }}"},
            request_middleware=RequestMiddleware(
                upstream_env_key="FAKE_BASE_URL",
                body_injections={},
                header_injections={},
            ),
            provider_env={"FAKE_BASE_URL": "https://upstream.example"},
            container_env={},
            env_model_vars=(),
            model_flag="--model",
            model_flag_style="space",
            flags_pre_permission=(),
            instruction_channel="argv",
            skills_dir_target=None,
            strip_vendor_namespace=False,
            strip_openrouter_prefix=True,
        )

        command = harness_command(
            "fake", "do it", "some-model", registry={"fake": spec}, provider_env=spec.provider_env
        )
        steps = command.split(" && ")
        middleware_boot = next(i for i, s in enumerate(steps) if "middleware_proxy.py" in s)
        env_rewrite = next(i for i, s in enumerate(steps) if s.startswith("export FAKE_BASE_URL="))
        config_write = next(i for i, s in enumerate(steps) if "endpoint.conf" in s)
        assert middleware_boot < env_rewrite < config_write, (
            f"middleware+config_files preamble order broke: "
            f"boot={middleware_boot}, rewrite={env_rewrite}, config={config_write}"
        )


class TestHarnessConfigFiles:
    """CLIs configured by file: rendered from the declared variables only."""

    @staticmethod
    def _spec(**overrides):
        from tolokaforge_coding_harnesses import HarnessSpec

        return HarnessSpec(
            install_source="cli", version="1", argv_prefix=("cli",), argv_suffix=(), **overrides
        )

    def test_config_toml_renders_the_effective_base_url(self):
        """The endpoint the trial's container will carry, not the shipped
        default — a run config pointing the harness at its own gateway has to
        reach the file codex actually reads."""
        from tolokaforge_coding_harnesses import harness_command

        command = harness_command(
            "codex",
            "do it",
            "m",
            provider_env={
                "OPENAI_BASE_URL": "https://gateway.invalid/v1",
                "OPENAI_API_KEY": "sk-not-in-the-command",
            },
        )
        assert 'openai_base_url = \\"https://gateway.invalid/v1\\"' in command

    def test_auth_json_names_the_key_env_var_and_never_its_value(self):
        """The command lands on ``TaskDescription.metadata`` and from there in
        the trial artifacts, so the credential reaches the file through the
        container's environment instead."""
        from tolokaforge_coding_harnesses import harness_command

        command = harness_command(
            "codex",
            "do it",
            "m",
            provider_env={
                "OPENAI_BASE_URL": "https://gateway.invalid/v1",
                "OPENAI_API_KEY": "sk-not-in-the-command",
            },
        )
        assert "$OPENAI_API_KEY" in command
        assert "sk-not-in-the-command" not in command

    def test_rendered_files_land_where_the_cli_reads_them(self, tmp_path):
        """The assembled preamble is shell, so run it: the quoting, the
        ``$HOME``-rooted path and the credential expansion all have to survive
        a real shell, and none of that is visible in the string."""
        from tolokaforge_coding_harnesses import harness_command

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "codex").write_text("#!/bin/sh\n")
        (bin_dir / "codex").chmod(0o755)
        command = harness_command("codex", "do it", "openrouter/openai/gpt-5-mini")

        proc = subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=30,
            env={
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "HOME": str(tmp_path),
                "OPENAI_API_KEY": "sk-from-the-container",
            },
        )
        assert proc.returncode == 0, proc.stderr
        assert (tmp_path / ".codex" / "config.toml").read_text() == (
            'openai_base_url = "https://openrouter.ai/api/v1"\n'
        )
        assert (tmp_path / ".codex" / "auth.json").read_text() == (
            '{"OPENAI_API_KEY": "sk-from-the-container"}\n'
        )

    def test_undeclared_variable_is_refused_at_construction(self):
        from tolokaforge_coding_harnesses import CONFIG_TEMPLATE_VARIABLES

        with pytest.raises(ValueError, match="undeclared variable"):
            self._spec(config_files={"/etc/cli.toml": "key = {{ api_key }}\n"})
        assert "api_key" not in CONFIG_TEMPLATE_VARIABLES

    def test_relative_path_is_refused_at_construction(self):
        with pytest.raises(ValueError, match="is relative"):
            self._spec(config_files={"cli.toml": "key = {{ model }}\n"})

    def test_every_declared_variable_renders(self):
        """The whitelist is the contract an operator writes templates against."""
        from tolokaforge_coding_harnesses import harness_command

        spec = self._spec(
            config_files={
                "$HOME/cli.toml": (
                    "model={{ model }} provider={{ provider }} "
                    "base_url={{ base_url }} key=${{ api_key_env }}\n"
                )
            },
        )
        command = harness_command(
            "cli",
            "go",
            "openrouter/openai/gpt-5-mini",
            registry={"cli": spec},
            provider_env={"OPENAI_BASE_URL": "https://x.invalid/v1", "OPENAI_API_KEY": "s"},
        )
        assert (
            "model=openai/gpt-5-mini provider=openrouter "
            "base_url=https://x.invalid/v1 key=$OPENAI_API_KEY" in command
        )

    @pytest.mark.parametrize(
        ("template", "provider_env"),
        [
            pytest.param(
                "url={{ base_url }}\n",
                {"OPENAI_BASE_URL": "https://a.invalid", "ANTHROPIC_BASE_URL": "b"},
                id="two-base-urls",
            ),
            pytest.param(
                "key=${{ api_key_env }}\n",
                {"OPENAI_API_KEY": "sk-a", "ANTHROPIC_API_KEY": "sk-b"},
                id="two-api-keys",
            ),
        ],
    )
    def test_ambiguous_provider_envelope_is_refused(self, template, provider_env):
        """Two endpoints — or two keys — leave no single answer for the template."""
        from tolokaforge_coding_harnesses import harness_command

        spec = self._spec(config_files={"/etc/cli.toml": template})
        with pytest.raises(ValueError, match="several entries"):
            harness_command("cli", "go", "m", registry={"cli": spec}, provider_env=provider_env)


class TestGatewayRouteIsInertToTheCommand:
    """``HarnessSpec.gateway_route`` describes how a *second* runtime reaches an
    alternative gateway. It is data this assembler must never read.

    The lock below is why: a route carries a ``config_files`` map, a
    ``container_env`` map and a ``provider_env`` map under the same names the
    default path uses, so the tempting edit — "the route has config files too,
    write them" — is one line away and would silently route every TF-side trial
    through a gateway the run config never named.
    """

    def test_the_command_is_byte_identical_with_and_without_a_route(self):
        from tolokaforge_coding_harnesses import GatewayRoute, HarnessSpec, harness_command

        unrouted = HarnessSpec(
            install_source="cli",
            version="1",
            argv_prefix=("cli",),
            argv_suffix=("--print",),
            config_files={"${HOME}/.cli/config.json": '{"model": "{{ model }}"}'},
            container_env={"CLI_TRUST_WORKSPACE": "true"},
            provider_env={
                "OPENAI_BASE_URL": "https://openrouter.ai/api/v1",
                "OPENAI_API_KEY": "${secret:OPENROUTER_API_KEY}",
            },
        )
        # ``model_copy`` rather than a second constructor call: it guarantees
        # the two specs differ in exactly the one field under test.
        routed = unrouted.model_copy(
            update={
                "gateway_route": GatewayRoute(
                    gateway="toloka_litellm",
                    passthrough_path="/gemini",
                    model_alias_pattern="{model}-pinned",
                    config_files={"${HOME}/.cli/gateway.json": '{"auth": "gateway"}'},
                    container_env={"CLI_GATEWAY_MODE": "1"},
                    provider_env={
                        "OPENAI_BASE_URL": "${gateway.base_url}${gateway.passthrough_path}",
                        "OPENAI_API_KEY": "${secret:LITELLM_API_KEY}",
                    },
                )
            }
        )

        assert harness_command("cli", "go", "m", {"cli": unrouted}) == harness_command(
            "cli", "go", "m", {"cli": routed}
        )

    @pytest.mark.parametrize(
        ("harness", "model"),
        [
            ("gemini-cli", "google/gemini-2.5-flash"),
            ("kimi-code", "openrouter/moonshotai/kimi-k2.7-code"),
        ],
    )
    def test_a_shipped_route_changes_nothing_about_that_harnesss_command(self, harness, model):
        """The same lock on the real entries that carry a route. Dropping the
        route from the shipped spec must produce the identical command, which
        is what "inert to the TF-side path" means for a harness an operator
        actually runs."""
        from tolokaforge_coding_harnesses import HARNESSES, harness_command

        spec = HARNESSES[harness]
        assert spec.gateway_route is not None, f"{harness} no longer carries a route to test"
        unrouted = spec.model_copy(update={"gateway_route": None})

        assert harness_command(harness, "do it", model) == harness_command(
            harness, "do it", model, {harness: unrouted}
        )

    def test_the_shipped_commands_carry_nothing_from_the_gateway_vocabulary(self):
        """No shipped harness's command may name a gateway token or a catalog
        key — the whole vocabulary is opaque to this side."""
        from tolokaforge_coding_harnesses import ALTERNATIVE_GATEWAYS, HARNESSES, harness_command

        for name in HARNESSES:
            command = harness_command(name, "go", "openrouter/vendor/model")
            assert "${gateway." not in command
            for gateway_name in ALTERNATIVE_GATEWAYS:
                assert gateway_name not in command


class TestModelFlagStyle:
    def test_space_style_is_two_argv_words(self):
        import shlex

        from tolokaforge_coding_harnesses import harness_command

        argv = shlex.split(harness_command("gemini-cli", "go", "google/gemini-2.5-flash"))
        assert argv[1:3] == ["--model", "gemini-2.5-flash"]

    def test_equals_style_is_one_argv_word(self):
        import shlex

        from tolokaforge_coding_harnesses import HarnessSpec, harness_command

        spec = HarnessSpec(
            install_source="opencode",
            version="1",
            argv_prefix=("opencode", "run"),
            argv_suffix=(),
            model_flag_style="equals",
        )
        command = harness_command("opencode", "go", "openrouter/openai/gpt-5", {"opencode": spec})
        assert shlex.split(command) == ["opencode", "run", "--model=openai/gpt-5", "go"]
