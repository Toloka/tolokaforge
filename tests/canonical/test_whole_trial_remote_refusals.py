"""``--runtime remote`` refuses the run shapes and settings a node does not carry.

A node rebuilds its run configuration from the trial inputs, which carry the
timeouts, turn and token caps, stuck heuristics, agent loop, log capture and the
agent's fallback chain. Every other run-level setting would be silently dropped,
so the orchestrator refuses each one up front, before any trial is leased, and
names what it refused with the reason. Carried settings, and observability the
node cannot serve, do not refuse.
"""

from __future__ import annotations

import logging
import warnings
from pathlib import Path
from typing import Any

import pytest

from tests.canonical._node_fakes import write_native_pack
from tolokaforge.core.models import (
    DispatchConfig,
    EvaluationConfig,
    GraderConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
)
from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps
from tolokaforge.runner.models import ServiceSpec

pytestmark = [pytest.mark.canonical, pytest.mark.usefixtures("env_backed_secrets")]

_AGENT = ModelConfig(provider="openai", name="gpt-4")
_USER = ModelConfig(provider="openrouter", name="anthropic/claude-sonnet-4.6")


@pytest.fixture(autouse=True)
def _accept_any_agent_loop_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only ``engine-loop`` is registered in-tree; the remote guard is what these tests read."""
    monkeypatch.setattr(Orchestrator, "_refuse_an_unregistered_agent_loop", lambda self: None)


def _orchestrator(
    tmp_path: Path,
    *,
    orchestrator: dict[str, Any] | None = None,
    compute: dict[str, Any] | None = None,
    models: dict[str, ModelConfig] | None = None,
    observability: dict[str, Any] | None = None,
    grader: GraderConfig | None = None,
    deps: OrchestratorDeps | None = None,
    environment_manifest: dict[str, Any] | None = None,
) -> Orchestrator:
    write_native_pack(tmp_path / "pack", environment_manifest=environment_manifest)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        config = RunConfig(
            models=models or {"agent": _AGENT, "user": _USER},
            orchestrator=OrchestratorConfig(
                **{
                    "workers": 1,
                    "repeats": 1,
                    "runtime": "remote",
                    "remote": {"nodes": ["node-a:50061"]},
                    **(orchestrator or {}),
                }
            ),
            evaluation=EvaluationConfig(
                output_dir=str(tmp_path / "out"), projects=[str(tmp_path / "pack")]
            ),
            compute=compute,
            observability=observability,
            grader=grader,
        )
    orch = Orchestrator(config, deps=deps)
    orch.load_tasks()
    return orch


def test_a_default_run_is_not_refused(tmp_path: Path) -> None:
    _orchestrator(tmp_path)._refuse_unsupported_remote_run()


def test_the_settings_the_node_carries_are_not_refused(tmp_path: Path) -> None:
    orch = _orchestrator(
        tmp_path,
        orchestrator={
            "timeouts": {"turn_s": 30, "episode_s": 600},
            "max_turns": 10,
            "max_trial_tokens": 200000,
            "stuck_heuristics": {"max_repeated_tool_calls": 3},
            "agent_loop": "custom-loop",
        },
        compute={"log_tail": 10, "capture_logs_on_success": True},
        models={
            "agent": _AGENT.model_copy(update={"fallbacks": [_USER]}),
            "user": _USER,
        },
    )

    orch._refuse_unsupported_remote_run()


@pytest.mark.parametrize(
    ("kwargs", "named"),
    [
        (
            {"compute": {"max_requests_per_second": 2.0}},
            "a run-level request throttle is global across all trials",
        ),
        (
            {"orchestrator": {"max_requests_per_second": 2.0}},
            "a node runs one trial and cannot enforce a shared limit",
        ),
        (
            {"orchestrator": {"typesense": {"enabled": True}}},
            "a dispatch node has no such bridge",
        ),
    ],
    ids=["compute-throttle", "orchestrator-throttle", "typesense"],
)
def test_a_setting_the_node_cannot_honour_is_refused_with_its_reason(
    tmp_path: Path, kwargs: dict[str, Any], named: str
) -> None:
    orch = _orchestrator(tmp_path, **kwargs)

    with pytest.raises(ValueError, match="does not carry") as raised:
        orch._refuse_unsupported_remote_run()

    assert named in str(raised.value)


def test_every_offending_setting_is_named_in_one_error(tmp_path: Path) -> None:
    orch = _orchestrator(
        tmp_path,
        orchestrator={"typesense": {"enabled": True}},
        compute={"max_requests_per_second": 2.0},
    )

    with pytest.raises(ValueError) as raised:
        orch._refuse_unsupported_remote_run()

    message = str(raised.value)
    assert "max_requests_per_second" in message
    assert "orchestrator.typesense" in message


def test_a_fallback_chain_with_the_rate_limit_probe_is_refused(tmp_path: Path) -> None:
    orch = _orchestrator(
        tmp_path,
        orchestrator={"rate_limit_probe": {"enabled": True}, "timeouts": {"episode_s": 20000}},
        models={"agent": _AGENT.model_copy(update={"fallbacks": [_USER]}), "user": _USER},
    )

    with pytest.raises(ValueError, match="switching models mid-probe"):
        orch._refuse_unsupported_remote_run()


def test_a_custom_conductor_factory_is_refused(tmp_path: Path) -> None:
    orch = _orchestrator(
        tmp_path,
        deps=OrchestratorDeps(conductor_factory=lambda _ctx: None),  # type: ignore[arg-type,return-value]
    )

    with pytest.raises(ValueError, match="custom conductor_factory"):
        orch._refuse_unsupported_remote_run()


def test_a_custom_agent_client_factory_is_refused(tmp_path: Path) -> None:
    orch = _orchestrator(
        tmp_path,
        deps=OrchestratorDeps(agent_client_factory=lambda _config: None),  # type: ignore[arg-type,return-value]
    )

    with pytest.raises(ValueError, match="inject fallbacks via models.agent.fallbacks"):
        orch._refuse_unsupported_remote_run()


def test_a_non_native_task_is_refused(tmp_path: Path) -> None:
    orch = _orchestrator(tmp_path)
    orch.tasks = [task.model_copy(update={"adapter_type": "terminal_bench"}) for task in orch.tasks]

    with pytest.raises(ValueError, match="non-native adapter or coding-harness mode"):
        orch._refuse_unsupported_remote_run()


def test_coding_harness_mode_is_refused(tmp_path: Path) -> None:
    orch = _orchestrator(tmp_path)
    orch.config.models["agent"] = _AGENT.model_copy(update={"harness": "claude-code"})

    with pytest.raises(ValueError, match="non-native adapter or coding-harness mode"):
        orch._refuse_unsupported_remote_run()


_IN_PACK_STACK = {"stack": {"compose_file": "./environment.compose.yaml"}}


def _with_manifest(orch: Orchestrator, **changes: Any) -> Orchestrator:
    """Replace fields of the task's resolved manifest, as a hand-built manifest would carry."""
    task_id = orch.tasks[0].task_id
    description = orch._task_description(task_id)
    assert description.environment_manifest is not None
    manifest = description.environment_manifest.model_copy(update=changes)
    orch._task_desc_cache[task_id] = description.model_copy(
        update={"environment_manifest": manifest}
    )
    return orch


