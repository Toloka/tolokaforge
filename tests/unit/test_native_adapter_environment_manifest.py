"""``NativeAdapter.to_task_description`` wires ``environment_manifest``
from ``task.yaml`` onto the produced ``TaskDescription``.

Pins the plumbing PR C added to close the gap: without this, a task
declaring ``environment_manifest`` in its YAML would have the field
silently dropped by the adapter, and ``PerTrialRuntimeBackend`` would
refuse to provision (raising ``ProvisionError("… task did not declare
one")``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.canonical._factories import write_yaml_file
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.models import EnvironmentPatch, StackPatch

pytestmark = pytest.mark.unit


def _build_task(
    tmp_path: Path,
    *,
    environment_manifest: dict | None = None,
    project_default_environment: EnvironmentPatch | None = None,
) -> NativeAdapter:
    task_dir = tmp_path / "tasks" / "manifest_task"
    task_dir.mkdir(parents=True)
    (task_dir / "system_prompt.md").write_text("system\n")
    (task_dir / "initial_state.json").write_text("{}")
    task_yaml: dict = {
        "task_id": "manifest_task",
        "name": "manifest task",
        "category": "tool_use",
        "description": "manifest task",
        "initial_state": {"json_db": "initial_state.json"},
        "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
        "user_simulator": {"mode": "llm", "persona": "cooperative"},
        "grading": "grading.yaml",
        "system_prompt": "system_prompt.md",
    }
    if environment_manifest is not None:
        task_yaml["environment_manifest"] = environment_manifest
    write_yaml_file(task_dir / "task.yaml", task_yaml)
    write_yaml_file(
        task_dir / "grading.yaml",
        {
            "combine": {
                "method": "weighted",
                "weights": {"state_checks": 1.0},
                "pass_threshold": 0.5,
            },
            "components": {"state_checks": {"jsonpaths": []}},
        },
    )
    # Compose file that satisfies the manifest validator (public postgres,
    # pinned tag).
    write_yaml_file(
        task_dir / "environment.compose.yaml",
        {
            "services": {
                "runner": {
                    "image": "tolokaforge-runner:local",
                    "ports": ["50051"],
                },
                "db": {
                    "image": "postgres:16",
                    "environment": {
                        "POSTGRES_USER": "tolokaforge",
                        "POSTGRES_PASSWORD": "tolokaforge",
                        "POSTGRES_DB": "tolokaforge",
                    },
                    "ports": ["5432"],
                },
            },
        },
    )
    params: dict = {"base_dir": str(tmp_path), "tasks_glob": "tasks/**/task.yaml"}
    if project_default_environment is not None:
        params["project_default_environment"] = project_default_environment
    return NativeAdapter(params)


class TestEnvironmentManifestWiring:
    """A task declaring ``environment_manifest`` in ``task.yaml`` has
    that manifest reach the produced ``TaskDescription`` with the
    ``compose_file`` path resolved absolute (task-directory-relative
    strings become filesystem paths the runtime can copy from).
    """

    def test_absent_manifest_yields_none(self, tmp_path: Path) -> None:
        adapter = _build_task(tmp_path)
        task_description = adapter.to_task_description("manifest_task")

        assert task_description.environment_manifest is None

    def test_present_manifest_reaches_task_description(self, tmp_path: Path) -> None:
        adapter = _build_task(
            tmp_path,
            environment_manifest={
                "compose_file": "./environment.compose.yaml",
                "services": {"db": {"isolation": "ephemeral"}},
                "runner_service": "runner",
            },
        )
        task_description = adapter.to_task_description("manifest_task")

        manifest = task_description.environment_manifest
        assert manifest is not None
        # compose_file resolved absolute against task directory.
        assert manifest.compose_file.is_absolute()
        assert manifest.compose_file.name == "environment.compose.yaml"
        assert manifest.compose_file.exists()
        # services + runner_service round-trip; the compose service
        # `runner` was not labelled so it fills with ephemeral.
        assert manifest.services["db"].isolation == "ephemeral"
        assert manifest.services["runner"].isolation == "ephemeral"
        assert manifest.requires_per_trial is True
        assert manifest.runner_service == "runner"

    def test_project_default_environment_composes_with_task_patch(self, tmp_path: Path) -> None:
        """The orchestrator forwards ``project.default_environment`` to
        the adapter via ``project_default_environment``; the adapter
        binds it to each task's own patch via :func:`resolve`. A task
        that patches only ``stack.inputs`` inherits ``compose_file`` and
        ``runner_service`` from the project — this pins that the
        orchestrator → adapter → resolve chain actually deep-merges."""
        # The project patch points at the same compose file the task
        # fixture writes; the concrete task pack lives at
        # ``tmp_path / "tasks" / "manifest_task" /``.
        compose_path = tmp_path / "tasks" / "manifest_task" / "environment.compose.yaml"
        project_patch = EnvironmentPatch(
            stack=StackPatch(
                compose_file=compose_path,
                runner_service="runner",
                inputs={"postgres_version": "16", "region": "eu"},
            ),
        )
        adapter = _build_task(
            tmp_path,
            environment_manifest={"stack": {"inputs": {"postgres_version": "17"}}},
            project_default_environment=project_patch,
        )
        task_description = adapter.to_task_description("manifest_task")

        manifest = task_description.environment_manifest
        assert manifest is not None
        # compose_file + runner_service inherited from the project patch.
        assert manifest.compose_file == compose_path
        assert manifest.runner_service == "runner"
        # inputs deep-merged: task wins on the conflicting key, project
        # value survives for the untouched one.
        assert manifest.stack_inputs == {"postgres_version": "17", "region": "eu"}
