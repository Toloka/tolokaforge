"""Behaviour-locking tests for the coding-harness driver's credential shield.

Exercises :class:`CodingHarnessDriver` end to end (real staged compose, real
:class:`LLMGatewayEndpoint` bound to a loopback ephemeral port) rather than
mocking the launcher — a real bind is fast and is stronger evidence than a
stub that the real credential never reaches the compose file.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.agent_driver import AgentDriver, EngineLoopDriver
from tolokaforge.core.drivers.coding_harness import CodingHarnessDriver, HarnessSelection
from tolokaforge.secrets import DictProvider, SecretManager, init_default_from

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACK_ROOT = _REPO_ROOT / "examples" / "native" / "coding_harness"

CANARY = "sk-REAL-UPSTREAM-CANARY-TOKEN-0123456789abcdef"


def _pack_adapter() -> NativeAdapter:
    return NativeAdapter({"tasks_glob": "task.yaml", "task_packs": [str(_PACK_ROOT)]})


def _driver(**overrides: object) -> CodingHarnessDriver:
    kwargs: dict[str, object] = {
        "agent_harness": "claude-code",
        "agent_model": "openrouter/anthropic/claude-sonnet-4-6",
    }
    kwargs.update(overrides)
    return CodingHarnessDriver(HarnessSelection(**kwargs))


@pytest.fixture
def canary_secret_manager(isolated_secret_manager: None) -> Iterator[None]:
    """Process default SecretManager resolves the canary for every real
    provider key a shipped harness might ask for — never a real credential."""
    init_default_from(
        SecretManager([DictProvider({"OPENROUTER_API_KEY": CANARY, "GEMINI_API_KEY": CANARY})])
    )
    yield


class TestContainerEnvNeverCarriesTheRealToken:
    def test_rewritten_cli_service_never_contains_the_canary(
        self, canary_secret_manager: None
    ) -> None:
        """The CLI's own compose service (``main``) must carry only the dummy
        credential. The gateway sidecar service DOES hold the real upstream
        token in its ``environment:`` — that is the whole point of the sidecar
        (the CLI reads dummy; the sidecar swaps in real on the way out). The
        assertion is scoped to the CLI's service, not the whole compose text."""
        adapter = _pack_adapter()
        staged = adapter.stage_task("fix_factorial")
        assert staged is not None
        driver = _driver()
        driver.attach("native", True)
        try:
            driver.apply_container_layers(staged=staged)
            doc = yaml.safe_load(staged.compose_file.read_text())
            cli_env = doc["services"]["main"].get("environment", {})
            cli_env_str = yaml.safe_dump(cli_env)
            assert CANARY not in cli_env_str
            assert "sk-tolokaforge-shielded-dummy-not-a-real-key" in cli_env_str
        finally:
            driver.close()

    def test_sidecar_carries_the_real_token_isolated_from_the_cli(
        self, canary_secret_manager: None
    ) -> None:
        """The gateway sidecar service carries the real token in its own
        ``environment:``; the CLI service does not. Pins both facts so a
        future refactor cannot collapse them without a test failure."""
        adapter = _pack_adapter()
        staged = adapter.stage_task("fix_factorial")
        assert staged is not None
        driver = _driver()
        driver.attach("native", True)
        try:
            driver.apply_container_layers(staged=staged)
            doc = yaml.safe_load(staged.compose_file.read_text())
            sidecar_env = doc["services"]["tolokaforge-llm-gateway"]["environment"]
            assert sidecar_env["TF_GATEWAY_UPSTREAM_TOKEN"] == CANARY
            cli_env = doc["services"]["main"].get("environment", {})
            assert CANARY not in yaml.safe_dump(cli_env)
        finally:
            driver.close()

    @pytest.mark.parametrize("agent_harness", ["codex", "opencode", "grok-build"])
    def test_harness_command_never_carries_the_canary_for_config_file_harnesses(
        self, canary_secret_manager: None, agent_harness: str
    ) -> None:
        """Harnesses that write on-disk auth files via `config_files` render
        the file contents into the ``agent_harness_command`` shell string as
        a ``printf ... | tee`` step. Under the shield, the token env var
        the config file references (`$OPENAI_API_KEY`, `$ANTHROPIC_API_KEY`,
        `$OPENROUTER_API_KEY`) expands at container-runtime from
        ``container_env`` — which carries the dummy value. The real token
        (`CANARY` under this fixture's `SecretManager`) must never appear
        anywhere in the rendered command."""
        adapter = _pack_adapter()
        staged = adapter.stage_task("fix_factorial")
        assert staged is not None
        base = adapter.to_task_description("fix_factorial")
        driver = _driver(agent_harness=agent_harness)
        driver.attach("native", True)
        try:
            td = driver.decorate_task_description(base, staged=staged)
            command = td.metadata["agent_harness_command"]
            assert CANARY not in command
        finally:
            driver.close()


