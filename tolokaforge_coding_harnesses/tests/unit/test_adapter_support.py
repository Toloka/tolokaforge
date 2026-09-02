"""The adapter-side coding-harness pattern one address any adapter can inherit.

The mixin is the "how an adapter adopts coding-harness mode" contract: adapter
declares the capability flag, resolves a spec, builds the shell command,
emits the metadata handshake the conductor branches on, emits the two runner
payloads (bash tool + ``test_execution`` grading), and lays out the standalone
install-script Dockerfile layer. Every helper here matches — bytes-for-bytes
where relevant — what the terminal-bench adapter emits today, so the Move 3
refactor to inherit is a change of caller, not of contract.

The engine's :class:`~tolokaforge.runner.models.ToolSchema` /
:class:`~tolokaforge.runner.models.RunnerGradingConfig` are pydantic models
and this test suite asserts the payload dict shape directly: importing the
engine types from tests would break the same boundary
``tests/unit/test_package_boundary.py`` guards on the source side.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from tolokaforge_coding_harnesses import (
    ENGINE_LOOP,
    HARNESSES,
    INSTALL_SCRIPT,
    MIDDLEWARE_PROXY_CONTAINER_PATH,
    MIDDLEWARE_PROXY_SCRIPT,
    CodingHarnessAdapterMixin,
    HarnessSpec,
    harness_command,
)

pytestmark = pytest.mark.unit


_SHIPPED_HARNESS_NAMES = (
    "claude-code",
    "codex",
    "gemini-cli",
    "grok-build",
    "kimi-code",
    "opencode",
)


class _Adapter(CodingHarnessAdapterMixin):
    """Bare mixin instance for the tests — a real adapter would inherit from
    ``BaseAdapter`` alongside; the mixin's helpers touch no adapter state, so a
    minimal subclass is enough to exercise them."""


@pytest.fixture
def adapter() -> _Adapter:
    return _Adapter()


class TestCapabilityFlag:
    def test_mixin_declares_supports_coding_harness_true(self) -> None:
        """The orchestrator's config-validation gate reads this class attribute
        to decide whether ``models.agent.harness`` can route to the adapter."""
        assert CodingHarnessAdapterMixin.supports_coding_harness is True
        assert _Adapter.supports_coding_harness is True
        assert _Adapter().supports_coding_harness is True


class TestResolveHarnessSpec:
    @pytest.mark.parametrize("name", _SHIPPED_HARNESS_NAMES)
    def test_accepts_each_of_the_six_shipped_names(self, adapter: _Adapter, name: str) -> None:
        """The shipped registry is what the default path resolves against;
        every documented harness must survive the round-trip."""
        spec = adapter.resolve_harness_spec(name, "vendor/some-model")
        assert isinstance(spec, HarnessSpec)
        assert spec == HARNESSES[name]

    def test_rejects_unknown_name(self, adapter: _Adapter) -> None:
        with pytest.raises(ValueError) as excinfo:
            adapter.resolve_harness_spec("not-a-harness", "vendor/some-model")
        # The rejection lists the accepted set so the caller sees the fix
        # for a typo without reading the registry docs.
        message = str(excinfo.value)
        assert "not-a-harness" in message
        for name in _SHIPPED_HARNESS_NAMES:
            assert name in message

    def test_rejects_engine_loop_because_it_runs_no_cli(self, adapter: _Adapter) -> None:
        with pytest.raises(ValueError, match="runs no CLI"):
            adapter.resolve_harness_spec(ENGINE_LOOP, "vendor/some-model")

    def test_rejects_empty_agent_model_for_real_harness(self, adapter: _Adapter) -> None:
        # The rejection reason mirrors the terminal-bench check that lifted
        # into the mixin: a blank model lets the CLI pick its own default,
        # so what runs is not the config's declared subject-under-test.
        with pytest.raises(ValueError, match="requires a non-empty agent_model"):
            adapter.resolve_harness_spec("claude-code", "")


class TestBuildHarnessCommand:
    @pytest.mark.parametrize(
        "name,model",
        [
            ("claude-code", "openrouter/anthropic/claude-sonnet-4-6"),
            ("codex", "openai/gpt-5-codex"),
            ("gemini-cli", "google/gemini-2.5-flash"),
        ],
    )
    def test_produces_the_same_bytes_as_direct_harness_command(
        self, adapter: _Adapter, name: str, model: str
    ) -> None:
        """The mixin is a call-forwarder; the assembled command must be
        byte-identical to what ``harness_command`` produces for the same inputs.
        Any drift here would ship a different invocation than the artifact
        records."""
        spec = adapter.resolve_harness_spec(name, model)
        instruction = "fix the failing test"
        via_mixin = adapter.build_harness_command(name, spec, instruction, model)
        via_direct = harness_command(name, instruction, model)
        assert via_mixin == via_direct

    def test_forwards_provider_env_to_config_file_rendering(self, adapter: _Adapter) -> None:
        # codex renders config.toml + auth.json from the provider envelope;
        # the mixin has to route provider_env through so the rendered
        # ``base_url`` / ``api_key_env`` match the caller's envelope.
        model = "openrouter/openai/gpt-5-codex"
        instruction = "do it"
        provider_env = {
            "OPENAI_BASE_URL": "https://custom.gateway.example/v1",
            "OPENAI_API_KEY": "",
        }
        spec = adapter.resolve_harness_spec("codex", model)
        via_mixin = adapter.build_harness_command(
            "codex", spec, instruction, model, provider_env=provider_env
        )
        via_direct = harness_command("codex", instruction, model, provider_env=provider_env)
        assert via_mixin == via_direct
        # And the caller's URL made it into the config-file preamble.
        assert "custom.gateway.example" in via_mixin


class TestEmitHarnessMetadata:
    def test_returns_exactly_the_four_conductor_keys(self, adapter: _Adapter) -> None:
        # The conductor branches on ``agent_harness_command`` and records the
        # other three on the trajectory; anything else is task-specific and
        # belongs on the adapter's own metadata merge, not this dict.
        spec = adapter.resolve_harness_spec("claude-code", "vendor/some-model")
        command = 'printf %s "instr" | claude --print'
        model = "openrouter/anthropic/claude-sonnet-4-6"
        metadata = adapter.emit_harness_metadata("claude-code", spec, command, model)
        assert metadata == {
            "agent_harness": "claude-code",
            "agent_harness_version": spec.version,
            "agent_harness_model": model,
            "agent_harness_command": command,
        }


class TestEmitHarnessToolSchema:
    def test_payload_matches_the_shape_the_runner_reads(self, adapter: _Adapter) -> None:
        # The DOCKER_COMPOSE_EXEC wrapper factory in the runner reads
        # source.extra['service'] + source.extra['compose_project_prefix']
        # to resolve the container; those must land on the payload verbatim.
        payload = adapter.emit_harness_tool_schema(
            service="agent",
            compose_project_prefix="tolokaforge-tbench",
            timeout_s=1200.0,
        )
        assert payload["name"] == "bash"
        assert payload["category"] == "compute"
        assert payload["timeout_s"] == 1200.0
        assert payload["parameters"] == {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to run",
                }
            },
            "required": ["command"],
        }
        source = payload["source"]
        assert source["invocation_style"] == "docker_compose_exec"
        assert source["class_name"] == "bash"
        assert source["module_path"] == ""
        # Default toolset is adapter-neutral so an adapter overrides only if
        # its factory-side dispatch needs the historical label.
        assert source["toolset"] == "coding_harness"
        assert source["extra"] == {
            "service": "agent",
            "compose_project_prefix": "tolokaforge-tbench",
        }

    def test_toolset_override_reaches_the_source(self, adapter: _Adapter) -> None:
        payload = adapter.emit_harness_tool_schema(
            service="agent",
            compose_project_prefix="tolokaforge-tbench",
            timeout_s=1200.0,
            toolset="terminal_bench",
        )
        assert payload["source"]["toolset"] == "terminal_bench"


class TestEmitTestExecutionGrading:
    def test_payload_matches_the_test_execution_grading_dispatch(self, adapter: _Adapter) -> None:
        # The runner dispatches on grading_method="test_execution"; weights
        # and threshold match the historical terminal-bench values so a run
        # switching to the mixin scores byte-identically.
        assert adapter.emit_test_execution_grading() == {
            "combine_method": "weighted",
            "weights": {"custom_checks": 1.0},
            "pass_threshold": 0.5,
            "grading_method": "test_execution",
        }


class TestWriteInstallScriptLayer:
    def test_writes_dockerfile_and_install_script_flat_under_context_dir(
        self, adapter: _Adapter, tmp_path: Path
    ) -> None:
        spec = HARNESSES["codex"]
        dockerfile_relpath = adapter.write_install_script_layer(
            tmp_path, base_image="python:3.11-slim", spec=spec
        )
        assert dockerfile_relpath == "harness.Dockerfile"

        # Installer is copied into the context, verbatim.
        installed = tmp_path / INSTALL_SCRIPT.name
        assert installed.is_file()
        assert installed.read_bytes() == INSTALL_SCRIPT.read_bytes()

        dockerfile_text = (tmp_path / dockerfile_relpath).read_text()
        lines = dockerfile_text.splitlines()
        # FROM line — base image passthrough.
        assert lines[0] == "FROM python:3.11-slim"
        # COPY sources the sibling installer by its bare basename (context is
        # tmp_path itself), destinations the container-side install path
        # the RUN line then invokes.
        assert lines[1] == f"COPY {INSTALL_SCRIPT.name} /opt/tolokaforge/install-harness.sh"
        assert lines[2] == (
            "RUN sh /opt/tolokaforge/install-harness.sh "
            f"{spec.install_method} "
            f"{shlex.quote(spec.install_source)} {shlex.quote(spec.version)}"
        )

    def test_middleware_proxy_flag_copies_proxy_and_adds_copy_line(
        self, adapter: _Adapter, tmp_path: Path
    ) -> None:
        spec = HARNESSES["kimi-code"]
        adapter.write_install_script_layer(
            tmp_path, base_image="python:3.11-slim", spec=spec, middleware_proxy=True
        )
        proxy_dest = tmp_path / MIDDLEWARE_PROXY_SCRIPT.name
        assert proxy_dest.is_file()
        assert proxy_dest.read_bytes() == MIDDLEWARE_PROXY_SCRIPT.read_bytes()

        dockerfile_text = (tmp_path / "harness.Dockerfile").read_text()
        assert (
            f"COPY {MIDDLEWARE_PROXY_SCRIPT.name} {MIDDLEWARE_PROXY_CONTAINER_PATH}"
            in dockerfile_text
        )

    def test_middleware_proxy_default_off_omits_proxy_copy(
        self, adapter: _Adapter, tmp_path: Path
    ) -> None:
        spec = HARNESSES["codex"]
        adapter.write_install_script_layer(tmp_path, base_image="python:3.11-slim", spec=spec)
        assert not (tmp_path / MIDDLEWARE_PROXY_SCRIPT.name).exists()
        dockerfile_text = (tmp_path / "harness.Dockerfile").read_text()
        assert MIDDLEWARE_PROXY_CONTAINER_PATH not in dockerfile_text

    def test_dockerfile_ends_with_a_trailing_newline(
        self, adapter: _Adapter, tmp_path: Path
    ) -> None:
        # Docker doesn't require it, but every text file we write elsewhere in
        # the repo ends with one; pinning the convention avoids a churn diff
        # if the terminal-bench refactor decides to keep the file byte-identical.
        adapter.write_install_script_layer(
            tmp_path, base_image="python:3.11-slim", spec=HARNESSES["codex"]
        )
        assert (tmp_path / "harness.Dockerfile").read_text().endswith("\n")
