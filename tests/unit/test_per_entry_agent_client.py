"""Per-entry agent clients for an engine-loop multi-harness run.

Two layers, each tested below the composite-build guard:

- ``Orchestrator._build_agent_clients_by_entry`` builds one client per entry
  whose effective (entry-over-run) agent model differs from the run-level one,
  routed through the ``_build_agent_client`` seam. Equal / non-overriding
  entries are absent (they reuse the run-level client); two entries resolving
  to the same model share one client.
- ``InProcessConductor._agent_client_for`` returns the entry's mapped client,
  or the run-level ``agent_client`` for the empty-string / unmapped entry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.canonical._factories import make_env_endpoints, make_task_description
from tolokaforge.core.conductor import InProcessConductor
from tolokaforge.core.execution_mode import select_execution_mode
from tolokaforge.core.llm import LLMClient
from tolokaforge.core.logging import get_logger
from tolokaforge.core.models import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
)
from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
from tolokaforge.core.trial import TrialSpec

pytestmark = pytest.mark.unit

_RUN_AGENT = ModelConfig(provider="openai", name="gpt-4")
_MODEL_A = ModelConfig(provider="openai", name="gpt-5")
_MODEL_B = ModelConfig(provider="anthropic", name="claude-3-7")


class _RecordingFactory:
    """Agent-client factory that records each requested ``ModelConfig``.

    Returns a real :class:`LLMClient` per call so the resulting map holds
    distinct instances whose identity and ``config`` are both assertable;
    identity across entries then proves the dedup, not a mock call count.
    """

    def __init__(self) -> None:
        self.requested: list[ModelConfig] = []

    def __call__(self, config: ModelConfig) -> LLMClient:
        self.requested.append(config)
        return LLMClient(config)


def _harness_config(entries: list[dict[str, object]]) -> RunConfig:
    return RunConfig(
        models={"agent": _RUN_AGENT},
        orchestrator=OrchestratorConfig(),
        evaluation=EvaluationConfig(output_dir="/tmp/per-entry", tasks_glob="tasks/**/task.yaml"),
        harnesses={"entries": entries},
    )


def _orchestrator(config: RunConfig, factory: _RecordingFactory) -> Orchestrator:
    # ``OrchestratorDeps.agent_client_factory`` is a typed callable; the
    # recording stand-in satisfies it structurally.
    return Orchestrator(
        config,
        deps=OrchestratorDeps(agent_client_factory=factory),  # type: ignore[arg-type]
    )


class TestBuildAgentClientsByEntry:
    def test_distinct_clients_for_differing_entries_dedup_and_reuse(self) -> None:
        config = _harness_config(
            [
                # effective agent == run-level: reuses the run-level client.
                {"name": "same", "adapter": "native", "model": {"agent": _RUN_AGENT}},
                # no model override at all: also the run-level agent.
                {"name": "plain", "adapter": "native"},
                # two entries on the SAME differing model: dedup to one client.
                {"name": "diffA", "adapter": "native", "model": {"agent": _MODEL_A}},
                {"name": "diffA2", "adapter": "native", "model": {"agent": _MODEL_A}},
                # a third, distinct differing model.
                {"name": "diffB", "adapter": "native", "model": {"agent": _MODEL_B}},
            ]
        )
        factory = _RecordingFactory()
        orch = _orchestrator(config, factory)

        by_entry = orch._build_agent_clients_by_entry()

        # Only differing entries are present; equal / non-overriding reuse the
        # run-level client and stay out of the map.
        assert set(by_entry) == {"diffA", "diffA2", "diffB"}
        # Dedup: the two same-model entries share one instance; the distinct
        # model gets its own.
        assert by_entry["diffA"] is by_entry["diffA2"]
        assert by_entry["diffA"] is not by_entry["diffB"]
        # The seam was asked for exactly the two distinct differing models.
        assert factory.requested == [_MODEL_A, _MODEL_B]
        # Each built client carries its resolved per-entry model.
        assert by_entry["diffA"].config == _MODEL_A
        assert by_entry["diffB"].config == _MODEL_B

    def test_single_adapter_run_has_no_per_entry_clients(self) -> None:
        config = RunConfig(
            models={"agent": _RUN_AGENT},
            orchestrator=OrchestratorConfig(),
            evaluation=EvaluationConfig(output_dir="/tmp/single"),
        )
        factory = _RecordingFactory()
        orch = _orchestrator(config, factory)

        assert orch._build_agent_clients_by_entry() == {}
        assert factory.requested == []


def _conductor(
    agent_client: LLMClient, agent_clients_by_entry: dict[str, LLMClient]
) -> InProcessConductor:
    return InProcessConductor(
        adapter=None,  # type: ignore[arg-type]
        artifact_writer=None,  # type: ignore[arg-type]
        config=RunConfig(
            models={"agent": _RUN_AGENT},
            orchestrator=OrchestratorConfig(),
            evaluation=EvaluationConfig(output_dir="/tmp/x"),
        ),
        logger=get_logger("test-per-entry-conductor"),
        agent_client=agent_client,
        agent_clients_by_entry=agent_clients_by_entry,
        runtime_backend=None,  # type: ignore[arg-type]
        trial_grader=None,  # type: ignore[arg-type]
        output_dir=Path("/tmp/x"),
    )


def _spec(entry: str) -> TrialSpec:
    task_desc = make_task_description(task_id="t")
    return TrialSpec(
        trial_id="t:0",
        run_id="run-1",
        entry=entry,
        task=task_desc,
        execution_mode=select_execution_mode(task_desc.metadata),
        agent_model_config=_RUN_AGENT,
        env_endpoints=make_env_endpoints(),
    )


class TestAgentClientFor:
    def test_mapped_entry_gets_its_client_others_fall_back(self) -> None:
        run_level = LLMClient(_RUN_AGENT)
        entry_client = LLMClient(_MODEL_A)
        conductor = _conductor(run_level, {"alpha": entry_client})

        # Mapped entry resolves to its own client.
        assert conductor._agent_client_for(_spec(entry="alpha")) is entry_client
        # The empty-string (single-adapter) entry reuses the run-level client.
        assert conductor._agent_client_for(_spec(entry="")) is run_level
        # An unmapped entry also reuses the run-level client.
        assert conductor._agent_client_for(_spec(entry="beta")) is run_level

    def test_empty_map_always_reuses_the_run_level_client(self) -> None:
        run_level = LLMClient(_RUN_AGENT)
        conductor = _conductor(run_level, {})

        assert conductor._agent_client_for(_spec(entry="")) is run_level
        assert conductor._agent_client_for(_spec(entry="anything")) is run_level
