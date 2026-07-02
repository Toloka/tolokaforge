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
import yaml

from tolokaforge.adapters.native import NativeAdapter

pytestmark = pytest.mark.unit


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _build_task(
    tmp_path: Path,
    *,
    environment_manifest: dict | None = None,
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
    _write_yaml(task_dir / "task.yaml", task_yaml)
    _write_yaml(
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
    _write_yaml(
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
    return NativeAdapter({"base_dir": str(tmp_path), "tasks_glob": "tasks/**/task.yaml"})


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
                "isolation": "per_trial",
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
        # isolation + runner_service round-trip.
        assert manifest.isolation.value == "per_trial"
        assert manifest.runner_service == "runner"
