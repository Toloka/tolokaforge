"""Unit tests for :meth:`Orchestrator._extract_run_env_manifest`.

The Phase 4 shared-stack path materialises exactly one task-declared
compose file per run. Runs whose tasks declare inconsistent manifests
would silently pick one (or fail late) — this helper picks up the
inconsistency at run-start and fails loud.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.models import (
    EvaluationConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
)
from tolokaforge.core.orchestrator import Orchestrator
from tolokaforge.core.trial import EnvironmentManifest, TaskIsolation

pytestmark = pytest.mark.unit


_FIXTURES = Path(__file__).parent.parent / "canonical" / "fixtures" / "environment_manifest"


def _run_config() -> RunConfig:
    return RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4")},
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir="/tmp/test_output"),
    )


def _task(task_id: str, manifest: EnvironmentManifest | None) -> Any:
    """Minimal stand-in for :class:`TaskConfig` — the helper only reads
    ``.task_id`` and ``.environment_manifest``."""
    t = MagicMock()
    t.task_id = task_id
    t.environment_manifest = manifest
    return t


def _manifest(fixture_name: str = "safe_two_service.yaml") -> EnvironmentManifest:
    return EnvironmentManifest(compose_file=_FIXTURES / fixture_name)


class TestNoManifest:
    def test_returns_none_when_no_tasks(self) -> None:
        orch = Orchestrator(_run_config())
        orch.tasks = []
        assert orch._extract_run_env_manifest() is None

    def test_returns_none_when_no_task_declares_manifest(self) -> None:
        orch = Orchestrator(_run_config())
        orch.tasks = [_task("t1", None), _task("t2", None)]
        assert orch._extract_run_env_manifest() is None


class TestConsistentManifest:
    def test_returns_manifest_when_all_tasks_declare_same_compose_file(self) -> None:
        m = _manifest("safe_two_service.yaml")
        orch = Orchestrator(_run_config())
        orch.tasks = [_task("t1", m), _task("t2", m)]
        result = orch._extract_run_env_manifest()
        assert result is m

    def test_returns_manifest_when_single_task_declares(self) -> None:
        m = _manifest("safe_one_service.yaml")
        orch = Orchestrator(_run_config())
        orch.tasks = [_task("solo", m)]
        assert orch._extract_run_env_manifest() is m

    def test_two_manifest_instances_with_same_compose_file_are_consistent(self) -> None:
        """Task authors may construct EnvironmentManifest objects
        separately per task; identity is by ``compose_file`` path, not
        Python object identity."""
        m1 = EnvironmentManifest(compose_file=_FIXTURES / "safe_two_service.yaml")
        m2 = EnvironmentManifest(compose_file=_FIXTURES / "safe_two_service.yaml")
        orch = Orchestrator(_run_config())
        orch.tasks = [_task("t1", m1), _task("t2", m2)]
        # Either instance may be returned — the check is that no error is raised.
        result = orch._extract_run_env_manifest()
        assert result is not None
        assert str(result.compose_file) == str(m1.compose_file)


class TestInconsistentManifest:
    def test_mixed_declared_and_undeclared_raises(self) -> None:
        m = _manifest()
        orch = Orchestrator(_run_config())
        orch.tasks = [_task("with", m), _task("without", None)]
        with pytest.raises(RuntimeError, match="mix of tasks with and without"):
            orch._extract_run_env_manifest()

    def test_different_compose_files_raises(self) -> None:
        m1 = _manifest("safe_one_service.yaml")
        m2 = _manifest("safe_two_service.yaml")
        orch = Orchestrator(_run_config())
        orch.tasks = [_task("t1", m1), _task("t2", m2)]
        with pytest.raises(RuntimeError, match="different environment_manifest.compose_file"):
            orch._extract_run_env_manifest()

    def test_error_names_the_offending_tasks(self) -> None:
        """The error must identify which tasks are on which side of the
        split so the operator can find them quickly."""
        m = _manifest()
        orch = Orchestrator(_run_config())
        orch.tasks = [
            _task("has-manifest-A", m),
            _task("no-manifest-B", None),
            _task("has-manifest-C", m),
            _task("no-manifest-D", None),
        ]
        with pytest.raises(RuntimeError) as excinfo:
            orch._extract_run_env_manifest()
        msg = str(excinfo.value)
        # Task ids on both sides appear in the message.
        assert "has-manifest-A" in msg
        assert "has-manifest-C" in msg
        assert "no-manifest-B" in msg
        assert "no-manifest-D" in msg


class TestIsolationDeclarationsPreserved:
    """The helper doesn't need to validate ``isolation`` — that's
    :meth:`_verify_isolation_compatibility`'s job. But it must not
    strip or alter the manifest's isolation declaration when returning."""

    def test_per_trial_isolation_survives(self) -> None:
        m = EnvironmentManifest(
            compose_file=_FIXTURES / "safe_two_service.yaml",
            isolation=TaskIsolation.PER_TRIAL,
        )
        orch = Orchestrator(_run_config())
        orch.tasks = [_task("t1", m)]
        result = orch._extract_run_env_manifest()
        assert result is not None
        assert result.isolation == TaskIsolation.PER_TRIAL

    def test_shared_ok_isolation_survives(self) -> None:
        m = EnvironmentManifest(
            compose_file=_FIXTURES / "safe_two_service.yaml",
            isolation=TaskIsolation.SHARED_OK,
        )
        orch = Orchestrator(_run_config())
        orch.tasks = [_task("t1", m)]
        result = orch._extract_run_env_manifest()
        assert result is not None
        assert result.isolation == TaskIsolation.SHARED_OK
