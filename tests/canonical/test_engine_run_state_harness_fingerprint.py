"""The terminal-bench self-report reaches ``engine_run_state.json``.

The unit tier pins what the fingerprint says; this pins that a real run
records it. A prepared run with the terminal-bench adapter in harness mode
carries, under ``adapter_fingerprints["terminal_bench"]["harness"]``, exactly
what the adapter's own seam returns — so nothing between the adapter and the
file re-derives, trims, or reshapes the payload.

Canonical tier: the in-memory runtime + conductor keep ``prepare_run``
Docker-free and LLM-free.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.core.conductor import InMemoryConductor
from tolokaforge.core.engine_run_state import read_engine_run_state
from tolokaforge.core.models import (
    EvaluationConfig,
    HarnessAdapterConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
)
from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
from tolokaforge.core.runtime import InMemoryRuntimeBackend

pytestmark = [pytest.mark.canonical, pytest.mark.usefixtures("env_backed_secrets")]

_TASKS_DIR = Path(__file__).parent.parent / "data" / "terminal_bench_tasks"


def test_prepare_run_records_the_adapter_harness_fingerprint(tmp_path: Path) -> None:
    output_dir = tmp_path / "harness-fingerprint-lock"
    config = RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4")},
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(
            output_dir=str(output_dir),
            harness_adapter=HarnessAdapterConfig(
                type="terminal_bench",
                params={
                    "terminal_bench_dir": str(_TASKS_DIR),
                    "staging_root": str(tmp_path / "staging"),
                    "task_ids": ["echo-hello"],
                    "agent_harness": "claude-code",
                    "agent_model": "openrouter/anthropic/claude-sonnet-4-6",
                    "prebuild_images": False,
                },
            ),
        ),
    )
    orch = Orchestrator(
        config,
        deps=OrchestratorDeps(
            runtime_backend=InMemoryRuntimeBackend(),
            conductor_factory=lambda _ctx: InMemoryConductor(),
        ),
    )
    orch.prepare_run(output_dir)

    persisted = read_engine_run_state(output_dir)["adapter_fingerprints"]
    assert persisted == {"terminal_bench": orch.adapter.fingerprint()}

    harness = persisted["terminal_bench"]["harness"]
    assert harness["agent_harness"] == "claude-code"
    assert harness["resolved_sha256"] == harness["shipped_sha256"]
    assert harness["overlay_file"] is None
    assert harness["plugin_bundles"] == []
