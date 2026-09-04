"""Scaffolding lock for :func:`run_via_snapshot_rpc` and friends.

Proves the snapshot-leg helpers wire up end-to-end on one canonical pack
(``rubric_only``) before the Stage 2 canonical parametrised tests broaden
across the 10-pack matrix:

* :func:`run_via_snapshot_rpc` returns a :class:`grader_pb2.Grade`;
* :func:`produce_snapshot_bundle` materialises a bundle a downstream
  :func:`run_via_snapshot_rpc` reads via ``bundle_dir_override``;
* the composite's ``_substrate_cls`` factory constructs a fresh
  :class:`SnapshotGradingSubstrate` per grade invocation (Lane C's
  regrade-parity property depends on this — the composite closes its
  substrate in ``finally`` and a reused instance would collapse after the
  first close).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.utils.grader_parity_harness import (
    load_parity_pack,
    produce_snapshot_bundle,
    run_via_snapshot_rpc,
)
from tolokaforge.core.grading import substrate as substrate_module
from tolokaforge.grader import grader_pb2

_RUBRIC_ONLY_PACK_DIR = (
    Path(__file__).resolve().parents[2] / "canonical" / "grader_parity_baselines" / "rubric_only"
)


def test_run_via_snapshot_rpc_shape_and_fresh_substrate_per_invocation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pack = load_parity_pack(_RUBRIC_ONLY_PACK_DIR)

    construction_count = 0
    original_init = substrate_module.SnapshotGradingSubstrate.__init__

    def counting_init(self, bundle_view):  # type: ignore[no-untyped-def]
        nonlocal construction_count
        construction_count += 1
        original_init(self, bundle_view)

    monkeypatch.setattr(substrate_module.SnapshotGradingSubstrate, "__init__", counting_init)

    first_grade = run_via_snapshot_rpc(pack, monkeypatch=monkeypatch)
    assert isinstance(first_grade, grader_pb2.Grade)

    bundle_dir = tmp_path / "regrade-bundle"
    produce_snapshot_bundle(pack, monkeypatch=monkeypatch, bundle_dir=bundle_dir)
    assert (bundle_dir / "manifest.json").is_file()

    replay_grade = run_via_snapshot_rpc(
        pack, monkeypatch=monkeypatch, bundle_dir_override=bundle_dir
    )
    assert isinstance(replay_grade, grader_pb2.Grade)

    assert construction_count >= 2, (
        "expected at least two SnapshotGradingSubstrate constructions across "
        "one full run and one replay; got "
        f"{construction_count} — factory reused an instance, which would "
        "collapse Lane C's regrade-parity property"
    )
