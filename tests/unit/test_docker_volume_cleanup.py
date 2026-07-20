"""Anonymous Docker volumes are reclaimed without touching named volumes."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

docker = pytest.importorskip("docker")

from tolokaforge.core.models import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    TaskConfig,
)
from tolokaforge.core.orchestrator import Orchestrator
from tolokaforge.docker.container import Container
from tolokaforge.docker.image import Image
from tolokaforge.docker.mount import Mount

pytestmark = pytest.mark.unit


def _image() -> Image:
    return Image(
        name="docker",
        tag="dind",
        dockerfile="prebuilt",
        context=".",
        context_hash="prebuilt",
    )


def _created_container(
    *,
    mounts: list[Mount] | None = None,
    live_mounts: list[dict[str, str]] | None = None,
) -> tuple[Container, MagicMock, MagicMock]:
    client = MagicMock(spec=docker.DockerClient)
    docker_container = MagicMock(name="docker_container")
    docker_container.id = "container-id"
    docker_container.attrs = {"Mounts": live_mounts or []}
    client.containers.create.return_value = docker_container
    client.containers.get.return_value = docker_container

    container = Container.create(
        image=_image(),
        name="tolokaforge-dind",
        mounts=mounts,
        client=client,
    )
    return container, client, docker_container


def test_volume_cleanup_reclaims_anonymous_volume_for_any_container() -> None:
    container, client, docker_container = _created_container(
        live_mounts=[
            {
                "Type": "volume",
                "Name": "f4b03b2b-anonymous",
                "Destination": "/var/lib/docker",
            }
        ]
    )
    anonymous_volume = MagicMock(name="anonymous_volume")
    client.volumes.get.return_value = anonymous_volume

    container.destroy(remove_volumes=True)

    docker_container.remove.assert_called_once_with(force=True, v=True)
    client.volumes.get.assert_called_once_with("f4b03b2b-anonymous")
    anonymous_volume.remove.assert_called_once_with()


def test_volume_cleanup_never_removes_declared_named_volume() -> None:
    container, client, _ = _created_container(
        mounts=[Mount.volume("tbench-workspace", "/workspace")],
        live_mounts=[
            {
                "Type": "volume",
                "Name": "tbench-workspace",
                "Destination": "/workspace",
            },
            {
                "Type": "volume",
                "Name": "dind-anonymous",
                "Destination": "/var/lib/docker",
            },
        ],
    )
    anonymous_volume = MagicMock(name="anonymous_volume")
    client.volumes.get.return_value = anonymous_volume

    container.destroy(remove_volumes=True)

    assert client.volumes.get.call_args_list == [call("dind-anonymous")]
    anonymous_volume.remove.assert_called_once_with()


def test_volume_cleanup_removal_failure_warns_and_does_not_propagate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    container, client, _ = _created_container(
        live_mounts=[
            {
                "Type": "volume",
                "Name": "failed-anonymous",
                "Destination": "/var/lib/docker",
            }
        ]
    )
    failed_volume = MagicMock(name="failed_volume")
    failed_volume.remove.side_effect = RuntimeError("volume is busy")
    client.volumes.get.return_value = failed_volume

    with caplog.at_level(logging.WARNING, logger="tolokaforge.docker.container"):
        container.destroy(remove_volumes=True)

    assert "Failed to reclaim anonymous volume 'failed-anonymous'" in caplog.text
    assert "volume is busy" in caplog.text


def _task() -> TaskConfig:
    return TaskConfig(
        task_id="volume-cleanup-task",
        name="Volume cleanup task",
        category="tool_use",
        description="Exercise crash-safe service teardown",
        initial_state={},
        tools={"agent": {"enabled": []}, "user": {"enabled": []}},
        user_simulator={"mode": "scripted"},
        grading="grading.yaml",
    )


def _run_config(tmp_path: Path, *, retain_anonymous_volumes: bool) -> RunConfig:
    return RunConfig(
        models={
            "agent": ModelConfig(provider="openai", name="gpt-4"),
            "user": ModelConfig(provider="openai", name="gpt-4"),
        },
        orchestrator=OrchestratorConfig(
            workers=1,
            repeats=1,
            auto_start_services=True,
            retain_anonymous_volumes=retain_anonymous_volumes,
        ),
        evaluation=EvaluationConfig(output_dir=str(tmp_path / "volume-cleanup")),
    )


def _run_until_executor_crash(
    tmp_path: Path,
    *,
    retain_anonymous_volumes: bool,
    stack_destroy_side_effect: object | None = None,
) -> tuple[MagicMock, RuntimeError]:
    orchestrator = Orchestrator(
        _run_config(
            tmp_path,
            retain_anonymous_volumes=retain_anonymous_volumes,
        )
    )
    orchestrator.tasks = [_task()]
    orchestrator._collect_existing_cost = MagicMock(return_value=0.0)  # type: ignore[method-assign]

    service_stack = MagicMock(name="service_stack")
    service_stack.get_service_url.return_value = "http://localhost:50051"
    service_stack.destroy.side_effect = stack_destroy_side_effect

    run_queue = MagicMock(name="run_queue")
    run_queue.recover_inflight.return_value = 0

    original_error = RuntimeError("executor setup crashed")
    executor_context = MagicMock(name="executor_context")
    executor_context.__enter__.side_effect = original_error
    executor_factory = MagicMock(return_value=executor_context)

    with (
        patch("tolokaforge.core.orchestrator.LLMClient"),
        patch("tolokaforge.core.orchestrator.create_run_queue", return_value=run_queue),
        patch("tolokaforge.core.orchestrator.ThreadPoolExecutor", executor_factory),
        patch("tolokaforge.core.docker_runtime.DockerRuntime"),
        patch("tolokaforge.docker.stacks.core_stack", return_value=service_stack),
    ):
        with pytest.raises(RuntimeError) as raised:
            orchestrator.run()

    assert raised.value is original_error
    return service_stack, original_error


def test_volume_cleanup_runs_on_orchestrator_crash_without_masking_original_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    container, client, _ = _created_container(
        live_mounts=[
            {
                "Type": "volume",
                "Name": "crash-path-anonymous",
                "Destination": "/var/lib/docker",
            }
        ]
    )
    failed_volume = MagicMock(name="failed_volume")
    failed_volume.remove.side_effect = RuntimeError("cleanup failed")
    client.volumes.get.return_value = failed_volume

    def destroy_stack(*, remove_volumes: bool) -> None:
        container.destroy(remove_volumes=remove_volumes)

    with caplog.at_level(logging.WARNING, logger="tolokaforge.docker.container"):
        service_stack, _ = _run_until_executor_crash(
            tmp_path,
            retain_anonymous_volumes=False,
            stack_destroy_side_effect=destroy_stack,
        )

    service_stack.destroy.assert_called_once_with(remove_volumes=True)
    failed_volume.remove.assert_called_once_with()
    assert "cleanup failed" in caplog.text


def test_volume_cleanup_retain_flag_suppresses_orchestrator_cleanup(tmp_path: Path) -> None:
    container, client, docker_container = _created_container(
        live_mounts=[
            {
                "Type": "volume",
                "Name": "retained-anonymous",
                "Destination": "/var/lib/docker",
            }
        ]
    )

    def destroy_stack(*, remove_volumes: bool) -> None:
        container.destroy(remove_volumes=remove_volumes)

    service_stack, _ = _run_until_executor_crash(
        tmp_path,
        retain_anonymous_volumes=True,
        stack_destroy_side_effect=destroy_stack,
    )

    service_stack.destroy.assert_called_once_with(remove_volumes=False)
    docker_container.remove.assert_called_once_with(force=True, v=False)
    client.volumes.get.assert_not_called()


def test_volume_cleanup_is_enabled_by_default_in_orchestrator_config() -> None:
    assert OrchestratorConfig().retain_anonymous_volumes is False
