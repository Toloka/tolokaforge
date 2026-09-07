"""Mechanical guardrail on the two load-bearing snapshot-mode opt-in defaults.

Locks that a PR flipping ``SnapshotBundleConfig.enabled`` or
``GraderConfig.expose_substrate`` (or making ``GraderConfig.snapshot``
present by default) must also edit this file. Both defaults are
security-relevant: ``expose_substrate=true`` opens the runner's
:class:`SubstrateService` gRPC surface, and ``snapshot.enabled=true``
turns on the orchestrator's grade-bundle producer seam. A brownfield
deploy that accidentally opens either would surface here first, before
the change reached a running system.

Assertions read through :attr:`BaseModel.model_fields`, not through a
fresh instance's :meth:`model_dump`, so a rename of the field or a
Pydantic-v2 default-shape change surfaces at the exact assertion — the
same seam the constants that write them live on.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.models.run_config import GraderConfig, SnapshotBundleConfig

pytestmark = pytest.mark.canonical


def test_snapshot_bundle_config_disabled_by_default() -> None:
    assert SnapshotBundleConfig.model_fields["enabled"].default is False
    with pytest.raises(ValueError, match="requires grader.snapshot.store"):
        SnapshotBundleConfig(enabled=True)


def test_grader_config_expose_substrate_off_by_default() -> None:
    assert GraderConfig.model_fields["expose_substrate"].default is False


def test_grader_snapshot_defaults_to_none() -> None:
    assert GraderConfig.model_fields["snapshot"].default is None