class TestSidecarService:
    """The gateway is a compose sidecar, not a host process. The CLI reaches
    it over docker's own DNS at ``http://tolokaforge-llm-gateway:8080`` —
    no ``extra_hosts`` mapping, no ``host-gateway`` docker-bridge magic, no
    dependence on the pack's declared ``network_policy``."""

    def test_sidecar_service_added_under_a_gateway_active_harness(
        self, canary_secret_manager: None
    ) -> None:
        adapter = _pack_adapter()
        staged = adapter.stage_task("fix_factorial")
        assert staged is not None
        driver = _driver(agent_harness="claude-code")
        driver.attach("native", True)
        try:
            driver.apply_container_layers(staged=staged)
            doc = yaml.safe_load(staged.compose_file.read_text())
            sidecar = doc["services"]["tolokaforge-llm-gateway"]
            assert sidecar["command"] == [
                "python",
                "-m",
                "tolokaforge.core.drivers.llm_gateway_serve",
            ]
            assert sidecar["environment"]["TF_GATEWAY_PORT"] == "8080"
            # CLI depends on the sidecar's healthcheck so it never races.
            assert doc["services"]["main"]["depends_on"]["tolokaforge-llm-gateway"] == {
                "condition": "service_healthy"
            }
            # No host-gateway extra_hosts under sidecar mode.
            assert "extra_hosts" not in doc["services"]["main"]
        finally:
            driver.close()

    def test_no_sidecar_when_credential_gateway_is_none(self, canary_secret_manager: None) -> None:
        adapter = _pack_adapter()
        staged = adapter.stage_task("fix_factorial")
        assert staged is not None
        driver = _driver(agent_harness="gemini-cli")
        driver.attach("native", True)
        try:
            driver.apply_container_layers(staged=staged)
            doc = yaml.safe_load(staged.compose_file.read_text())
            assert "tolokaforge-llm-gateway" not in doc["services"]
            assert "extra_hosts" not in doc["services"]["main"]
        finally:
            driver.close()


class TestRunnerSecretStrip:
    """Under the shield, the sidecar already carries the real upstream
    token. The driver marks that key as ``stripped_container_secrets`` on
    the manifest so ``inject_runner_credentials`` omits it from the
    runner's ``TOLOKAFORGE_SECRETS_JSON`` payload — the credential lives
    in exactly one place inside the trial stack (the sidecar), not two."""

    def test_shielded_manifest_strips_the_shielded_upstream_token(
        self, canary_secret_manager: None
    ) -> None:
        adapter = _pack_adapter()
        staged = adapter.stage_task("fix_factorial")
        assert staged is not None
        base = adapter.to_task_description("fix_factorial")
        driver = _driver(agent_harness="claude-code")
        driver.attach("native", True)
        try:
            td = driver.decorate_task_description(base, staged=staged)
            manifest = td.environment_manifest
            assert manifest is not None
            gateway_spec = driver.spec.credential_gateway
            assert gateway_spec is not None
            assert gateway_spec.upstream_token_env_var in manifest.stripped_container_secrets
        finally:
            driver.close()

    def test_unshielded_manifest_leaves_strip_set_untouched(
        self, canary_secret_manager: None
    ) -> None:
        adapter = _pack_adapter()
        staged = adapter.stage_task("fix_factorial")
        assert staged is not None
        base = adapter.to_task_description("fix_factorial")
        driver = _driver(agent_harness="gemini-cli")
        driver.attach("native", True)
        try:
            td = driver.decorate_task_description(base, staged=staged)
            manifest = td.environment_manifest
            assert manifest is not None
            assert manifest.stripped_container_secrets == frozenset()
        finally:
            driver.close()


class TestDriverClose:
    def test_idempotent(self, canary_secret_manager: None) -> None:
        driver = _driver()
        driver.attach("native", True)
        driver.close()
        driver.close()  # must not raise

    def test_noop_without_a_prior_attach(self) -> None:
        driver = _driver()
        driver.close()  # must not raise; no gateway was ever launched


class TestAgentDriverProtocolClose:
    def test_engine_loop_driver_implements_close(self) -> None:
        assert isinstance(EngineLoopDriver(), AgentDriver)
        EngineLoopDriver().close()  # must not raise


