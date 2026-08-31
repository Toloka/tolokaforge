"""Backend-capability registry and admission gate.

Pins the closed vocabulary of :data:`CAPABILITY_REGISTRY` and every
branch of :func:`check_admission` — unknown names fail loud, missing
advertisements fail loud, subset requests pass silently.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.backend_capabilities import (
    CAPABILITY_REGISTRY,
    LOCAL_DOCKER_ADVERTISED,
    check_admission,
)
from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend
from tolokaforge.core.shared_stack_runtime import SharedStackRuntimeBackend

pytestmark = pytest.mark.unit


class TestRegistryVocabulary:
    def test_registry_contains_baseline_names(self) -> None:
        expected = {
            "per_trial_stack",
            "shared_stack",
            "hybrid_stack",
            "reset_recipes:sql_dump",
            "reset_recipes:filesystem_dir",
            "reset_recipes:redis_dump",
            "reset_recipes:bare",
            "network_isolation:no_internet",
            "network_isolation:limited_internet",
        }
        assert expected.issubset(CAPABILITY_REGISTRY.keys())

    def test_hybrid_stack_is_registered(self) -> None:
        """New capability entry added in #1365. HybridRuntimeBackend
        (registered in #1366) will advertise it."""
        spec = CAPABILITY_REGISTRY["hybrid_stack"]
        assert "shared engine services" in spec.description
        assert "task-declared services per trial" in spec.description

    def test_local_docker_advertises_registry_subset(self) -> None:
        assert LOCAL_DOCKER_ADVERTISED.issubset(CAPABILITY_REGISTRY.keys())


class TestBackendAdvertisements:
    def test_per_trial_advertises_per_trial_stack(self) -> None:
        assert "per_trial_stack" in PerTrialRuntimeBackend.advertised_capabilities
        assert "shared_stack" not in PerTrialRuntimeBackend.advertised_capabilities

    def test_shared_stack_advertises_shared_stack(self) -> None:
        assert "shared_stack" in SharedStackRuntimeBackend.advertised_capabilities
        assert "per_trial_stack" not in SharedStackRuntimeBackend.advertised_capabilities

    def test_shared_stack_does_not_advertise_reset_recipes(self) -> None:
        assert "reset_recipes:sql_dump" not in SharedStackRuntimeBackend.advertised_capabilities

    def test_per_trial_advertises_reset_recipes(self) -> None:
        assert "reset_recipes:sql_dump" in PerTrialRuntimeBackend.advertised_capabilities

    def test_both_docker_backends_advertise_network_isolation(self) -> None:
        for backend in (PerTrialRuntimeBackend, SharedStackRuntimeBackend):
            assert "network_isolation:no_internet" in backend.advertised_capabilities
            assert "network_isolation:limited_internet" in backend.advertised_capabilities


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
