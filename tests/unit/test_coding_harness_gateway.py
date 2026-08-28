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
    def test_rewritten_compose_never_contains_the_canary(self, canary_secret_manager: None) -> None:
        adapter = _pack_adapter()
        staged = adapter.stage_task("fix_factorial")
        assert staged is not None
        driver = _driver()
        driver.attach("native", True)
        try:
            driver.apply_container_layers(staged=staged)
            compose_text = staged.compose_file.read_text()
            assert CANARY not in compose_text
            assert "sk-tolokaforge-shielded-dummy-not-a-real-key" in compose_text
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


class TestExtraHosts:
    def test_added_under_a_gateway_active_harness(self, canary_secret_manager: None) -> None:
        adapter = _pack_adapter()
        staged = adapter.stage_task("fix_factorial")
        assert staged is not None
        driver = _driver(agent_harness="claude-code")
        driver.attach("native", True)
        try:
            driver.apply_container_layers(staged=staged)
            doc = yaml.safe_load(staged.compose_file.read_text())
            extra_hosts = doc["services"]["main"]["extra_hosts"]
            assert extra_hosts == ["tolokaforge-llm-gateway:host-gateway"]
        finally:
            driver.close()

    def test_not_added_when_credential_gateway_is_none(self, canary_secret_manager: None) -> None:
        adapter = _pack_adapter()
        staged = adapter.stage_task("fix_factorial")
        assert staged is not None
        driver = _driver(agent_harness="gemini-cli")
        driver.attach("native", True)
        try:
            driver.apply_container_layers(staged=staged)
            doc = yaml.safe_load(staged.compose_file.read_text())
            assert "extra_hosts" not in doc["services"]["main"]
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
    """Local-mode host-side gateway is unreachable from a docker
    ``internal: true`` network — the CLI's container has no route to the
    orchestrator host. When the shield is active AND the pack declares
    ``NO_INTERNET``, the driver auto-elevates to ``FULL_INTERNET`` so the
    gateway is reachable; a loud warning names the elevation and points at
    the escape hatch (``disable_credential_gateway``). The reserved
    ``SidecarGatewayLauncher`` (ADR-0041) will make ``NO_INTERNET``
    compatible with the shield in a follow-up. Unshielded harnesses and
    packs that already declare ``FULL_INTERNET`` or ``LIMITED_INTERNET``
    are left untouched."""

    def test_shielded_no_internet_pack_is_elevated_to_full_internet(
        self, canary_secret_manager: None, caplog: pytest.LogCaptureFixture
    ) -> None:
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
            with caplog.at_level("WARNING"):
                td = driver.decorate_task_description(base, staged=staged)
            manifest = td.environment_manifest
            assert manifest is not None
            assert manifest.network_policy is NetworkPolicy.FULL_INTERNET
            assert any(
                "network_policy=no_internet" in record.message
                and "Elevating" in record.message
                and "disable_credential_gateway" in record.message
                for record in caplog.records
            ), [r.message for r in caplog.records]
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
