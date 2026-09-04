"""Locks :attr:`SharedStackRuntimeBackend.isolation_mode` — the computed
posture derived from :attr:`EnvironmentManifest.plan_shape` (or from the
``_per_trial_mode`` flag when no manifest is set).

The orchestrator reads ``isolation_mode`` to route the isolation-compatibility
check; every plan-shape branch resolves to the shape here so the routing stays
predictable across the composition surface.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend
from tolokaforge.core.runtime import IsolationMode
from tolokaforge.core.shared_stack_runtime import SharedStackRuntimeBackend
from tolokaforge.core.trial import EnvironmentManifest
from tolokaforge.runner.models import StackDecl, StackScope

pytestmark = pytest.mark.unit


_FIXTURES = Path(__file__).parent.parent / "canonical" / "fixtures" / "environment_manifest"
_COMPOSE_FILE = _FIXTURES / "safe_two_service.yaml"


def _manifest_with_stacks(*scopes: StackScope) -> EnvironmentManifest:
    """Build a manifest whose ``stacks`` list carries one :class:`StackDecl`
    per requested scope. The compose file mirrors the fixture the isolation-
    compat suite reuses, so :attr:`plan_shape` reads directly from the passed
    scope tuple.

    An empty ``scopes`` (no ``StackDecl``) falls back to ``TRIAL_SCOPED_ONLY``
    per :attr:`EnvironmentManifest.plan_shape`; callers pass at least one
    scope.
    """
    manifest = EnvironmentManifest(compose_file=_COMPOSE_FILE, runner_service="default")
    for idx, scope in enumerate(scopes):
        manifest.stacks.append(
            StackDecl(
                stack_id=f"stack-{idx}",
                compose_file=_COMPOSE_FILE,
                stack_scope=scope,
                runner_service="default" if scope != "task" else None,
            )
        )
    return manifest


class TestIsolationModeEnum:
    def test_admits_composed_stack_value(self) -> None:
        assert IsolationMode.COMPOSED_STACK.value == "composed_stack"

    def test_closed_vocab_has_three_members(self) -> None:
        assert {mode.value for mode in IsolationMode} == {
            "shared_stack",
            "per_trial_stack",
            "composed_stack",
        }


class TestBuiltInStackAndDelegateModes:
    """``env_manifest=None`` selects the mode from the ``_per_trial_mode`` flag.

    Built-in-stack mode is the orchestrator's default when the run relies on
    the shared runner already brought up by :class:`EngineStack`; the
    per-trial-delegate mode is set by :class:`PerTrialRuntimeBackend`.
    """

    def test_built_in_stack_mode_is_shared_stack(self) -> None:
        backend = SharedStackRuntimeBackend(runner_address="sentinel:50051")
        assert backend.isolation_mode is IsolationMode.SHARED_STACK

    def test_per_trial_delegate_mode_is_per_trial_stack(self) -> None:
        backend = SharedStackRuntimeBackend(runner_address="sentinel:50051")
        backend._per_trial_mode = True
        assert backend.isolation_mode is IsolationMode.PER_TRIAL_STACK

    def test_per_trial_runtime_backend_isolation_mode_is_per_trial_stack(self) -> None:
        assert PerTrialRuntimeBackend.isolation_mode is IsolationMode.PER_TRIAL_STACK


class TestPlanShapeBranches:
    """Each :class:`PlanShape` classification maps to exactly one posture."""

    def test_single_run_plan_is_shared_stack(self) -> None:
        backend = SharedStackRuntimeBackend(env_manifest=_manifest_with_stacks("run"))
        assert backend.isolation_mode is IsolationMode.SHARED_STACK

    def test_trial_scoped_only_plan_is_per_trial_stack(self) -> None:
        backend = SharedStackRuntimeBackend(env_manifest=_manifest_with_stacks("trial"))
        assert backend.isolation_mode is IsolationMode.PER_TRIAL_STACK

    def test_task_scoped_only_plan_is_composed_stack(self) -> None:
        backend = SharedStackRuntimeBackend(env_manifest=_manifest_with_stacks("task"))
        assert backend.isolation_mode is IsolationMode.COMPOSED_STACK

    def test_multi_scope_plan_is_composed_stack(self) -> None:
        backend = SharedStackRuntimeBackend(env_manifest=_manifest_with_stacks("run", "trial"))
        assert backend.isolation_mode is IsolationMode.COMPOSED_STACK
