"""Backend-capability registry and admission gate.

Pins the closed vocabulary of :data:`CAPABILITY_REGISTRY` and every
branch of :func:`check_admission` — unknown names fail loud, missing
advertisements fail loud, subset requests pass silently. Also locks
every plan-shape branch of :attr:`SharedStackRuntimeBackend.advertised_capabilities`
(computed from :attr:`EnvironmentManifest.plan_shape` or the
``_per_trial_mode`` flag).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.core.backend_capabilities import (
    CAPABILITY_REGISTRY,
    LOCAL_DOCKER_ADVERTISED,
    check_admission,
)
from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend
from tolokaforge.core.shared_stack_runtime import (
    NETWORK_CAPABILITIES,
    RESET_RECIPE_CAPABILITIES,
    SharedStackRuntimeBackend,
)
from tolokaforge.core.trial import EnvironmentManifest
from tolokaforge.runner.models import StackDecl, StackScope

pytestmark = pytest.mark.unit


_FIXTURES = Path(__file__).parent.parent / "canonical" / "fixtures" / "environment_manifest"
_COMPOSE_FILE = _FIXTURES / "safe_two_service.yaml"


def _manifest_with_stacks(*scopes: StackScope) -> EnvironmentManifest:
    """Build a manifest whose ``stacks`` list carries one :class:`StackDecl`
    per requested scope. Mirrors the helper in
    ``test_shared_stack_runtime_isolation_mode.py`` so :attr:`plan_shape`
    reads directly from the passed scope tuple.
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


class TestRegistryVocabulary:
    def test_registry_contains_baseline_names(self) -> None:
        expected = {
            "per_trial_stack",
            "shared_stack",
            "composed_stack",
            "reset_recipes:sql_dump",
            "reset_recipes:filesystem_dir",
            "reset_recipes:redis_dump",
            "reset_recipes:bare",
            "network_isolation:no_internet",
            "network_isolation:limited_internet",
        }
        assert expected.issubset(CAPABILITY_REGISTRY.keys())

    def test_local_docker_advertises_registry_subset(self) -> None:
        assert LOCAL_DOCKER_ADVERTISED.issubset(CAPABILITY_REGISTRY.keys())


class TestBackendAdvertisements:
    def test_per_trial_advertises_per_trial_stack(self) -> None:
        assert "per_trial_stack" in PerTrialRuntimeBackend.advertised_capabilities
        assert "shared_stack" not in PerTrialRuntimeBackend.advertised_capabilities

    def test_shared_stack_advertises_shared_stack(self) -> None:
        backend = SharedStackRuntimeBackend(runner_address="sentinel:50051")
        assert "shared_stack" in backend.advertised_capabilities
        assert "per_trial_stack" not in backend.advertised_capabilities

    def test_shared_stack_does_not_advertise_reset_recipes(self) -> None:
        backend = SharedStackRuntimeBackend(runner_address="sentinel:50051")
        assert "reset_recipes:sql_dump" not in backend.advertised_capabilities

    def test_per_trial_advertises_reset_recipes(self) -> None:
        assert "reset_recipes:sql_dump" in PerTrialRuntimeBackend.advertised_capabilities

    def test_both_docker_backends_advertise_network_isolation(self) -> None:
        backends = (
            PerTrialRuntimeBackend(),
            SharedStackRuntimeBackend(runner_address="sentinel:50051"),
        )
        for backend in backends:
            assert "network_isolation:no_internet" in backend.advertised_capabilities
            assert "network_isolation:limited_internet" in backend.advertised_capabilities


