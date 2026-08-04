"""Unit tests for :meth:`Orchestrator._extract_run_env_manifest`.

The task-declared shared-stack path materialises exactly one compose
file per run. Runs whose tasks declare inconsistent manifests would
silently pick one (or fail late) — this helper picks up the
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
    ServiceSpec,
)
from tolokaforge.core.orchestrator import Orchestrator
from tolokaforge.core.trial import (
    EnvironmentManifest,  # noqa: F401 — kept for canonical-fixture consumers
)
from tolokaforge.runner.models import EnvironmentPatch, StackPatch

pytestmark = pytest.mark.unit


_FIXTURES = Path(__file__).parent.parent / "canonical" / "fixtures" / "environment_manifest"


def _run_config(runtime: str | None = "shared") -> RunConfig:
    """Build a minimal :class:`RunConfig`. ``runtime`` defaults to
    ``"shared"`` so existing tests exercise the shared-stack heterogeneity
    check; pass ``runtime="per_trial"`` for the operator-override branch
    or ``runtime=None`` for the task-driven signal branch (which
    requires a stub adapter on the orchestrator).
    """
    orchestrator_kwargs: dict[str, Any] = {
        "workers": 1,
        "repeats": 1,
        "auto_start_services": False,
    }
    if runtime is not None:
        orchestrator_kwargs["runtime"] = runtime
    return RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4")},
        orchestrator=OrchestratorConfig(**orchestrator_kwargs),
        evaluation=EvaluationConfig(output_dir="/tmp/test_output"),
    )


def _task(task_id: str, patch: EnvironmentPatch | None) -> Any:
    """Minimal stand-in for :class:`TaskConfig` — the helper only reads
    ``.task_id`` and ``.environment_manifest`` (typed
    ``EnvironmentPatch | None`` per M2.5 shape)."""
    t = MagicMock()
    t.task_id = task_id
    t.environment_manifest = patch
    return t


def _patch(
    fixture_name: str = "safe_two_service.yaml", **services: ServiceSpec
) -> EnvironmentPatch:
    """Construct a task-side :class:`EnvironmentPatch` pointing at a
    canonical compose fixture. ``services`` optional per-service specs
    layer into ``EnvironmentPatch.services``."""
    return EnvironmentPatch(
        stack=StackPatch(compose_file=str(_FIXTURES / fixture_name)),
        services=services or None,
    )


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
        p = _patch("safe_two_service.yaml")
        orch = Orchestrator(_run_config())
        orch.tasks = [_task("t1", p), _task("t2", p)]
        result = orch._extract_run_env_manifest()
        assert result is not None
        assert str(result.compose_file) == str(_FIXTURES / "safe_two_service.yaml")

    def test_returns_manifest_when_single_task_declares(self) -> None:
        p = _patch("safe_one_service.yaml")
        orch = Orchestrator(_run_config())
        orch.tasks = [_task("solo", p)]
        result = orch._extract_run_env_manifest()
        assert result is not None
        assert str(result.compose_file) == str(_FIXTURES / "safe_one_service.yaml")

    def test_two_patch_instances_with_same_compose_file_are_consistent(self) -> None:
        """Tasks may carry separately-constructed :class:`EnvironmentPatch`
        instances that resolve to the same compose file; identity is by
        ``compose_file`` path, not Python object identity."""
        p1 = _patch("safe_two_service.yaml")
        p2 = _patch("safe_two_service.yaml")
        orch = Orchestrator(_run_config())
        orch.tasks = [_task("t1", p1), _task("t2", p2)]
        result = orch._extract_run_env_manifest()
        assert result is not None
        assert str(result.compose_file) == str(_FIXTURES / "safe_two_service.yaml")


class TestInconsistentManifest:
    def test_mixed_declared_and_undeclared_raises(self) -> None:
        m = _patch()
        orch = Orchestrator(_run_config())
        orch.tasks = [_task("with", m), _task("without", None)]
        with pytest.raises(RuntimeError, match="mix of tasks with and without"):
            orch._extract_run_env_manifest()

    def test_different_compose_files_raises(self) -> None:
        m1 = _patch("safe_one_service.yaml")
        m2 = _patch("safe_two_service.yaml")
        orch = Orchestrator(_run_config())
        orch.tasks = [_task("t1", m1), _task("t2", m2)]
        with pytest.raises(RuntimeError, match="different environment_manifest.compose_file"):
            orch._extract_run_env_manifest()

    def test_error_names_the_offending_tasks(self) -> None:
        """The error must identify which tasks are on which side of the
        split so the operator can find them quickly."""
        m = _patch()
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


class TestRunWorkerGuard:
    """Distributed worker mode doesn't currently support env_manifest —
    the parent's testcontainers-allocated runner address isn't propagated
    to workers, so a worker joining an env_manifest run would connect to
    a stale/wrong address. Fail loud instead of silently misroute."""

    def test_extract_helper_flags_env_manifest_runs(self) -> None:
        """The run_worker guard uses ``_extract_run_env_manifest`` to
        detect env_manifest runs. Once the helper returns non-None,
        run_worker raises rather than proceeding with the default
        EXECUTOR_ADDRESS."""
        m = _patch("safe_two_service.yaml")
        orch = Orchestrator(_run_config())
        orch.tasks = [_task("t1", m)]
        # Helper returns the manifest; run_worker's guard reads this to
        # decide whether to fail loud. The guard behaviour itself is a
        # single ``if`` in run_worker; here we just pin the helper's
        # non-None contract that the guard depends on.
        assert orch._extract_run_env_manifest() is not None


class TestPerTrialRuntimeReturnsNone:
    """Under ``runtime: per_trial`` the backend resolves
    ``task.environment_manifest`` independently per trial — a run-level
    shared manifest doesn't apply. The helper must short-circuit to
    ``None`` and NOT enforce the shared-stack single-compose-file
    constraint that only applies to ``SharedStackRuntimeBackend``.

    Regression lock: the MB adapter emits one pack per problem, each
    with its own problem-specific compose. A three-problem per-trial
    smoke run declares three distinct ``compose_file`` values — before
    this gate the run failed at ``_extract_run_env_manifest`` before
    Docker was even touched.
    """

    def test_heterogeneous_compose_files_allowed_under_per_trial(self) -> None:
        m1 = _patch("safe_one_service.yaml")
        m2 = _patch("safe_two_service.yaml")
        orch = Orchestrator(_run_config(runtime="per_trial"))
        orch.tasks = [_task("t1", m1), _task("t2", m2)]
        assert orch._extract_run_env_manifest() is None

    def test_mixed_declared_and_undeclared_allowed_under_per_trial(self) -> None:
        m = _patch("safe_two_service.yaml")
        orch = Orchestrator(_run_config(runtime="per_trial"))
        orch.tasks = [_task("with", m), _task("without", None)]
        assert orch._extract_run_env_manifest() is None

    def test_consistent_manifests_also_return_none_under_per_trial(self) -> None:
        """Even when tasks happen to share one compose file, per-trial
        doesn't consume a run-level manifest — the backend resolves the
        task's own manifest per trial. Return None to keep the call
        sites consistent (``run_env_manifest is None`` under per-trial)."""
        p = _patch("safe_two_service.yaml")
        orch = Orchestrator(_run_config(runtime="per_trial"))
        orch.tasks = [_task("t1", p), _task("t2", p)]
        assert orch._extract_run_env_manifest() is None

    def test_shared_runtime_still_enforces_heterogeneity_check(self) -> None:
        """Ensure the per-trial short-circuit does not silently loosen
        the shared-runtime invariant."""
        m1 = _patch("safe_one_service.yaml")
        m2 = _patch("safe_two_service.yaml")
        orch = Orchestrator(_run_config(runtime="shared"))
        orch.tasks = [_task("t1", m1), _task("t2", m2)]
        with pytest.raises(RuntimeError, match="different environment_manifest.compose_file"):
            orch._extract_run_env_manifest()

    def test_task_driven_per_trial_signal_short_circuits(self) -> None:
        """When the operator drops the deprecated ``orchestrator.runtime``
        override and per-trial selection derives from
        ``manifest.requires_per_trial`` via
        :meth:`_select_backend_from_tasks`, the short-circuit must still
        fire — otherwise the shared-stack heterogeneity check would run
        on a per-trial run.

        Uses a stub adapter to drive the task-driven branch (``override
        is None + adapter is not None``); this is the path exercised
        once the deprecation lands and consumers migrate off the
        operator override.
        """
        m1 = _patch("safe_one_service.yaml")
        m2 = _patch("safe_two_service.yaml")
        # override=None → task-driven selection
        orch = Orchestrator(_run_config(runtime=None))
        orch.tasks = [_task("t1", m1), _task("t2", m2)]
        # Stub adapter: to_task_description returns a manifest whose
        # ``requires_per_trial`` is True, driving _select_backend_from_tasks
        # to return "per_trial".
        adapter = MagicMock()
        per_trial_manifest = MagicMock()
        per_trial_manifest.requires_per_trial = True
        task_desc = MagicMock()
        task_desc.environment_manifest = per_trial_manifest
        adapter.to_task_description.return_value = task_desc
        orch.adapter = adapter
        assert orch._extract_run_env_manifest() is None


class TestServicesDeclarationsPreserved:
    """The helper doesn't need to validate isolation labels — that's
    :meth:`_verify_isolation_compatibility`'s job. But it must not
    strip or alter the manifest's per-service declarations."""

    def test_reset_service_survives(self) -> None:
        from tolokaforge.core.models import ResetSpec

        m = _patch(
            "safe_two_service.yaml",
            db=ServiceSpec(isolation="reset", reset=ResetSpec(seed="baseline")),
            default=ServiceSpec(isolation="shared"),
        )
        orch = Orchestrator(_run_config())
        orch.tasks = [_task("t1", m)]
        result = orch._extract_run_env_manifest()
        assert result is not None
        assert result.services["db"].isolation == "reset"

    def test_all_shared_survives(self) -> None:
        m = _patch(
            "safe_two_service.yaml",
            db=ServiceSpec(isolation="shared"),
            default=ServiceSpec(isolation="shared"),
        )
        orch = Orchestrator(_run_config())
        orch.tasks = [_task("t1", m)]
        result = orch._extract_run_env_manifest()
        assert result is not None
        assert result.services["db"].isolation == "shared"
        assert result.services["default"].isolation == "shared"