def test_a_single_stack_in_pack_manifest_is_not_refused(tmp_path: Path) -> None:
    orch = _orchestrator(tmp_path, environment_manifest=_IN_PACK_STACK)

    orch._refuse_unsupported_remote_run()


def test_a_multi_stack_manifest_is_refused_by_task(tmp_path: Path) -> None:
    orch = _orchestrator(tmp_path, environment_manifest=_IN_PACK_STACK)
    stack = orch._task_description("flat").environment_manifest.stacks[0]
    _with_manifest(orch, stacks=[stack, stack.model_copy(update={"stack_id": "second"})])

    with pytest.raises(ValueError, match="task 'flat' declares 2 stacks") as raised:
        orch._refuse_unsupported_remote_run()

    assert "shared or per_trial" in str(raised.value)


def test_a_reset_service_is_refused_by_name(tmp_path: Path) -> None:
    orch = _orchestrator(tmp_path, environment_manifest=_IN_PACK_STACK)
    manifest = orch._task_description("flat").environment_manifest
    reset = ServiceSpec.model_construct(isolation="reset", reset=None)
    _with_manifest(orch, services={**manifest.services, "default": reset})

    with pytest.raises(ValueError, match="isolation: reset for default") as raised:
        orch._refuse_unsupported_remote_run()

    assert "task 'flat'" in str(raised.value)
    assert "seeds are not yet shipped to the node" in str(raised.value)


