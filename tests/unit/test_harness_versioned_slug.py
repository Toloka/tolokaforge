"""Unit tests for ``models.agent.coding_harness`` — the pin override slug.

The registry pins each vendor CLI to a specific version by default
(reproducibility-first). Two shapes let an operator override the pin without
editing the registry:

- ``models.agent.coding_harness: "claude-code@2.2.0"`` — inline slug (ergonomic).
- ``models.agent.coding_harness: "claude-code"`` + ``models.agent.coding_harness_version: "2.2.0"``
  — struct form (visible in a config diff).

A ``model_validator(mode="before")`` on ``ModelConfig`` splits the slug and
populates ``coding_harness_version``; downstream (orchestrator, adapters,
registry lookup) never sees the ``@`` syntax. The same validator also lifts
the pre-rename ``harness`` / ``harness_version`` field names (which shipped
briefly in the same release) with a ``DeprecationWarning`` naming the new
location.
"""

from __future__ import annotations

import warnings

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
        cfg = ModelConfig(**_agent(coding_harness="claude-code"))
        assert cfg.coding_harness == "claude-code"
        assert cfg.coding_harness_version is None

    def test_slug_populates_both_fields(self) -> None:
        cfg = ModelConfig(**_agent(coding_harness="claude-code@2.2.0"))
        assert cfg.coding_harness == "claude-code"
        assert cfg.coding_harness_version == "2.2.0"

    def test_slug_and_struct_agree_no_conflict(self) -> None:
        cfg = ModelConfig(
            **_agent(coding_harness="claude-code@2.2.0", coding_harness_version="2.2.0")
        )
        assert cfg.coding_harness == "claude-code"
        assert cfg.coding_harness_version == "2.2.0"

    def test_slug_and_struct_disagree_raises(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            ModelConfig(
                **_agent(coding_harness="claude-code@2.2.0", coding_harness_version="2.3.0")
            )
        msg = str(excinfo.value)
        assert "conflicts" in msg
        assert "2.2.0" in msg
        assert "2.3.0" in msg

    def test_struct_only_leaves_coding_harness_bare(self) -> None:
        cfg = ModelConfig(**_agent(coding_harness="claude-code", coding_harness_version="2.2.0"))
        assert cfg.coding_harness == "claude-code"
        assert cfg.coding_harness_version == "2.2.0"


class TestEmptySegments:
    def test_empty_name_before_at_raises(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            ModelConfig(**_agent(coding_harness="@2.2.0"))
        assert "empty name" in str(excinfo.value)

    def test_empty_version_after_at_raises(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            ModelConfig(**_agent(coding_harness="claude-code@"))
        assert "empty version" in str(excinfo.value)

    def test_at_only_raises(self) -> None:
        with pytest.raises(ValueError):
            ModelConfig(**_agent(coding_harness="@"))


class TestVersionThreadedThroughRunConfig:
    def test_slug_survives_run_config_parse(self) -> None:
        cfg = RunConfig(
            models={
                "user": {"provider": "openai", "name": "gpt-4o"},
                "agent": {
                    "provider": "openrouter",
                    "name": "openrouter/anthropic/claude-sonnet-4-6",
                    "coding_harness": "claude-code@2.2.0",
                },
            },
            orchestrator={},
            evaluation={"output_dir": "results/x"},
        )
        assert cfg.models["agent"].coding_harness == "claude-code"
        assert cfg.models["agent"].coding_harness_version == "2.2.0"

    def test_struct_survives_run_config_parse(self) -> None:
        cfg = RunConfig(
            models={
                "user": {"provider": "openai", "name": "gpt-4o"},
                "agent": {
                    "provider": "openrouter",
                    "name": "openrouter/anthropic/claude-sonnet-4-6",
                    "coding_harness": "claude-code",
                    "coding_harness_version": "2.2.0",
                },
            },
            orchestrator={},
            evaluation={"output_dir": "results/x"},
        )
        assert cfg.models["agent"].coding_harness == "claude-code"
        assert cfg.models["agent"].coding_harness_version == "2.2.0"


class TestLegacyHarnessFieldLift:
    """Pre-rename ``harness`` / ``harness_version`` field names lift to the
    ``coding_harness`` / ``coding_harness_version`` canonical location with
    a ``DeprecationWarning``. The lift preserves the slug-split path
    downstream."""

    def test_legacy_name_lifts_and_warns(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cfg = ModelConfig(**_agent(harness="claude-code"))
        assert cfg.coding_harness == "claude-code"
        assert any(
            issubclass(w.category, DeprecationWarning) and "models.agent.harness" in str(w.message)
            for w in caught
        )

    def test_legacy_version_lifts_and_warns(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cfg = ModelConfig(**_agent(coding_harness="claude-code", harness_version="2.2.0"))
        assert cfg.coding_harness_version == "2.2.0"
        assert any(
            issubclass(w.category, DeprecationWarning)
            and "models.agent.harness_version" in str(w.message)
            for w in caught
        )

    def test_legacy_and_canonical_disagree_raises(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            ModelConfig(**_agent(harness="codex", coding_harness="claude-code"))
        msg = str(excinfo.value)
        assert "models.agent.harness" in msg
        assert "models.agent.coding_harness" in msg

    def test_legacy_slug_lifts_and_splits(self) -> None:
        # Legacy name lifted first, then the slug split ran on the canonical
        # field.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cfg = ModelConfig(**_agent(harness="claude-code@2.2.0"))
        assert cfg.coding_harness == "claude-code"
        assert cfg.coding_harness_version == "2.2.0"
        assert any(
            issubclass(w.category, DeprecationWarning) and "models.agent.harness" in str(w.message)
            for w in caught
        )


class TestMixinResolverAppliesOverride:
    def test_override_replaces_spec_version(self) -> None:
        from tolokaforge_coding_harnesses import (
            HARNESSES,
            CodingHarnessAdapterMixin,
        )

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
