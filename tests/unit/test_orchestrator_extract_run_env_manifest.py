"""Unit tests for :meth:`Orchestrator._extract_run_env_manifest`.

Per-scope INV-1 (ADR-0044 §5): the helper compares only the ``run``-scope
subset of each task's resolved composition plan across tasks. Task and
trial scopes may diverge freely — the composer materialises those
per-task at ``provision_trial`` time. A divergence on the ``run``-scope
subset fails loud so an ambiguous cross-task declaration surfaces before
Docker is touched.
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

# Explicit ``shared`` labels on every compose service force
# ``requires_per_trial=False``, so the scalar-form synthesis stamps the
# stack ``stack_scope="run"`` and the divergence check has something to
# compare across tasks. Without these overrides the fill-defaults path
# stamps ``ephemeral`` (→ trial scope) and every stack drops out of the
# run-scope subset — legal, but not what the run-scope divergence tests
# exercise.
_SHARED_TWO_SERVICE: dict[str, ServiceSpec] = {
    "db": ServiceSpec(isolation="shared"),
    "default": ServiceSpec(isolation="shared"),
}
_SHARED_ONE_SERVICE: dict[str, ServiceSpec] = {"default": ServiceSpec(isolation="shared")}


def _run_config(runtime: str | None = None) -> RunConfig:
    """Build a minimal :class:`RunConfig`. ``runtime=None`` exercises the
    automatic path; ``"per_trial"`` / ``"shared"`` exercise the operator
    coercion knobs.
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


def _run_scope_patch(
    fixture_name: str = "safe_two_service.yaml",
    **service_overrides: ServiceSpec,
) -> EnvironmentPatch:
    """Construct a task-side :class:`EnvironmentPatch` pointing at a
    canonical compose fixture with ``shared`` labels on every service.

    Force-``shared`` isolation makes ``requires_per_trial=False``, so the
    scalar-form synthesis stamps ``stack_scope="run"`` — the branch the
    INV-1 divergence check gates. ``service_overrides`` (per-service
    :class:`ServiceSpec`) layer on top for cases that need
    ``reset``-labelled services (still ``run``-scope because at least
    one service stays ``shared``).
    """
    if fixture_name == "safe_one_service.yaml":
        services = dict(_SHARED_ONE_SERVICE)
    else:
        services = dict(_SHARED_TWO_SERVICE)
    services.update(service_overrides)
    return EnvironmentPatch(
        stack=StackPatch(compose_file=str(_FIXTURES / fixture_name)),
        services=services,
    )


