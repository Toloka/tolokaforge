"""Canonical byte-parity: the ``bundle_producer`` helper's end-to-end
produce → load → snapshot-substrate round-trip reads byte-equal to the
source :class:`InProcessGradingSubstrate` on every faithfully-representable
Protocol method.

The parity claim rests on two invariants:

- ``serialize_bundle_from_substrate`` composes the same reads the
  producer's callers will drive at trial-end, and its output is
  byte-identical to a hand-serialised bundle over the same inputs.
- ``normalise_floats`` (``%.6g`` six-significant-digit float
  normalisation) is applied by :func:`serialize_grade_bundle` to every
  JSON payload; the in-process leg's fixture inputs are pre-normalised
  so the parity assertion isolates substrate behaviour rather than
  float encoding. Matches the shipped pattern at
  ``tests/canonical/test_snapshot_substrate_parity.py`` — the fixture
  input carries a non-round float (``0.123456789``) so the round-trip
  would fail if either side skipped the normaliser.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.canonical._bundle_fixtures import synthetic_inputs
from tolokaforge.core.grading.bundle import (
    load_grade_bundle,
    normalise_floats,
)
from tolokaforge.core.grading.bundle_producer import serialize_bundle_from_substrate
from tolokaforge.core.grading.filesystem_view import read_agent_visible_filesystem
from tolokaforge.core.grading.substrate import (
    InProcessGradingSubstrate,
    SnapshotGradingSubstrate,
)

pytestmark = pytest.mark.canonical


class _StubGrading:
    """Structural stand-in for a ``GradingConfig`` — the producer only calls
    ``model_dump(mode="json")`` and treats the result as the bundle's
    ``grading_config`` payload."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def model_dump(self, *, mode: str) -> dict:
        del mode
        return self._payload


class _StubTaskDescription:
    def __init__(self, *, tool_artifacts: dict, grading_payload: dict) -> None:
        self.tool_artifacts = tool_artifacts
        self.grading = _StubGrading(grading_payload)


class _StubTrajectory:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def model_dump(self, *, mode: str) -> dict:
        del mode
        return self._payload


def _paired_substrates(tmp_path: Path):
    inputs = synthetic_inputs(tmp_path)
    fs_root = inputs["filesystem_root"]
    assert isinstance(fs_root, Path)

    canonical_initial = normalise_floats(inputs["initial_state"])
    canonical_final = normalise_floats(inputs["final_state"])
    canonical_stable = normalise_floats(inputs["final_state_stable"])

    source = InProcessGradingSubstrate(
        db_reader=MagicMock(),
        knowledge_search=None,
        filesystem_root=fs_root,
        initial_state=canonical_initial,
        final_state=canonical_final,
        final_state_stable_factory=lambda: canonical_stable,
        filesystem_state_factory=lambda: read_agent_visible_filesystem(fs_root),
    )

    import base64

    tool_artifacts_b64 = {
        rel: base64.b64encode(data).decode() for rel, data in inputs["checks"].items()
    }
    task_description = _StubTaskDescription(
        tool_artifacts=tool_artifacts_b64,
        grading_payload=inputs["grading_config"],
    )
    trajectory = _StubTrajectory(inputs["trajectory"])

    bundle_dir = tmp_path / "bundle"
    serialize_bundle_from_substrate(
        substrate=source,
        trial_id=inputs["trial_id"],
        out_dir=bundle_dir,
        trajectory=trajectory,
        task_description=task_description,
    )
    view = load_grade_bundle(bundle_dir)
    snapshot = SnapshotGradingSubstrate(view)
    return source, snapshot, bundle_dir


def test_produced_bundle_initial_state_parity(tmp_path: Path) -> None:
    source, snapshot, _ = _paired_substrates(tmp_path)
    try:
        assert snapshot.initial_state() == source.initial_state()
    finally:
        snapshot.close()
        source.close()


def test_produced_bundle_final_state_parity(tmp_path: Path) -> None:
    source, snapshot, _ = _paired_substrates(tmp_path)
    try:
        assert snapshot.final_state() == source.final_state()
    finally:
        snapshot.close()
        source.close()


def test_produced_bundle_final_state_stable_parity(tmp_path: Path) -> None:
    source, snapshot, _ = _paired_substrates(tmp_path)
    try:
        assert snapshot.final_state_stable() == source.final_state_stable()
    finally:
        snapshot.close()
        source.close()


def test_produced_bundle_filesystem_state_parity(tmp_path: Path) -> None:
    source, snapshot, _ = _paired_substrates(tmp_path)
    try:
        assert snapshot.filesystem_state() == source.filesystem_state()
    finally:
        snapshot.close()
        source.close()


def test_produced_bundle_carries_tool_artifacts_verbatim(tmp_path: Path) -> None:
    """``TaskDescription.tool_artifacts`` (base64 ``dict[str, str]``) is
    decoded and materialised under ``checks/`` in the produced bundle.
    Byte-for-byte round-trip."""
    _, _, bundle_dir = _paired_substrates(tmp_path)
    expected = synthetic_inputs(tmp_path / "unused")["checks"]
    for rel, data in expected.items():
        assert (bundle_dir / "checks" / rel).read_bytes() == data