def test_a_compose_file_outside_the_task_directory_is_refused(tmp_path: Path) -> None:
    shared = tmp_path / "shared-compose.yaml"
    shared.write_text("services:\n  default:\n    image: tolokaforge/runner:0.5.0\n")
    orch = _orchestrator(tmp_path, environment_manifest={"stack": {"compose_file": str(shared)}})

    with pytest.raises(ValueError, match="outside its own directory") as raised:
        orch._refuse_unsupported_remote_run()

    assert str(shared) in str(raised.value)
    assert "task 'flat'" in str(raised.value)


def test_a_task_without_a_pack_directory_is_refused(tmp_path: Path) -> None:
    orch = _orchestrator(tmp_path, environment_manifest=_IN_PACK_STACK)
    orch.tasks[0]._source_dir = None

    with pytest.raises(ValueError, match="no task pack directory"):
        orch._refuse_unsupported_remote_run()


def test_the_existing_guards_still_refuse(tmp_path: Path) -> None:
    contradicting = _orchestrator(tmp_path / "a", orchestrator={"auto_start_services": True})
    with pytest.raises(ValueError, match="contradicts orchestrator.auto_start_services"):
        contradicting._refuse_unsupported_remote_run()

    static_grader = _orchestrator(tmp_path / "b", grader=GraderConfig(name="queue"))
    with pytest.raises(ValueError, match="static runner address"):
        static_grader._refuse_unsupported_remote_run()


def test_configured_tracing_warns_that_the_node_emits_no_live_traces(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    orch = _orchestrator(tmp_path, observability={"tracing": {"exporter": "langfuse"}})

    with caplog.at_level(logging.WARNING):
        orch._refuse_unsupported_remote_run()

    assert any("emit no live traces" in record.getMessage() for record in caplog.records)


def test_an_unconfigured_tracing_block_does_not_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    orch = _orchestrator(tmp_path, observability={"tracing": {"exporter": "none"}})

    with caplog.at_level(logging.WARNING):
        orch._refuse_unsupported_remote_run()

    assert not any("emit no live traces" in record.getMessage() for record in caplog.records)


def _local_dispatch_orchestrator(
    tmp_path: Path, *, orchestrator: dict[str, Any] | None = None
) -> Orchestrator:
    """An orchestrator whose dispatch block selects the in-process ``local`` adapter."""
    write_native_pack(tmp_path / "pack")
    config = RunConfig(
        models={"agent": _AGENT, "user": _USER},
        orchestrator=OrchestratorConfig(
            **{
                "workers": 1,
                "repeats": 1,
                "dispatch": DispatchConfig(adapter="local", config={"mode": "thread"}),
                **(orchestrator or {}),
            }
        ),
        evaluation=EvaluationConfig(
            output_dir=str(tmp_path / "out"), projects=[str(tmp_path / "pack")]
        ),
    )
    orch = Orchestrator(config)
    orch.load_tasks()
    return orch


def test_a_local_dispatch_run_routes_to_the_shared_dispatch_refusals(tmp_path: Path) -> None:
    """The local adapter is a dispatch run but not grpc_node, so it is gated on
    the shared refusals, not the grpc_node-only path."""
    orch = _local_dispatch_orchestrator(tmp_path)
    assert orch._is_dispatch_run() is True
    assert orch._dispatches_via_grpc_node() is False


def test_a_local_dispatch_run_refuses_a_setting_the_node_does_not_carry(tmp_path: Path) -> None:
    """A local dispatch run drops the global throttle just as a remote node does,
    so it is refused up front instead of being silently ignored."""
    orch = _local_dispatch_orchestrator(tmp_path, orchestrator={"max_requests_per_second": 2.0})

    with pytest.raises(ValueError, match="does not carry") as raised:
        orch._refuse_unsupported_dispatch_run()

    assert "max_requests_per_second" in str(raised.value)
