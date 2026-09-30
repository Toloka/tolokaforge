"""`config validate` and the run find the same `litellm_models` entry for a config."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from tolokaforge.core.config_validator import Severity, validate_run_config
from tolokaforge.core.llm.client import LLMClient
from tolokaforge.core.llm.presets import set_overlay_path
from tolokaforge.core.models.run_config import ModelConfig
from tolokaforge.dx.cli.main import cli

pytestmark = pytest.mark.unit

EVIDENCE = "2026-09-30, litellm 1.93.0: no entry, so the route refused tools before sending"

#: `(provider, name)` as a config states them. The names are absent from
#: litellm's map by construction; the first two carry a `/` that is not the
#: provider, which is where the two sides of the lookup used to part ways.
CONFIGS = [
    ("openai", "self-hosted/tolokaforge-canary-a"),
    ("openrouter", "fake-vendor-xyz/tolokaforge-canary-b"),
    ("meta", "tolokaforge-canary-c"),
    ("nova", "tolokaforge-canary-d"),
]


def _overlay(tmp_path: Path) -> Path:
    path = tmp_path / "overlay.yaml"
    entries = {
        f"{provider}/{name}": {"supports_function_calling": True, "evidence": EVIDENCE}
        for provider, name in CONFIGS
    }
    path.write_text(yaml.safe_dump({"litellm_models": entries}))
    return path


def _run_config(provider: str, name: str, overlay: Path | None = None) -> dict:
    raw: dict = {
        "evaluation": {
            "tasks_glob": "tasks/**/task.yaml",
            "output_dir": "output",
            "harness_adapter": {"type": "frozen_mcp_core"},
        },
        "orchestrator": {"workers": 1, "repeats": 1},
        "models": {
            "agent": {"provider": provider, "name": name},
            "user": {"provider": "openrouter", "name": "anthropic/claude-sonnet-4.6"},
        },
    }
    if overlay is not None:
        raw["engine"] = {"presets_file": str(overlay)}
    return raw


@pytest.mark.parametrize("provider, name", CONFIGS)
def test_the_entry_keyed_provider_slash_name_satisfies_the_preflight_and_the_run(
    provider, name, tmp_path
):
    overlay = _overlay(tmp_path)
    config = tmp_path / "run.yaml"
    config.write_text(yaml.safe_dump(_run_config(provider, name, overlay)))

    result = CliRunner().invoke(cli, ["config", "validate", "--config", str(config)])

    assert "Preset overlay OK" in result.output, result.output
    assert "cannot confirm function-calling support" not in result.output, result.output
    assert "does not appear to support function calling" not in result.output, result.output

    set_overlay_path(str(overlay))
    client = LLMClient(ModelConfig(provider=provider, name=name))
    assert client.allowed_openai_params == ["tools", "tool_choice", "parallel_tool_calls"]


@pytest.mark.parametrize(
    "provider, name, key",
    [
        ("openai", "self-hosted/tolokaforge-canary-a", "openai/self-hosted/tolokaforge-canary-a"),
        (
            "openrouter",
            "openrouter/fake-vendor-xyz/tolokaforge-canary-b",
            "openrouter/fake-vendor-xyz/tolokaforge-canary-b",
        ),
        ("Meta", "tolokaforge-canary-c", "meta/tolokaforge-canary-c"),
        ("nova", "tolokaforge-canary-d", "nova/tolokaforge-canary-d"),
    ],
)
def test_the_undeclared_hint_names_the_key_the_lookup_uses(provider, name, key):
    set_overlay_path(None)

    result = validate_run_config(_run_config(provider, name))

    hints = [
        i.hint
        for i in result.issues
        if i.severity == Severity.INFO
        and i.path == "models.agent.name"
        and "cannot confirm function-calling support" in i.message
    ]
    assert hints == [
        "If the run needs tools, declare it in the presets overlay: "
        f"litellm_models.{key} with supports_function_calling: true"
    ]
