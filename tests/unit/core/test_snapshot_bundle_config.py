"""Unit tests for ``SnapshotBundleConfig`` — the ``grader.snapshot`` subblock
that turns on trial-end grade-bundle production.

Locks the discriminated-union parse rules and the ``build_store`` factory
that materialises a concrete :class:`BundleStore` from the config's
``store`` variant. The config is a compatibility surface (task-pack
authors and run-config authors read it in ``docs/CONFIG.md``); shape
drift here reads as a silent contract break for every downstream config.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tolokaforge.core.grading.bundle_store import LocalDiskBundleStore, S3BundleStore
from tolokaforge.core.models.run_config import (
    GraderConfig,
    LocalDiskBundleStoreConfig,
    S3BundleStoreConfig,
    SnapshotBundleConfig,
)

pytestmark = pytest.mark.unit


class TestSnapshotBundleConfigDefaults:
    def test_grader_snapshot_defaults_to_none(self) -> None:
        assert GraderConfig().snapshot is None

    def test_defaults_are_opt_out(self) -> None:
        cfg = SnapshotBundleConfig()
        assert cfg.enabled is False
        assert cfg.store is None
        assert cfg.max_bundle_mb == 32.0
        assert cfg.fallback_on_oversize == "live_callback"


class TestSnapshotBundleConfigValidation:
    def test_enabled_requires_store(self) -> None:
        with pytest.raises(ValidationError, match="requires grader.snapshot.store"):
            SnapshotBundleConfig(enabled=True, store=None)

    def test_max_bundle_mb_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            SnapshotBundleConfig(max_bundle_mb=0)

    def test_max_bundle_mb_rejects_negative(self) -> None:
        with pytest.raises(ValidationError):
            SnapshotBundleConfig(max_bundle_mb=-1.0)


class TestBundleStoreDiscriminator:
    def test_local_disk_discriminator(self, tmp_path) -> None:
        cfg = SnapshotBundleConfig.model_validate(
            {
                "enabled": True,
                "store": {"type": "local_disk", "root_dir": str(tmp_path)},
            }
        )
        assert isinstance(cfg.store, LocalDiskBundleStoreConfig)
        assert cfg.store.root_dir == str(tmp_path)

    def test_s3_discriminator(self) -> None:
        cfg = SnapshotBundleConfig.model_validate(
            {
                "enabled": True,
                "store": {"type": "s3", "bucket": "my-buk", "prefix": "grade_bundles"},
            }
        )
        assert isinstance(cfg.store, S3BundleStoreConfig)
        assert cfg.store.bucket == "my-buk"

    def test_local_disk_rejects_s3_field(self, tmp_path) -> None:
        """A mis-tagged input (``bucket`` on a ``local_disk`` block) fails at
        load-time, not at ``build_store`` time — the discriminator +
        extras=forbid combination is the type-safety guarantee."""
        with pytest.raises(ValidationError):
            SnapshotBundleConfig.model_validate(
                {
                    "enabled": True,
                    "store": {
                        "type": "local_disk",
                        "root_dir": str(tmp_path),
                        "bucket": "wrong",
                    },
                }
            )


class TestBuildStoreFactory:
    def test_build_local_disk_store(self, tmp_path) -> None:
        cfg = SnapshotBundleConfig(
            enabled=True,
            store=LocalDiskBundleStoreConfig(root_dir=str(tmp_path)),
        )
        store = cfg.build_store()
        assert isinstance(store, LocalDiskBundleStore)
        assert store.name == "local_disk"
        assert store.root_dir == tmp_path.resolve()

    def test_build_s3_store(self) -> None:
        pytest.importorskip("boto3")
        cfg = SnapshotBundleConfig(
            enabled=True,
            store=S3BundleStoreConfig(bucket="my-buk", prefix="grade_bundles"),
        )
        store = cfg.build_store()
        assert isinstance(store, S3BundleStore)
        assert store.name == "s3"
        assert store.bucket == "my-buk"

    def test_build_store_refuses_no_store(self) -> None:
        cfg = SnapshotBundleConfig()  # enabled=False, store=None — valid
        with pytest.raises(ValueError, match="requires .store to be set"):
            cfg.build_store()
