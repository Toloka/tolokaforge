"""Every shipped run config loads through the run path and declares only model-config keys.

The sweep covers every ``examples/**/*.yaml`` whose top level declares ``models:``.
Each one is merged with its project's ``run_defaults`` and built into a
:class:`RunConfig` the way ``tolokaforge run`` builds it. Each raw
``models.<role>`` block — its ``fallbacks``, ``openrouter``, ``session`` and
``reasoning`` included — must carry only keys the matching type declares, so an
example never teaches a key the engine does not read.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from tolokaforge.core.llm.reasoning import ReasoningConfig
from tolokaforge.core.models import ModelConfig, ModelSessionConfig, OpenRouterConfig, RunConfig
from tolokaforge.core.project_loader import construct_config, load_effective_run_config

pytestmark = pytest.mark.canonical

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLES_DIR = _REPO_ROOT / "examples"
_SHIPPED_RUN_CONFIGS_AT_LEAST = 48

_NESTED_BLOCK_FIELDS: dict[str, frozenset[str]] = {
    "openrouter": frozenset(OpenRouterConfig.model_fields),
    "session": frozenset(ModelSessionConfig.model_fields),
    "reasoning": frozenset(field.name for field in dataclasses.fields(ReasoningConfig)),
}


def _example_run_configs() -> list[Path]:
    configs = []
    for path in sorted(_EXAMPLES_DIR.rglob("*.yaml")):
        document = yaml.safe_load(path.read_text())
        if isinstance(document, dict) and "models" in document:
            configs.append(path)
    return configs


def _undeclared_keys(block: dict[str, Any], where: str) -> Iterator[str]:
    for key in sorted(set(block) - set(ModelConfig.model_fields), key=str):
        yield f"{where}.{key}"
    for name, declared in _NESTED_BLOCK_FIELDS.items():
        nested = block.get(name)
        if isinstance(nested, dict):
            for key in sorted(set(nested) - declared, key=str):
                yield f"{where}.{name}.{key}"
    for index, fallback in enumerate(block.get("fallbacks") or []):
        yield from _undeclared_keys(fallback, f"{where}.fallbacks.{index}")


def test_the_sweep_finds_every_shipped_run_config() -> None:
    assert len(_example_run_configs()) >= _SHIPPED_RUN_CONFIGS_AT_LEAST


@pytest.mark.parametrize(
    "config_path",
    _example_run_configs(),
    ids=lambda path: str(path.relative_to(_EXAMPLES_DIR)),
)
def test_a_shipped_run_config_loads_and_declares_only_model_config_keys(config_path: Path) -> None:
    merged, _ = load_effective_run_config(config_path)

    undeclared = [
        path
        for role, block in merged["models"].items()
        for path in _undeclared_keys(block, f"models.{role}")
    ]

    assert undeclared == [], f"{config_path.relative_to(_REPO_ROOT)} carries {undeclared}"
    construct_config(RunConfig, merged, source=config_path)
