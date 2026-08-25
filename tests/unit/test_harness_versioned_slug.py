"""Unit tests for the ``models.agent.harness = "<name>@<version>"`` slug shape.

The registry pins each harness's CLI version by default (reproducibility-first).
An operator override rides one of two locations, sharing a single field:

- ``models.agent.harness: "claude-code@2.2.0"`` — inline slug (ergonomic).
- ``models.agent.harness: "claude-code"`` + ``models.agent.harness_version: "2.2.0"``
  — struct form (visible in a config diff).

A ``model_validator(mode="before")`` on ``ModelConfig`` splits the slug and
populates ``harness_version``; downstream (orchestrator, adapters, registry
lookup) never sees the ``@`` syntax. This suite pins the split, the collision
policy, and the empty-segment refusals — plus the end-to-end propagation
through the orchestrator's adapter params.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.models import RunConfig
from tolokaforge.core.models.model_config import ModelConfig

pytestmark = pytest.mark.unit


def _agent(**overrides) -> dict:
    """Minimal valid ``ModelConfig`` kwargs for the agent role."""
    return {
        "provider": "openrouter",
        "name": "openrouter/anthropic/claude-sonnet-4-6",
        **overrides,
    }


class TestSlugSplit:
    def test_bare_name_leaves_version_none(self) -> None:
        cfg = ModelConfig(**_agent(harness="claude-code"))
        assert cfg.harness == "claude-code"
        assert cfg.harness_version is None

    def test_slug_populates_both_fields(self) -> None:
        cfg = ModelConfig(**_agent(harness="claude-code@2.2.0"))
        assert cfg.harness == "claude-code"
        assert cfg.harness_version == "2.2.0"

    def test_slug_and_struct_agree_no_conflict(self) -> None:
        # The pre-validator only writes ``harness_version`` when the slug
        # carries ``@``; a user setting both to the same value on parse is
        # fine (equal-value dedup semantics match the alias lift).
        cfg = ModelConfig(**_agent(harness="claude-code@2.2.0", harness_version="2.2.0"))
        assert cfg.harness == "claude-code"
        assert cfg.harness_version == "2.2.0"

    def test_slug_and_struct_disagree_raises(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            ModelConfig(**_agent(harness="claude-code@2.2.0", harness_version="2.3.0"))
        msg = str(excinfo.value)
        assert "conflicts" in msg
        assert "2.2.0" in msg
        assert "2.3.0" in msg

    def test_struct_only_leaves_harness_bare(self) -> None:
        cfg = ModelConfig(**_agent(harness="claude-code", harness_version="2.2.0"))
        assert cfg.harness == "claude-code"
        assert cfg.harness_version == "2.2.0"


class TestEmptySegments:
    def test_empty_name_before_at_raises(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            ModelConfig(**_agent(harness="@2.2.0"))
        assert "empty name" in str(excinfo.value)

    def test_empty_version_after_at_raises(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            ModelConfig(**_agent(harness="claude-code@"))
        assert "empty version" in str(excinfo.value)

    def test_at_only_raises(self) -> None:
        with pytest.raises(ValueError):
            ModelConfig(**_agent(harness="@"))


class TestVersionThreadedThroughRunConfig:
    """The orchestrator injects ``models.agent.harness_version`` into adapter
    params as ``agent_harness_version`` when the ``models.agent.harness`` is
    set. This test locks the run-config-side surface — the orchestrator part
    is exercised via the integration path in test_orchestrator_docker_cli_detection."""

    def test_slug_survives_run_config_parse(self) -> None:
        cfg = RunConfig(
            models={
                "user": {"provider": "openai", "name": "gpt-4o"},
                "agent": {
                    "provider": "openrouter",
                    "name": "openrouter/anthropic/claude-sonnet-4-6",
                    "harness": "claude-code@2.2.0",
                },
            },
            orchestrator={},
            evaluation={"output_dir": "results/x"},
        )
        assert cfg.models["agent"].harness == "claude-code"
        assert cfg.models["agent"].harness_version == "2.2.0"

    def test_struct_survives_run_config_parse(self) -> None:
        cfg = RunConfig(
            models={
                "user": {"provider": "openai", "name": "gpt-4o"},
                "agent": {
                    "provider": "openrouter",
                    "name": "openrouter/anthropic/claude-sonnet-4-6",
                    "harness": "claude-code",
                    "harness_version": "2.2.0",
                },
            },
            orchestrator={},
            evaluation={"output_dir": "results/x"},
        )
        assert cfg.models["agent"].harness == "claude-code"
        assert cfg.models["agent"].harness_version == "2.2.0"


class TestMixinResolverAppliesOverride:
    """The ``CodingHarnessAdapterMixin.resolve_harness_spec(version_override=…)``
    call site returns a spec whose ``version`` is model-copied. This gives
    third-party adapters a boundary-clean way to honour the override without
    reaching into ``HarnessSpec.model_copy`` themselves."""

    def test_override_replaces_spec_version(self) -> None:
        from tolokaforge_coding_harnesses import (
            HARNESSES,
            CodingHarnessAdapterMixin,
        )

        # A shipped harness — pinned version comes from the registry data.
        shipped_pin = HARNESSES["claude-code"].version
        assert shipped_pin != "9.9.9-fake"

        class _Adapter(CodingHarnessAdapterMixin):
            pass

        adapter = _Adapter()
        overridden = adapter.resolve_harness_spec(
            "claude-code",
            "openrouter/anthropic/claude-sonnet-4-6",
            version_override="9.9.9-fake",
        )
        assert overridden.version == "9.9.9-fake"
        # Every other field survives.
        assert overridden.install_source == HARNESSES["claude-code"].install_source
        assert overridden.install_method == HARNESSES["claude-code"].install_method

    def test_missing_override_leaves_shipped_pin(self) -> None:
        from tolokaforge_coding_harnesses import (
            HARNESSES,
            CodingHarnessAdapterMixin,
        )

        class _Adapter(CodingHarnessAdapterMixin):
            pass

        adapter = _Adapter()
        spec = adapter.resolve_harness_spec(
            "claude-code",
            "openrouter/anthropic/claude-sonnet-4-6",
        )
        assert spec.version == HARNESSES["claude-code"].version

    def test_empty_override_raises(self) -> None:
        from tolokaforge_coding_harnesses import CodingHarnessAdapterMixin

        class _Adapter(CodingHarnessAdapterMixin):
            pass

        adapter = _Adapter()
        with pytest.raises(ValueError) as excinfo:
            adapter.resolve_harness_spec(
                "claude-code",
                "openrouter/anthropic/claude-sonnet-4-6",
                version_override="",
            )
        assert "version_override" in str(excinfo.value)