class TestNetworkPolicy:
    """Sidecar-mode gateway shares the CLI's compose network via docker DNS,
    so the shield works whichever ``NetworkPolicy`` the pack declared. The
    driver adds :data:`GATEWAY_HOSTNAME` to
    :attr:`~EnvironmentManifest.bridged_services` — same treatment as
    ``runner_service`` — so under ``no_internet``/``limited_internet``
    the sidecar joins BOTH the internal (CLI-reachable) and edge
    (upstream-reachable) networks. The pack's declared policy is
    preserved unchanged; no elevation, no downgrade."""

    def test_shielded_no_internet_pack_is_preserved_and_gateway_bridged(
        self, canary_secret_manager: None
    ) -> None:
        from tolokaforge.core.drivers.llm_gateway import GATEWAY_HOSTNAME
        from tolokaforge.runner.models import NetworkPolicy

        adapter = _pack_adapter()
        staged = adapter.stage_task("fix_factorial")
        assert staged is not None
        base = adapter.to_task_description("fix_factorial")
        assert base.environment_manifest is not None
        assert base.environment_manifest.network_policy is NetworkPolicy.NO_INTERNET
        driver = _driver(agent_harness="claude-code")
        driver.attach("native", True)
        try:
            td = driver.decorate_task_description(base, staged=staged)
            manifest = td.environment_manifest
            assert manifest is not None
            # Pack's declared policy is preserved — the sidecar handles the
            # bridge instead.
            assert manifest.network_policy is NetworkPolicy.NO_INTERNET
            # Gateway service is bridged so it has both internal and edge
            # network attachments once the runtime applies netpolicy.
            assert GATEWAY_HOSTNAME in manifest.bridged_services
        finally:
            driver.close()

    def test_shielded_full_internet_pack_is_preserved(self, canary_secret_manager: None) -> None:
        from tolokaforge.runner.models import NetworkPolicy

        adapter = _pack_adapter()
        staged = adapter.stage_task("fix_factorial")
        assert staged is not None
        base = adapter.to_task_description("fix_factorial")
        assert base.environment_manifest is not None
        # Force the pack's manifest to FULL_INTERNET so the elevation branch
        # is skipped — this pins that the elevation is targeted, not blanket.
        base_with_full = base.model_copy(
            update={
                "environment_manifest": base.environment_manifest.model_copy(
                    update={"network_policy": NetworkPolicy.FULL_INTERNET}
                )
            }
        )
        driver = _driver(agent_harness="claude-code")
        driver.attach("native", True)
        try:
            td = driver.decorate_task_description(base_with_full, staged=staged)
            manifest = td.environment_manifest
            assert manifest is not None
            assert manifest.network_policy is NetworkPolicy.FULL_INTERNET
        finally:
            driver.close()

    def test_unshielded_harness_preserves_pack_declared_network_policy(
        self, canary_secret_manager: None
    ) -> None:
        adapter = _pack_adapter()
        staged = adapter.stage_task("fix_factorial")
        assert staged is not None
        base = adapter.to_task_description("fix_factorial")
        base_policy = (
            base.environment_manifest.network_policy if base.environment_manifest else None
        )
        driver = _driver(agent_harness="gemini-cli")
        driver.attach("native", True)
        try:
            td = driver.decorate_task_description(base, staged=staged)
            manifest = td.environment_manifest
            assert manifest is not None
            assert manifest.network_policy == base_policy
        finally:
            driver.close()


class TestEscapeHatch:
    def test_restores_pre_shield_behavior(
        self, canary_secret_manager: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        adapter = _pack_adapter()
        staged = adapter.stage_task("fix_factorial")
        assert staged is not None
        base = adapter.to_task_description("fix_factorial")
        base_policy = (
            base.environment_manifest.network_policy if base.environment_manifest else None
        )
        with caplog.at_level("WARNING"):
            driver = _driver(agent_harness="claude-code", disable_credential_gateway=True)
            driver.attach("native", True)
        try:
            assert driver.container_env["ANTHROPIC_API_KEY"] == CANARY
            driver.apply_container_layers(staged=staged)
            doc = yaml.safe_load(staged.compose_file.read_text())
            assert "extra_hosts" not in doc["services"]["main"]
            td = driver.decorate_task_description(base, staged=staged)
            manifest = td.environment_manifest
            assert manifest is not None
            assert manifest.network_policy == base_policy
        finally:
            driver.close()
        assert any("credential gateway disabled" in record.message for record in caplog.records)
