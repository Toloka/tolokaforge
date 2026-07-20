"""Unit coverage for best-effort host-to-DinD task image preloading."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from docker.errors import ImageNotFound

from tolokaforge.core.models import OrchestratorConfig
from tolokaforge.docker.image_preload import (
    _stream_image_to_dind,
    discover_image_tags,
    preload_images_into_dind,
)
from tolokaforge.docker.stacks.core import core_stack

pytestmark = pytest.mark.unit

DIND_ENDPOINT = "tcp://tolokaforge-dind:2375"


class TestImagePreload:
    def test_preload_config_defaults_and_core_stack_propagation(self, tmp_path: Path) -> None:
        compose_file = tmp_path / "docker-compose.yaml"
        config = OrchestratorConfig()

        stack = core_stack(
            enable_dind=True,
            task_compose_files=[compose_file],
            preload_task_images=config.preload_task_images,
            preload_images=["tolokaforge/extra:1"],
        )

        assert config.preload_task_images is True
        assert config.preload_images == []
        assert stack.task_compose_files == [compose_file]
        assert stack.preload_task_images is True
        assert stack.preload_images == ["tolokaforge/extra:1"]

    def test_preload_discovers_compose_images_and_unions_explicit_tags(
        self, tmp_path: Path
    ) -> None:
        compose_a = tmp_path / "compose-a.yaml"
        compose_a.write_text(
            """
services:
  cad:
    build: .
    image: tolokaforge/engineering-cad:2026-07
  helper:
    image: registry.example/helper:4
  source-only:
    build: ./source
""",
            encoding="utf-8",
        )
        compose_b = tmp_path / "compose-b.yaml"
        compose_b.write_text(
            """
services:
  duplicate:
    image: registry.example/helper:4
  other:
    image: tolokaforge/other:9
""",
            encoding="utf-8",
        )

        tags = discover_image_tags(
            [compose_a, compose_b],
            ["registry.example/helper:4", "explicit.example/tool:2"],
        )

        assert tags == [
            "tolokaforge/engineering-cad:2026-07",
            "registry.example/helper:4",
            "tolokaforge/other:9",
            "explicit.example/tool:2",
        ]

    def test_preload_present_host_tag_triggers_one_load_with_dind_endpoint(self) -> None:
        host_client = MagicMock()

        with patch("tolokaforge.docker.image_preload._stream_image_to_dind") as stream:
            preload_images_into_dind(
                ["tolokaforge/engineering-cad:2026-07"],
                dind_container_name="tolokaforge-dind",
                dind_endpoint=DIND_ENDPOINT,
                host_client=host_client,
            )

        host_client.images.get.assert_called_once_with("tolokaforge/engineering-cad:2026-07")
        stream.assert_called_once_with(
            "tolokaforge/engineering-cad:2026-07",
            dind_container_name="tolokaforge-dind",
            dind_endpoint=DIND_ENDPOINT,
        )

    def test_preload_stream_uses_tag_container_and_dind_endpoint(self) -> None:
        save_process = MagicMock()
        save_process.stdout = MagicMock()
        save_process.wait.return_value = 0
        load_process = MagicMock()
        load_process.wait.return_value = 0

        with patch(
            "tolokaforge.docker.image_preload.subprocess.Popen",
            side_effect=[save_process, load_process],
        ) as popen:
            _stream_image_to_dind(
                "tolokaforge/engineering-cad:2026-07",
                dind_container_name="tolokaforge-dind",
                dind_endpoint=DIND_ENDPOINT,
            )

        assert popen.call_args_list[0] == call(
            ["docker", "save", "tolokaforge/engineering-cad:2026-07"],
            stdout=-1,
            stderr=popen.call_args_list[0].kwargs["stderr"],
        )
        assert popen.call_args_list[1] == call(
            [
                "docker",
                "exec",
                "-i",
                "tolokaforge-dind",
                "docker",
                "--host",
                DIND_ENDPOINT,
                "load",
            ],
            stdin=save_process.stdout,
            stdout=popen.call_args_list[1].kwargs["stdout"],
            stderr=popen.call_args_list[1].kwargs["stderr"],
        )

    def test_preload_absent_host_tag_warns_and_skips_load(self, caplog) -> None:
        host_client = MagicMock()
        host_client.images.get.side_effect = ImageNotFound("missing")

        with (
            caplog.at_level(logging.WARNING),
            patch("tolokaforge.docker.image_preload._stream_image_to_dind") as stream,
        ):
            preload_images_into_dind(
                ["tolokaforge/missing:1"],
                dind_container_name="tolokaforge-dind",
                dind_endpoint=DIND_ENDPOINT,
                host_client=host_client,
            )

        stream.assert_not_called()
        assert "absent from the host Docker daemon" in caplog.text
        assert "tolokaforge/missing:1" in caplog.text

    def test_preload_disabled_skips_discovery_host_check_and_load(self) -> None:
        stack = core_stack(
            enable_dind=True,
            preload_task_images=False,
            task_compose_files=[Path("unused-compose.yaml")],
            preload_images=["tolokaforge/unused:1"],
        )

        with (
            patch("tolokaforge.docker.image_preload.discover_image_tags") as discover,
            patch("tolokaforge.docker.image_preload.docker.from_env") as from_env,
            patch("tolokaforge.docker.image_preload._stream_image_to_dind") as stream,
        ):
            stack._preload_images_into_dind()  # noqa: SLF001

        discover.assert_not_called()
        from_env.assert_not_called()
        stream.assert_not_called()

    def test_preload_hook_runs_after_dind_start_and_before_runner(self) -> None:
        stack = core_stack(enable_dind=True, preload_images=["tolokaforge/task:1"])
        events: list[str] = []

        with (
            patch.object(type(stack), "create_networks"),
            patch.object(
                type(stack),
                "_start_service",
                side_effect=lambda service, wait: events.append(f"start:{service.name}"),
            ),
            patch.object(
                type(stack),
                "_preload_images_into_dind",
                side_effect=lambda: events.append("preload"),
            ),
        ):
            stack.start_all(wait=True, build=False)

        assert events.index("start:dind") < events.index("preload")
        assert events.index("preload") < events.index("start:runner")

    def test_preload_load_failure_warns_and_continues_to_next_tag(self, caplog) -> None:
        host_client = MagicMock()

        with (
            caplog.at_level(logging.WARNING),
            patch(
                "tolokaforge.docker.image_preload._stream_image_to_dind",
                side_effect=[RuntimeError("load broke"), None],
            ) as stream,
        ):
            preload_images_into_dind(
                ["tolokaforge/first:1", "tolokaforge/second:2"],
                dind_container_name="tolokaforge-dind",
                dind_endpoint=DIND_ENDPOINT,
                host_client=host_client,
            )

        assert stream.call_count == 2
        assert "Failed to preload task image tolokaforge/first:1" in caplog.text
        assert "load broke" in caplog.text
