"""Unit tests for the ``RuntimeBackend.build_grade_bundle`` +
``remember_trial_inputs`` opt-in hooks — every shipped impl.

The Protocol grows two per-trial methods for the trial-end snapshot
producer seam. Every shipped backend adopts them: the two production
backends materialise the bundle via
:class:`LiveRunnerCallbackGradingSubstrate`; the in-memory test fixture
opts out with :class:`NotImplementedError` and the orchestrator's
startup gate refuses snapshot mode against it. These tests lock the
adoption pattern so a future backend that grows the Protocol without
either shape trips at collection.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.core.per_trial_runtime import PerTrialRuntimeBackend
from tolokaforge.core.runtime import InMemoryRuntimeBackend, RuntimeBackend
from tolokaforge.core.shared_stack_runtime import SharedStackRuntimeBackend

pytestmark = pytest.mark.unit


class TestProtocolDeclaresHook:
    def test_runtime_backend_protocol_declares_build_grade_bundle(self) -> None:
        assert "build_grade_bundle" in dir(RuntimeBackend)

    def test_runtime_backend_protocol_declares_remember_trial_inputs(self) -> None:
        assert "remember_trial_inputs" in dir(RuntimeBackend)


class TestInMemoryRuntimeBackendOptsOut:
    def test_build_grade_bundle_raises_not_implemented(self, tmp_path: Path) -> None:
        backend = InMemoryRuntimeBackend()
        with pytest.raises(NotImplementedError, match="build_grade_bundle"):
            backend.build_grade_bundle("trial-1", out_dir=tmp_path)

    def test_remember_trial_inputs_records_call(self) -> None:
        backend = InMemoryRuntimeBackend()
        trajectory = MagicMock()
        task_description = MagicMock()
        backend.remember_trial_inputs("trial-1", trajectory, task_description)
        assert backend.call_log.remembered_trial_inputs == ["trial-1"]

    def test_remember_trial_inputs_accumulates_calls(self) -> None:
        backend = InMemoryRuntimeBackend()
        backend.remember_trial_inputs("trial-1", MagicMock(), MagicMock())
        backend.remember_trial_inputs("trial-2", MagicMock(), MagicMock())
        assert backend.call_log.remembered_trial_inputs == ["trial-1", "trial-2"]


class TestSharedStackRuntimeBackendImpl:
    def test_build_grade_bundle_composes_substrate_reads(self, tmp_path: Path) -> None:
        backend = SharedStackRuntimeBackend(runner_address="fake-runner:50051")
        trajectory = MagicMock()
        task_description = MagicMock()
        backend.remember_trial_inputs("trial-1", trajectory, task_description)

        expected_manifest = object()
        fake_substrate_instance = MagicMock()
        with (
            patch(
                "tolokaforge.core.shared_stack_runtime.LiveRunnerCallbackGradingSubstrate",
                return_value=fake_substrate_instance,
            ) as fake_substrate_cls,
            patch(
                "tolokaforge.core.shared_stack_runtime.serialize_bundle_from_substrate",
                return_value=expected_manifest,
            ) as fake_serialise,
        ):
            result = backend.build_grade_bundle("trial-1", out_dir=tmp_path / "bundle")

        assert result is expected_manifest
        fake_substrate_cls.assert_called_once_with("fake-runner:50051", "trial-1")
        fake_serialise.assert_called_once_with(
            substrate=fake_substrate_instance,
            trial_id="trial-1",
            out_dir=tmp_path / "bundle",
            trajectory=trajectory,
            task_description=task_description,
        )
        fake_substrate_instance.close.assert_called_once()

    def test_build_grade_bundle_closes_substrate_on_serialise_failure(self, tmp_path: Path) -> None:
        backend = SharedStackRuntimeBackend(runner_address="fake-runner:50051")
        backend.remember_trial_inputs("trial-1", MagicMock(), MagicMock())

        fake_substrate = MagicMock()
        with (
            patch(
                "tolokaforge.core.shared_stack_runtime.LiveRunnerCallbackGradingSubstrate",
                return_value=fake_substrate,
            ),
            patch(
                "tolokaforge.core.shared_stack_runtime.serialize_bundle_from_substrate",
                side_effect=RuntimeError("boom"),
            ),
            pytest.raises(RuntimeError, match="boom"),
        ):
            backend.build_grade_bundle("trial-1", out_dir=tmp_path / "bundle")

        fake_substrate.close.assert_called_once()

    def test_build_grade_bundle_refuses_unremembered_trial(self, tmp_path: Path) -> None:
        backend = SharedStackRuntimeBackend(runner_address="fake-runner:50051")
        with pytest.raises(KeyError, match="no remembered trajectory"):
            backend.build_grade_bundle("unknown-trial", out_dir=tmp_path)

    def test_cleanup_trial_clears_pending_inputs(self) -> None:
        backend = SharedStackRuntimeBackend(runner_address="fake-runner:50051")
        backend.remember_trial_inputs("trial-1", MagicMock(), MagicMock())
        backend.runner_client = MagicMock()
        backend.runner_client.cleanup_trial.return_value = {"success": True, "error": None}
        backend.cleanup_trial("trial-1")
        assert "trial-1" not in backend._pending_trajectories
        assert "trial-1" not in backend._pending_task_descriptions


class TestPerTrialRuntimeBackendImpl:
    def test_build_grade_bundle_composes_substrate_reads(self, tmp_path: Path) -> None:
        backend = PerTrialRuntimeBackend()
        trajectory = MagicMock()
        task_description = MagicMock()
        backend.remember_trial_inputs("trial-1", trajectory, task_description)

        fake_client = MagicMock()
        fake_client.runner_address = "per-trial-runner:50051"
        # _client_for calls connect() on first use; assign directly and mark connected.
        backend._clients["trial-1"] = fake_client
        backend._connected_trials.add("trial-1")

        expected_manifest = object()
        fake_substrate_instance = MagicMock()
        with (
            patch(
                "tolokaforge.core.per_trial_runtime.LiveRunnerCallbackGradingSubstrate",
                return_value=fake_substrate_instance,
            ) as fake_substrate_cls,
            patch(
                "tolokaforge.core.per_trial_runtime.serialize_bundle_from_substrate",
                return_value=expected_manifest,
            ) as fake_serialise,
        ):
            result = backend.build_grade_bundle("trial-1", out_dir=tmp_path / "bundle")

        assert result is expected_manifest
        fake_substrate_cls.assert_called_once_with("per-trial-runner:50051", "trial-1")
        fake_serialise.assert_called_once()
        fake_substrate_instance.close.assert_called_once()

    def test_cleanup_trial_clears_pending_inputs(self) -> None:
        backend = PerTrialRuntimeBackend()
        backend.remember_trial_inputs("trial-1", MagicMock(), MagicMock())
        backend.cleanup_trial("trial-1")
        assert "trial-1" not in backend._pending_trajectories
        assert "trial-1" not in backend._pending_task_descriptions
