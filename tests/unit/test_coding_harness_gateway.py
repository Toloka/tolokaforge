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
    """Local-mode host-side gateway is incompatible with docker
    ``internal: true`` networks (the CLI's container would have no route to
    the orchestrator host, defeating the whole shield). Egress restriction is
    a follow-up that requires the ``SidecarGatewayLauncher`` variant tracked
    by ADR-0041. Until then, the driver preserves the pack's own declared
    ``network_policy`` unchanged whether the harness is shielded or not."""

    def test_shielded_harness_preserves_pack_declared_network_policy(
        self, canary_secret_manager: None
    ) -> None:
        adapter = _pack_adapter()
        staged = adapter.stage_task("fix_factorial")
        assert staged is not None
        base = adapter.to_task_description("fix_factorial")
        base_policy = (
            base.environment_manifest.network_policy if base.environment_manifest else None
        )
        base_allowlist = list(
            base.environment_manifest.limited_internet_allowlist
            if base.environment_manifest
            else []
        )
        driver = _driver(agent_harness="claude-code")
        driver.attach("native", True)
        try:
            td = driver.decorate_task_description(base, staged=staged)
            manifest = td.environment_manifest
            assert manifest is not None
            assert manifest.network_policy == base_policy
            assert list(manifest.limited_internet_allowlist) == base_allowlist
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
            assert driver._gateway_handle is None
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