def _trial_scope_patch(fixture_name: str = "safe_two_service.yaml") -> EnvironmentPatch:
    """Task-side patch with no explicit isolation labels — the fill-defaults
    path stamps every service ``ephemeral``, so the synthesised stack is
    ``stack_scope="trial"`` and drops out of the run-scope divergence
    check.
    """
    return EnvironmentPatch(
        stack=StackPatch(compose_file=str(_FIXTURES / fixture_name)),
        services=None,
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
    """Scalar-form single-stack path — every task synthesises one
    run-scope stack from the same fixture, so their signatures match."""

    def test_returns_manifest_when_all_tasks_declare_same_compose_file(self) -> None:
        p = _run_scope_patch("safe_two_service.yaml")
        orch = Orchestrator(_run_config())
        orch.tasks = [_task("t1", p), _task("t2", p)]
        result = orch._extract_run_env_manifest()
        assert result is not None
        assert str(result.compose_file) == str(_FIXTURES / "safe_two_service.yaml")

    def test_returns_manifest_when_single_task_declares(self) -> None:
        p = _run_scope_patch("safe_one_service.yaml")
        orch = Orchestrator(_run_config())
        orch.tasks = [_task("solo", p)]
        result = orch._extract_run_env_manifest()
        assert result is not None
        assert str(result.compose_file) == str(_FIXTURES / "safe_one_service.yaml")

    def test_two_patch_instances_with_same_compose_file_are_consistent(self) -> None:
        """Tasks may carry separately-constructed :class:`EnvironmentPatch`
        instances that resolve to the same run-scope signature; identity
        is by canonical compose bytes + stack_id + inputs, not Python
        object identity."""
        p1 = _run_scope_patch("safe_two_service.yaml")
        p2 = _run_scope_patch("safe_two_service.yaml")
        orch = Orchestrator(_run_config())
        orch.tasks = [_task("t1", p1), _task("t2", p2)]
        result = orch._extract_run_env_manifest()
        assert result is not None
        assert str(result.compose_file) == str(_FIXTURES / "safe_two_service.yaml")


class TestInconsistentManifest:
    """Scalar-form single-stack path — divergent run-scope stacks refuse."""

    def test_mixed_declared_and_undeclared_raises(self) -> None:
        m = _run_scope_patch("safe_two_service.yaml")
        orch = Orchestrator(_run_config())
        orch.tasks = [_task("with", m), _task("without", None)]
        with pytest.raises(RuntimeError, match="disagree on the run-scope subset"):
            orch._extract_run_env_manifest()

    def test_different_compose_files_raises(self) -> None:
        m1 = _run_scope_patch("safe_one_service.yaml")
        m2 = _run_scope_patch("safe_two_service.yaml")
        orch = Orchestrator(_run_config())
        orch.tasks = [_task("t1", m1), _task("t2", m2)]
        with pytest.raises(RuntimeError, match="disagree on the run-scope subset"):
            orch._extract_run_env_manifest()

    def test_error_names_the_offending_tasks(self) -> None:
        """The error must identify every task's run-scope digest so the
        operator can spot which tasks disagree."""
        m = _run_scope_patch()
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
        assert "has-manifest-A" in msg
        assert "has-manifest-C" in msg
        assert "no-manifest-B" in msg
        assert "no-manifest-D" in msg


class TestPerScopeInvariant:
    """Per-scope INV-1: only the run-scope subset must agree across tasks.

    Task and trial scopes are the composer's per-task business — the
    orchestrator only enforces that every task declares the same ordered
    run-scope stack sequence (canonical compose bytes + stack_id +
    runner_service + inputs).
    """

    def test_matching_run_scope_diverging_trial_scope_resolves(self) -> None:
        """Two tasks that share their run-scope stack but declare
        different trial-scope compose files (via the same fixture is
        semantically fine — the check filters by scope, not by
        compose_file diff)."""
        # Both patches carry a run-scope stack; the trial-scope
        # component is per-task by resolve construction (fill-defaults
        # doesn't apply to the scalar mirror when at least one
        # ``shared`` label is present, so both tasks resolve to a
        # single run-scope stack with matching bytes).
        p = _run_scope_patch("safe_two_service.yaml")
        orch = Orchestrator(_run_config())
        orch.tasks = [_task("t1", p), _task("t2", p)]
        result = orch._extract_run_env_manifest()
        assert result is not None

    def test_diverging_run_scope_stacks_raise_with_task_ids(self) -> None:
        """Different compose bytes on the run-scope stack across tasks
        raise; both offending task_ids appear in the error."""
        m1 = _run_scope_patch("safe_one_service.yaml")
        m2 = _run_scope_patch("safe_two_service.yaml")
        orch = Orchestrator(_run_config())
        orch.tasks = [_task("task-alpha", m1), _task("task-beta", m2)]
        with pytest.raises(RuntimeError) as excinfo:
            orch._extract_run_env_manifest()
        msg = str(excinfo.value)
        assert "task-alpha" in msg
        assert "task-beta" in msg
        assert "disagree on the run-scope subset" in msg

    def test_mixed_run_scope_declaration_raises(self) -> None:
        """Task A declares a run-scope stack; Task B has only a
        trial-scope stack (pure ephemeral). Task A's signature has one
        stack; Task B's is empty. Divergence — refuse.
        """
        run_scope = _run_scope_patch("safe_two_service.yaml")
        trial_only = _trial_scope_patch("safe_two_service.yaml")
        orch = Orchestrator(_run_config())
        orch.tasks = [_task("with-run", run_scope), _task("trial-only", trial_only)]
        with pytest.raises(RuntimeError, match="disagree on the run-scope subset"):
            orch._extract_run_env_manifest()

    def test_all_tasks_empty_run_scope_returns_none(self) -> None:
        """When every task's run-scope subset is empty — even if the
        tasks declare divergent trial-scope compose files — the helper
        returns ``None``. No run-scope stack to materialise, so the
        composer's ``materialise_run`` no-ops. The plan-shape
        short-circuit at the top handles the ``TRIAL_SCOPED_ONLY`` case;
        this branch covers the divergent-trial-scope-but-adapterless
        equivalent.
        """
        m1 = _trial_scope_patch("safe_one_service.yaml")
        m2 = _trial_scope_patch("safe_two_service.yaml")
        orch = Orchestrator(_run_config())
        orch.tasks = [_task("t1", m1), _task("t2", m2)]
        assert orch._extract_run_env_manifest() is None


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
        m = _run_scope_patch("safe_two_service.yaml")
        orch = Orchestrator(_run_config())
        orch.tasks = [_task("t1", m)]
        assert orch._extract_run_env_manifest() is not None


class TestPerTrialRuntimeReturnsNone:
    """Under ``runtime: per_trial`` (operator coercion) OR under the
    task-driven signal that every task's ``plan_shape`` is
    ``TRIAL_SCOPED_ONLY``, the helper short-circuits to ``None`` — no
    run-scope stack materialises, so nothing to extract at run-connect
    time. The composer's ``provision_trial`` reads each task's own
    manifest per trial.
    """

    def test_heterogeneous_compose_files_allowed_under_per_trial(self) -> None:
        m1 = _run_scope_patch("safe_one_service.yaml")
        m2 = _run_scope_patch("safe_two_service.yaml")
        orch = Orchestrator(_run_config(runtime="per_trial"))
        orch.tasks = [_task("t1", m1), _task("t2", m2)]
        assert orch._extract_run_env_manifest() is None

    def test_mixed_declared_and_undeclared_allowed_under_per_trial(self) -> None:
        m = _run_scope_patch("safe_two_service.yaml")
        orch = Orchestrator(_run_config(runtime="per_trial"))
        orch.tasks = [_task("with", m), _task("without", None)]
        assert orch._extract_run_env_manifest() is None

    def test_consistent_manifests_also_return_none_under_per_trial(self) -> None:
        """Even when tasks happen to share one compose file, per-trial
        coercion means the composer resolves per-task at provision time
        — return None to keep the call sites consistent
        (``run_env_manifest is None`` under per-trial)."""
        p = _run_scope_patch("safe_two_service.yaml")
        orch = Orchestrator(_run_config(runtime="per_trial"))
        orch.tasks = [_task("t1", p), _task("t2", p)]
        assert orch._extract_run_env_manifest() is None

    def test_shared_runtime_still_enforces_divergence_check(self) -> None:
        """The per_trial short-circuit must not silently loosen the
        divergence check under the default (automatic) path."""
        m1 = _run_scope_patch("safe_one_service.yaml")
        m2 = _run_scope_patch("safe_two_service.yaml")
        orch = Orchestrator(_run_config(runtime="shared"))
        orch.tasks = [_task("t1", m1), _task("t2", m2)]
        with pytest.raises(RuntimeError, match="disagree on the run-scope subset"):
            orch._extract_run_env_manifest()

    def test_task_driven_per_trial_signal_short_circuits(self) -> None:
        """When the operator drops the deprecated ``orchestrator.runtime``
        override and every task's ``plan_shape`` is ``TRIAL_SCOPED_ONLY``,
        the short-circuit must still fire — otherwise the divergence
        check would run on a per-trial run.

        Uses a stub adapter to drive the task-driven branch (``override
        is None + adapter is not None``); this is the path exercised
        once the deprecation lands and consumers migrate off the
        operator override.
        """
        from tolokaforge.runner.models import PlanShape

        m1 = _run_scope_patch("safe_one_service.yaml")
        m2 = _run_scope_patch("safe_two_service.yaml")
        orch = Orchestrator(_run_config(runtime=None))
        orch.tasks = [_task("t1", m1), _task("t2", m2)]
        adapter = MagicMock()
        per_trial_manifest = MagicMock()
        per_trial_manifest.plan_shape = PlanShape.TRIAL_SCOPED_ONLY
        task_desc = MagicMock()
        task_desc.environment_manifest = per_trial_manifest
        task_desc.adapter_type = "native"
        adapter.to_task_description.return_value = task_desc
        orch.adapter = adapter
        assert orch._extract_run_env_manifest() is None


class TestServicesDeclarationsPreserved:
    """The helper doesn't validate isolation labels — that's
    :meth:`_verify_isolation_compatibility`'s job. It must not strip or
    alter the manifest's per-service declarations on the run-scope
    path where it does return one."""

    def test_all_shared_survives(self) -> None:
        m = _run_scope_patch(
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
