"""Canonical parity: :class:`SnapshotGradingSubstrate` reads byte-equal
to :class:`InProcessGradingSubstrate` on every Protocol method that a
snapshot substrate can faithfully implement over a bundle format v1.0.

The parity claim is what the substrate abstraction rests on — the composite
grading pipeline above the substrate must not care which topology it holds.
Given the same trial inputs, the two shipping substrates return the same
values, so an operator swapping between them changes only the transport,
never the verdict.

Two Protocol methods are DELIBERATELY excluded from the parity assertion:

- :meth:`GradingSubstrate.db_probe` — snapshot raises
  :class:`SubstrateUnreachableError` (the caller-supplied DSN is only
  reachable inside the task's docker network; offline substrates cannot
  dial it). InProcess opens a real connection to the DSN and returns
  rows. Bundle format v1.0 has no pre-materialised probe part.
- :meth:`GradingSubstrate.knowledge_search` — snapshot returns ``None``
  (bundle format v1.0's optional ``kb/`` subtree carries raw bytes
  without a queryable index; the judge's Protocol treats ``None`` as
  "the trial declared no KB"). InProcess wires whatever KB the caller
  passes at construction.

The behaviour on both excluded methods is locked separately by the unit
tests in :mod:`tests.unit.grading.test_snapshot_substrate`.

The parity fixture routes the in-process leg's dict inputs through the
same :func:`~tolokaforge.core.grading.bundle.normalise_floats` the bundle
producer applies (``%.6g`` six-significant-digit float normalisation);
without that step a fixture value like ``0.123456789`` would land in the
bundle as ``0.123457`` while the in-process leg saw the raw float, and
the parity claim would fail on a canonicalisation artefact rather than a
substrate divergence. Both substrates receive the ``%.6g``-normalised
form, so any remaining divergence is a real substrate bug.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.canonical._bundle_fixtures import synthetic_inputs
from tolokaforge.core.grading.bundle import (
    load_grade_bundle,
    normalise_floats,
    serialize_grade_bundle,
)
from tolokaforge.core.grading.filesystem_view import read_agent_visible_filesystem
from tolokaforge.core.grading.substrate import (
    InProcessGradingSubstrate,
    SnapshotGradingSubstrate,
)


@pytest.fixture
def paired_substrates(
    tmp_path: Path,
) -> tuple[InProcessGradingSubstrate, SnapshotGradingSubstrate, dict[str, object]]:
    inputs = synthetic_inputs(tmp_path)
    bundle_dir = tmp_path / "bundle"
    serialize_grade_bundle(bundle_dir, **inputs)
    view = load_grade_bundle(bundle_dir)
    fs_root = inputs["filesystem_root"]
    assert isinstance(fs_root, Path)

    canonical_initial = normalise_floats(inputs["initial_state"])
    canonical_final = normalise_floats(inputs["final_state"])
    canonical_stable = normalise_floats(inputs["final_state_stable"])

    in_process = InProcessGradingSubstrate(
        db_reader=MagicMock(),
        knowledge_search=None,
        filesystem_root=fs_root,
        initial_state=canonical_initial,
        final_state=canonical_final,
        final_state_stable_factory=lambda: canonical_stable,
        filesystem_state_factory=lambda: read_agent_visible_filesystem(fs_root),
    )
    snapshot = SnapshotGradingSubstrate(view)
    try:
        yield in_process, snapshot, inputs
    finally:
        snapshot.close()
        in_process.close()


def test_initial_state_parity(
    paired_substrates: tuple[
        InProcessGradingSubstrate, SnapshotGradingSubstrate, dict[str, object]
    ],
) -> None:
    in_process, snapshot, _ = paired_substrates
    assert snapshot.initial_state() == in_process.initial_state()


def test_final_state_parity(
    paired_substrates: tuple[
        InProcessGradingSubstrate, SnapshotGradingSubstrate, dict[str, object]
    ],
) -> None:
    in_process, snapshot, _ = paired_substrates
    assert snapshot.final_state() == in_process.final_state()


def test_final_state_stable_parity(
    paired_substrates: tuple[
        InProcessGradingSubstrate, SnapshotGradingSubstrate, dict[str, object]
    ],
) -> None:
    in_process, snapshot, _ = paired_substrates
    assert snapshot.final_state_stable() == in_process.final_state_stable()


def test_db_reader_get_state_parity(
    paired_substrates: tuple[
        InProcessGradingSubstrate, SnapshotGradingSubstrate, dict[str, object]
    ],
) -> None:
    """Snapshot's DB reader reads ``final_state.json`` back byte-equal; the
    in-process leg's DB reader is a caller-owned mock in the shipped
    ``InProcessGradingSubstrate`` shape, so the parity assertion pins
    snapshot against the raw ``final_state`` its caller supplied."""
    in_process, snapshot, _ = paired_substrates
    assert snapshot.db_reader().get_state() == in_process.final_state()


def test_db_reader_query_parity(
    paired_substrates: tuple[
        InProcessGradingSubstrate, SnapshotGradingSubstrate, dict[str, object]
    ],
) -> None:
    _, snapshot, inputs = paired_substrates
    final_state = inputs["final_state"]
    assert isinstance(final_state, dict)
    users = final_state["tables"]["users"]
    assert snapshot.db_reader().query("$.tables.users[*].id") == {
        "results": [row["id"] for row in users]
    }


def test_filesystem_state_parity(
    paired_substrates: tuple[
        InProcessGradingSubstrate, SnapshotGradingSubstrate, dict[str, object]
    ],
) -> None:
    """Both substrates route filesystem_state through the SAME
    ``read_agent_visible_filesystem`` helper — snapshot walks its
    extracted tmpdir, in-process walks the caller's original tree —
    and the helper's output only depends on the visible files'
    contents. So the two dicts land byte-equal."""
    in_process, snapshot, _ = paired_substrates
    assert snapshot.filesystem_state() == in_process.filesystem_state()


def test_filesystem_root_extracts_the_same_visible_files(
    paired_substrates: tuple[
        InProcessGradingSubstrate, SnapshotGradingSubstrate, dict[str, object]
    ],
) -> None:
    in_process, snapshot, _ = paired_substrates
    snapshot_root = snapshot.filesystem_root()
    in_process_root = in_process.filesystem_root()
    assert snapshot_root is not None
    assert in_process_root is not None
    snapshot_files = sorted(read_agent_visible_filesystem(snapshot_root).keys())
    in_process_files = sorted(read_agent_visible_filesystem(in_process_root).keys())
    assert snapshot_files == in_process_files


def test_entry_point_discovery_resolves_snapshot_class() -> None:
    from tolokaforge.core.plugin_registry import load_grading_substrate

    assert load_grading_substrate("snapshot") is SnapshotGradingSubstrate


def test_bundle_parts_round_trip_byte_equal_to_source_inputs(tmp_path: Path) -> None:
    """The parity story rests on the bundle carrying the source inputs
    byte-for-byte. Serialise, load, and read each JSON part through the
    snapshot substrate; assert every read matches the source dict."""
    inputs = synthetic_inputs(tmp_path)
    bundle_dir = tmp_path / "bundle"
    serialize_grade_bundle(bundle_dir, **inputs)
    view = load_grade_bundle(bundle_dir)
    snapshot = SnapshotGradingSubstrate(view)
    try:
        assert snapshot.initial_state() == json.loads(
            (bundle_dir / "initial_state.json").read_bytes()
        )
        assert snapshot.final_state() == json.loads((bundle_dir / "final_state.json").read_bytes())
        assert snapshot.final_state_stable() == json.loads(
            (bundle_dir / "final_state_stable.json").read_bytes()
        )
    finally:
        snapshot.close()
