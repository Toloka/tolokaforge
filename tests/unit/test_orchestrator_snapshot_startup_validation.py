"""Unit tests for ``Orchestrator._validate_snapshot_mode_compatibility``.

Two gates fire at run-start when ``grader.snapshot.enabled=true``:

- ``grader.expose_substrate`` must be ``True`` — the producer composes
  ``SubstrateService`` reads.
- The resolved runtime backend must implement ``build_grade_bundle`` —
  probed with a fake trial id; ``NotImplementedError`` opts out.

Both fail loud at run-start with an actionable :class:`ValueError` so a
snapshot-configured run does not silently record ``produce_failed`` for
every trial.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tolokaforge.core.models import (
    EvaluationConfig,
    GraderConfig,
    LocalDiskBundleStoreConfig,
    ModelConfig,
    OrchestratorConfig,
    RunConfig,
    SnapshotBundleConfig,
)
from tolokaforge.core.orchestrator import Orchestrator
from tolokaforge.core.runtime import InMemoryRuntimeBackend

pytestmark = pytest.mark.unit


def _make_run_config(
    *,
    snapshot: SnapshotBundleConfig | None,
    expose_substrate: bool = True,
) -> RunConfig:
    return RunConfig(
        models={"agent": ModelConfig(provider="openai", name="gpt-4")},
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir="/tmp/test_output"),
        grader=GraderConfig(
            expose_substrate=expose_substrate,
            snapshot=snapshot,
        ),
    )


class TestSnapshotDisabled:
    def test_no_grader_block_skips_probe(self) -> None:
        config = RunConfig(
            models={"agent": ModelConfig(provider="openai", name="gpt-4")},
            orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
            evaluation=EvaluationConfig(output_dir="/tmp/test_output"),
        )
        orch = Orchestrator(config)
        backend = MagicMock()
        # Never called; no exception raised.
        orch._validate_snapshot_mode_compatibility(backend)
        backend.build_grade_bundle.assert_not_called()

    def test_snapshot_none_skips_probe(self) -> None:
        orch = Orchestrator(_make_run_config(snapshot=None))
        backend = MagicMock()
        orch._validate_snapshot_mode_compatibility(backend)
        backend.build_grade_bundle.assert_not_called()

    def test_snapshot_disabled_skips_probe(self, tmp_path) -> None:
        orch = Orchestrator(
            _make_run_config(
                snapshot=SnapshotBundleConfig(
                    enabled=False,
                    store=LocalDiskBundleStoreConfig(root_dir=str(tmp_path)),
                ),
            )
        )
        backend = MagicMock()
        orch._validate_snapshot_mode_compatibility(backend)
        backend.build_grade_bundle.assert_not_called()


class TestExposeSubstrateGate:
    def test_refuses_snapshot_mode_when_expose_substrate_false(self, tmp_path) -> None:
        orch = Orchestrator(
            _make_run_config(
                snapshot=SnapshotBundleConfig(
                    enabled=True,
                    store=LocalDiskBundleStoreConfig(root_dir=str(tmp_path)),
                ),
                expose_substrate=False,
            )
        )
        backend = MagicMock()
        with pytest.raises(ValueError, match="expose_substrate=true"):
            orch._validate_snapshot_mode_compatibility(backend)
        backend.build_grade_bundle.assert_not_called()


class TestBackendCapabilityGate:
    def test_refuses_snapshot_mode_when_backend_lacks_hook(self, tmp_path) -> None:
        orch = Orchestrator(
            _make_run_config(
                snapshot=SnapshotBundleConfig(
                    enabled=True,
                    store=LocalDiskBundleStoreConfig(root_dir=str(tmp_path)),
                ),
                expose_substrate=True,
            )
        )
        backend = InMemoryRuntimeBackend()
        with pytest.raises(ValueError, match="build_grade_bundle"):
            orch._validate_snapshot_mode_compatibility(backend)

    def test_accepts_snapshot_mode_when_backend_implements_hook(self, tmp_path) -> None:
        orch = Orchestrator(
            _make_run_config(
                snapshot=SnapshotBundleConfig(
                    enabled=True,
                    store=LocalDiskBundleStoreConfig(root_dir=str(tmp_path)),
                ),
                expose_substrate=True,
            )
        )
        backend = MagicMock()
        # A real impl raises trial-not-registered / KeyError on the probe
        # trial id — the gate treats any non-NotImplementedError as
        # "backend supports snapshot mode".
        backend.build_grade_bundle.side_effect = KeyError("trial not registered")
        orch._validate_snapshot_mode_compatibility(backend)
        backend.build_grade_bundle.assert_called_once()
        _args, kwargs = backend.build_grade_bundle.call_args
        assert kwargs["out_dir"].exists() is False  # cleaned up in finally