class TestSharedStackAdvertisementsByPlanShape:
    """Each plan-shape branch of :attr:`SharedStackRuntimeBackend.advertised_capabilities`
    resolves to a closed frozenset — locked byte-identically via ``==``.
    """

    def test_built_in_stack_mode_advertises_shared_stack_only(self) -> None:
        backend = SharedStackRuntimeBackend(runner_address="sentinel:50051")
        assert backend.advertised_capabilities == frozenset({"shared_stack"}) | NETWORK_CAPABILITIES

    def test_per_trial_delegate_advertises_per_trial_and_reset_recipes(self) -> None:
        backend = SharedStackRuntimeBackend(runner_address="sentinel:50051")
        backend._per_trial_mode = True
        assert backend.advertised_capabilities == (
            frozenset({"per_trial_stack"}) | RESET_RECIPE_CAPABILITIES | NETWORK_CAPABILITIES
        )

    def test_single_run_plan_advertises_shared_stack_only(self) -> None:
        backend = SharedStackRuntimeBackend(env_manifest=_manifest_with_stacks("run"))
        assert backend.advertised_capabilities == frozenset({"shared_stack"}) | NETWORK_CAPABILITIES

    def test_trial_scoped_only_plan_advertises_per_trial_and_reset_recipes(self) -> None:
        backend = SharedStackRuntimeBackend(env_manifest=_manifest_with_stacks("trial"))
        assert backend.advertised_capabilities == (
            frozenset({"per_trial_stack"}) | RESET_RECIPE_CAPABILITIES | NETWORK_CAPABILITIES
        )

    def test_task_scoped_only_plan_advertises_composed_stack_and_reset_recipes(self) -> None:
        backend = SharedStackRuntimeBackend(env_manifest=_manifest_with_stacks("task"))
        assert backend.advertised_capabilities == (
            frozenset({"composed_stack"}) | RESET_RECIPE_CAPABILITIES | NETWORK_CAPABILITIES
        )

    def test_multi_scope_plan_advertises_composed_stack_and_reset_recipes(self) -> None:
        backend = SharedStackRuntimeBackend(env_manifest=_manifest_with_stacks("run", "trial"))
        assert backend.advertised_capabilities == (
            frozenset({"composed_stack"}) | RESET_RECIPE_CAPABILITIES | NETWORK_CAPABILITIES
        )


class TestCapabilityRegistry:
    def test_composed_stack_capability_registered(self) -> None:
        assert "composed_stack" in CAPABILITY_REGISTRY
        assert CAPABILITY_REGISTRY["composed_stack"].description.startswith(
            "Backend materialises stacks per plan scope"
        )


class TestAdmissionGate:
    def test_empty_request_passes(self) -> None:
        check_admission([], frozenset({"per_trial_stack"}))

    def test_subset_request_passes_with_bare_strings(self) -> None:
        advertised = frozenset({"per_trial_stack", "reset_recipes:sql_dump"})
        check_admission(["per_trial_stack"], advertised)

    def test_subset_request_passes_with_param_dict(self) -> None:
        advertised = frozenset({"network_isolation:no_internet"})
        check_admission([{"network_isolation:no_internet": {}}], advertised)

    def test_unknown_name_fails_loud(self) -> None:
        advertised = frozenset({"per_trial_stack"})
        with pytest.raises(RuntimeError, match="Unknown compute.capabilities") as exc:
            check_admission(["gpu.h100"], advertised)
        assert "gpu.h100" in str(exc.value)

    def test_missing_advertisement_fails_loud(self) -> None:
        advertised = frozenset({"per_trial_stack"})
        with pytest.raises(RuntimeError, match="does not advertise") as exc:
            check_admission(["reset_recipes:sql_dump"], advertised)
        assert "reset_recipes:sql_dump" in str(exc.value)

    def test_multiple_offenders_all_named(self) -> None:
        advertised = frozenset({"per_trial_stack"})
        with pytest.raises(RuntimeError, match="does not advertise") as exc:
            check_admission(["reset_recipes:sql_dump", "shared_stack"], advertised)
        message = str(exc.value)
        assert "reset_recipes:sql_dump" in message
        assert "shared_stack" in message

    def test_composed_stack_capability_admissible(self) -> None:
        check_admission(["composed_stack"], frozenset({"composed_stack"}))
        with pytest.raises(RuntimeError, match="does not advertise") as exc:
            check_admission(["composed_stack"], frozenset())
        assert "composed_stack" in str(exc.value)
