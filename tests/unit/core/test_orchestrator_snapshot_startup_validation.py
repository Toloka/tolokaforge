"""Unit tests for ``Orchestrator._validate_snapshot_mode_compatibility``.

Three gates fire at run-start when ``grader.snapshot.enabled=true``:

- ``grader.expose_substrate`` must be ``True`` — the producer composes
  ``SubstrateService`` reads.
- The resolved runtime backend must implement ``build_grade_bundle`` —
  probed with a fake trial id; ``NotImplementedError`` opts out.
- The resolved bundle store must answer ``probe()`` — S3 ``head_bucket``,
  LocalDisk sentinel write+delete. Any exception (including
  ``AttributeError`` from an out-of-tree plugin that hasn't upgraded)
  wraps into a single actionable :class:`ValueError`.

Each fails loud at run-start so a snapshot-configured run does not
silently record ``produce_failed`` for every trial.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tolokaforge.core.grading.bundle_store import BundleStoreUnreachableError
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
        with patch.object(SnapshotBundleConfig, "build_store") as build_store:
            orch._validate_snapshot_mode_compatibility(backend)
        backend.build_grade_bundle.assert_not_called()
        build_store.assert_not_called()


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
        # trial id — the gate treats that (and the shared-stack's pre-connect
        # ``RuntimeError``) as "backend supports snapshot mode".
        backend.build_grade_bundle.side_effect = KeyError("trial not registered")
        orch._validate_snapshot_mode_compatibility(backend)
        backend.build_grade_bundle.assert_called_once()
        _args, kwargs = backend.build_grade_bundle.call_args
        assert kwargs["trial_id"] == "__snapshot_probe__"

    def test_accepts_snapshot_mode_when_backend_raises_pre_connect_runtime_error(
        self, tmp_path
    ) -> None:
        """Shared-stack backend probed before ``connect()`` raises
        ``RuntimeError("build_grade_bundle called before connect()")`` —
        the gate treats it as capability-present."""
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
        backend.build_grade_bundle.side_effect = RuntimeError(
            "SharedStackRuntimeBackend.build_grade_bundle called before connect()."
        )
        orch._validate_snapshot_mode_compatibility(backend)

    def test_propagates_unexpected_backend_exception(self, tmp_path) -> None:
        """A non-``NotImplementedError`` / ``KeyError`` / ``RuntimeError``
        exception from the probe now propagates instead of being silently
        swallowed as "backend supports snapshot mode". Genuine backend
        bugs surface at run-start, not per-trial at grade time."""
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
        backend.build_grade_bundle.side_effect = ValueError("unexpected backend bug")
        with pytest.raises(ValueError, match="unexpected backend bug"):
            orch._validate_snapshot_mode_compatibility(backend)


class TestBundleStoreProbeGate:
    """Third gate: the resolved bundle store must answer ``probe()``.

    Every reachability failure — shipped-store ``BundleStoreUnreachableError``,
    missing ``bundle-store-s3`` extra ``RuntimeError``, or a stale plugin
    without ``probe()`` (``AttributeError``) — collapses into one
    actionable :class:`ValueError`. The orchestrator wire-up catches
    :class:`Exception` uniformly so the ``grader-detach`` import-linter
    contract stays green with zero new imports on ``orchestrator.py``;
    ``__cause__`` preserves the underlying diagnostic.
    """

    def _make_snapshot_orch(self, tmp_path) -> Orchestrator:
        return Orchestrator(
            _make_run_config(
                snapshot=SnapshotBundleConfig(
                    enabled=True,
                    store=LocalDiskBundleStoreConfig(root_dir=str(tmp_path)),
                ),
                expose_substrate=True,
            )
        )

    def _capability_present_backend(self) -> MagicMock:
        backend = MagicMock()
        backend.build_grade_bundle.side_effect = KeyError("trial not registered")
        return backend

    def test_refuses_when_store_probe_raises_unreachable(self, tmp_path) -> None:
        orch = self._make_snapshot_orch(tmp_path)
        backend = self._capability_present_backend()
        store = MagicMock()
        store.probe.side_effect = BundleStoreUnreachableError("bucket unreachable")
        with patch.object(SnapshotBundleConfig, "build_store", return_value=store):
            with pytest.raises(ValueError, match=r"grader\.snapshot\.store.*not reachable") as ei:
                orch._validate_snapshot_mode_compatibility(backend)
        assert "local_disk" in str(ei.value)
        assert "bucket unreachable" in str(ei.value)
        store.close.assert_called_once()

    def test_accepts_when_store_probe_returns_cleanly(self, tmp_path) -> None:
        orch = self._make_snapshot_orch(tmp_path)
        backend = self._capability_present_backend()
        store = MagicMock()
        store.probe.return_value = None
        with patch.object(SnapshotBundleConfig, "build_store", return_value=store):
            orch._validate_snapshot_mode_compatibility(backend)
        store.probe.assert_called_once()
        store.close.assert_called_once()

    def test_refuses_when_store_probe_raises_missing_extra_runtime_error(self, tmp_path) -> None:
        """Missing ``bundle-store-s3`` extra: ``_boto3_client()`` raises
        the install-hint ``RuntimeError``. The wrapper collapses it into
        the same actionable ``ValueError`` shape (one re-raise contract)."""
        orch = self._make_snapshot_orch(tmp_path)
        backend = self._capability_present_backend()
        store = MagicMock()
        store.probe.side_effect = RuntimeError("S3BundleStore requires boto3.")
        with patch.object(SnapshotBundleConfig, "build_store", return_value=store):
            with pytest.raises(ValueError, match=r"grader\.snapshot\.store.*not reachable"):
                orch._validate_snapshot_mode_compatibility(backend)

    def test_refuses_when_unupgraded_plugin_lacks_probe_attribute(self, tmp_path) -> None:
        """Regression lock: an out-of-tree plugin that upgraded tolokaforge
        without adding ``probe()`` raises ``AttributeError`` at the
        ``store.probe()`` call site. The wrapper turns that into the
        same actionable ``ValueError``; ``__cause__`` preserves the
        ``AttributeError`` so plugin authors see the missing method."""
        orch = self._make_snapshot_orch(tmp_path)
        backend = self._capability_present_backend()
        store = MagicMock(spec=["put", "get", "close"])
        with patch.object(SnapshotBundleConfig, "build_store", return_value=store):
            with pytest.raises(ValueError, match=r"grader\.snapshot\.store.*not reachable") as ei:
                orch._validate_snapshot_mode_compatibility(backend)
        assert isinstance(ei.value.__cause__, AttributeError)
        assert "probe" in str(ei.value.__cause__)

    def test_store_probe_runs_after_backend_probe(self, tmp_path) -> None:
        """Ordering: with a capability-present backend AND a store whose
        probe raises, the orchestrator raises the store-probe ``ValueError``
        (proving the backend probe ran first and passed, then the store
        probe ran second and failed)."""
        orch = self._make_snapshot_orch(tmp_path)
        backend = self._capability_present_backend()
        store = MagicMock()
        store.probe.side_effect = BundleStoreUnreachableError("bucket unreachable")
        with patch.object(SnapshotBundleConfig, "build_store", return_value=store):
            with pytest.raises(ValueError, match=r"grader\.snapshot\.store.*not reachable"):
                orch._validate_snapshot_mode_compatibility(backend)
        backend.build_grade_bundle.assert_called_once()
        store.probe.assert_called_once()
