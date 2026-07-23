"""Orchestrator-level integration: overlay-defined presets flow through the
same plumbing the per-trial output uses to stamp resolved policies into
``task.yaml`` / metrics.

These tests exercise the in-process integration boundary — config YAML →
overlay activation → ``build_capabilities`` → ``_build_resolved_block`` —
without subprocesses, Docker, or LLM credentials. They prove that when an
operator hands in a run-config with ``engine.presets_file``, the overlay's
preset actually drives the runtime policy resolution that downstream
analytics consume.

What this catches that the unit tests don't: a future change that wires the
overlay into the loader but forgets to thread it through ``Orchestrator``
or the resolved-block helper would still pass the unit tests but fail
here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tolokaforge.core.conductor import _build_resolved_block
from tolokaforge.core.llm import build_capabilities
from tolokaforge.core.llm.presets import (
    resolve_effective_preset,
    resolve_policy_names,
)
from tolokaforge.core.models import EngineConfig, ModelConfig, RunConfig
from tolokaforge.dx.cli.main import _activate_presets_overlay

pytestmark = pytest.mark.integration


def _write_yaml(path: Path, data: dict) -> str:
    path.write_text(yaml.safe_dump(data))
    return str(path)


def _minimal_run_config_yaml(
    tmp_path: Path,
    *,
    agent_model_name: str,
    agent_provider: str,
    presets_file: str | None = None,
) -> Path:
    """Write a minimal ``run.yaml`` that ``RunConfig(**yaml.safe_load(...))``
    can ingest. Mirrors the on-disk shape operators actually use."""
    cfg = {
        "models": {
            "agent": {
                "provider": agent_provider,
                "name": agent_model_name,
                "temperature": 0.0,
            }
        },
        "orchestrator": {"workers": 1, "repeats": 1, "max_turns": 4},
        "evaluation": {"output_dir": str(tmp_path / "results")},
    }
    if presets_file is not None:
        cfg["engine"] = {"presets_file": presets_file}
    p = tmp_path / "run.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return p


class TestOverlayDrivesBuildCapabilities:
    """The public ``build_capabilities()`` is what the orchestrator (and
    LLMClient) calls per trial. An overlay-defined preset must surface there.
    """

    def test_overlay_preset_drives_resolved_capabilities(
        self, tmp_path: Path, overlay_isolation
    ) -> None:
        overlay_path = _write_yaml(
            tmp_path / "overlay.yaml",
            {
                "presets": {
                    "lab_response_override": {
                        "match": ["lab/custom-model*"],
                        "response_policy": "array_dict_map",
                        "reasoning_codec": "openai",
                    }
                }
            },
        )
        # Run-config has engine.presets_file pointing at the overlay.
        run_config_path = _minimal_run_config_yaml(
            tmp_path,
            agent_model_name="lab/custom-model-v1",
            agent_provider="openrouter",
            presets_file=overlay_path,
        )
        # Re-load through the same path the CLI uses.
        with open(run_config_path) as f:
            run_config = RunConfig(**yaml.safe_load(f))
        resolved = _activate_presets_overlay(
            cli_presets_file=None, run_config=run_config, run_dir=None
        )
        assert resolved == overlay_path

        # The public capability builder must see the overlay's preset.
        agent_config = run_config.models["agent"]
        caps = build_capabilities(agent_config.name, agent_config.provider)
        names = resolve_policy_names(caps)
        assert names["response_policy"] == "array_dict_map"
        assert names["reasoning_codec"] == "openai"
        assert resolve_effective_preset(agent_config.name, agent_config.provider) == (
            "lab_response_override"
        )


class TestOverlayResolvedBlock:
    """The orchestrator's ``_build_resolved_block`` is what writes the
    per-trial preset fingerprint into ``task.yaml`` (Stage 7 contract).
    Analytics tools diff this block to detect capability drift across runs.
    An overlay-defined preset MUST surface in this block — otherwise
    operators can't tell from the output which preset actually ran.
    """

    def test_resolved_block_records_overlay_preset_name(
        self, tmp_path: Path, overlay_isolation
    ) -> None:
        overlay_path = _write_yaml(
            tmp_path / "overlay.yaml",
            {
                "presets": {
                    "lab_v2_preset": {
                        "match": ["lab-v2/*"],
                        "response_policy": "json_coerce",
                    }
                }
            },
        )
        run_config = RunConfig(
            models={
                "agent": ModelConfig(
                    provider="openrouter",
                    name="lab-v2/some-model",
                    temperature=0.0,
                ),
            },
            orchestrator={"workers": 1},
            evaluation={"output_dir": str(tmp_path / "results")},
            engine=EngineConfig(presets_file=overlay_path),
        )
        _activate_presets_overlay(cli_presets_file=None, run_config=run_config, run_dir=None)

        block = _build_resolved_block(run_config.models["agent"])
        assert block["effective_preset"] == "lab_v2_preset"
        assert block["response_policy"] == "json_coerce"

    def test_resolved_block_for_bundled_model_unchanged_when_overlay_does_not_match(
        self, tmp_path: Path, overlay_isolation
    ) -> None:
        """An overlay that only adds new presets must not perturb bundled
        models' resolved blocks. This is the no-behaviour-change invariant
        for unrelated models when an operator ships an additive overlay.
        """
        overlay_path = _write_yaml(
            tmp_path / "overlay.yaml",
            {
                "presets": {
                    "unrelated_overlay_preset": {
                        "match": ["totally-different/*"],
                        "response_policy": "array_dict_map",
                    }
                }
            },
        )
        run_config = RunConfig(
            models={
                "agent": ModelConfig(
                    provider="openrouter",
                    name="anthropic/claude-opus-4.8",
                    temperature=0.0,
                ),
            },
            orchestrator={"workers": 1},
            evaluation={"output_dir": str(tmp_path / "results")},
            engine=EngineConfig(presets_file=overlay_path),
        )

        # Capture the bundled resolved block with no overlay first.
        from tolokaforge.core.llm.presets import set_overlay_path

        set_overlay_path(None)
        bundled_block = _build_resolved_block(run_config.models["agent"])

        # Now install the overlay and re-evaluate.
        _activate_presets_overlay(None, run_config, run_dir=None)
        overlaid_block = _build_resolved_block(run_config.models["agent"])

        assert overlaid_block == bundled_block, (
            "Overlay that adds an unrelated preset must not change a bundled "
            "model's resolved block — this is the no-behaviour-change "
            "invariant."
        )


class TestOverlayShadowFlowsThroughOrchestratorHelpers:
    """Shadowing a bundled preset via an overlay must surface in
    ``_build_resolved_block`` — operators rely on this to confirm an
    ablation actually took effect.
    """

    def test_overlay_shadow_changes_resolved_block(self, tmp_path: Path, overlay_isolation) -> None:
        # Overlay a preset that matches a bundled model name (claude-opus-4.8)
        # with an earlier match → first-match-wins gives the overlay priority.
        overlay_path = _write_yaml(
            tmp_path / "overlay.yaml",
            {
                "presets": {
                    "ablation_no_cache": {
                        "match": ["anthropic/claude-opus-4.8*"],
                        "cache_policy": "none",
                        "content_policy": "anthropic",
                    }
                }
            },
        )
        run_config = RunConfig(
            models={
                "agent": ModelConfig(
                    provider="anthropic",
                    name="anthropic/claude-opus-4.8",
                    temperature=0.0,
                ),
            },
            orchestrator={"workers": 1},
            evaluation={"output_dir": str(tmp_path / "results")},
            engine=EngineConfig(presets_file=overlay_path),
        )
        _activate_presets_overlay(None, run_config, run_dir=None)

        block = _build_resolved_block(run_config.models["agent"])
        assert block["effective_preset"] == "ablation_no_cache"
        assert block["cache_policy"] == "none"


class TestCLIPrecedenceFlowsThroughOrchestratorHelpers:
    """An overlay supplied via ``--presets-file`` (highest precedence) must
    beat ``engine.presets_file`` (lowest), and the orchestrator's resolved
    block must reflect the winner.
    """

    def test_cli_overlay_beats_engine_config_overlay(
        self, tmp_path: Path, overlay_isolation
    ) -> None:
        config_overlay = _write_yaml(
            tmp_path / "config_overlay.yaml",
            {
                "presets": {
                    "from_config": {
                        "match": ["mixed/*"],
                        "response_policy": "json_coerce",
                    }
                }
            },
        )
        cli_overlay = _write_yaml(
            tmp_path / "cli_overlay.yaml",
            {
                "presets": {
                    "from_cli": {
                        "match": ["mixed/*"],
                        "response_policy": "array_dict_map",
                    }
                }
            },
        )
        run_config = RunConfig(
            models={
                "agent": ModelConfig(
                    provider="openrouter",
                    name="mixed/some-model",
                    temperature=0.0,
                ),
            },
            orchestrator={"workers": 1},
            evaluation={"output_dir": str(tmp_path / "results")},
            engine=EngineConfig(presets_file=config_overlay),
        )
        _activate_presets_overlay(
            cli_presets_file=cli_overlay,
            run_config=run_config,
            run_dir=None,
        )

        block = _build_resolved_block(run_config.models["agent"])
        assert block["effective_preset"] == "from_cli"
        assert block["response_policy"] == "array_dict_map"
