"""Behaviour locks for :class:`SnapshotGradingSubstrate`.

Twenty tests cover every Protocol method the snapshot substrate ships over
a bundle produced by :func:`serialize_grade_bundle`, plus the fail-loud
translator that turns bundle-read failures into
:class:`SubstrateUnreachableError` (the sole exception the composite
dispatch propagates unswallowed).
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
from pathlib import Path
from typing import Any

import pytest

from tests.canonical._bundle_fixtures import synthetic_inputs
from tolokaforge.core.grading.bundle import (
    BUNDLE_SCHEMA_VERSION,
    GradeBundleManifest,
    GradeBundleView,
    load_grade_bundle,
    serialize_grade_bundle,
)
from tolokaforge.core.grading.filesystem_view import read_agent_visible_filesystem
from tolokaforge.core.grading.substrate import (
    GradingSubstrate,
    SnapshotGradingSubstrate,
    SubstrateUnreachableError,
)


def _write_bundle(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    inputs = synthetic_inputs(tmp_path)
    bundle_dir = tmp_path / "bundle"
    serialize_grade_bundle(bundle_dir, **inputs)
    return bundle_dir, inputs


def test_satisfies_the_grading_substrate_protocol(tmp_path: Path) -> None:
    bundle_dir, _ = _write_bundle(tmp_path)
    view = load_grade_bundle(bundle_dir)
    substrate = SnapshotGradingSubstrate(view)
    try:
        assert isinstance(substrate, GradingSubstrate)
    finally:
        substrate.close()


def test_initial_state_matches_bundle_part(tmp_path: Path) -> None:
    bundle_dir, _ = _write_bundle(tmp_path)
    view = load_grade_bundle(bundle_dir)
    substrate = SnapshotGradingSubstrate(view)
    try:
        expected = json.loads((bundle_dir / "initial_state.json").read_bytes())
        assert substrate.initial_state() == expected
    finally:
        substrate.close()


def test_final_state_matches_bundle_part(tmp_path: Path) -> None:
    bundle_dir, _ = _write_bundle(tmp_path)
    view = load_grade_bundle(bundle_dir)
    substrate = SnapshotGradingSubstrate(view)
    try:
        expected = json.loads((bundle_dir / "final_state.json").read_bytes())
        assert substrate.final_state() == expected
    finally:
        substrate.close()


def test_final_state_stable_matches_bundle_part(tmp_path: Path) -> None:
    bundle_dir, _ = _write_bundle(tmp_path)
    view = load_grade_bundle(bundle_dir)
    substrate = SnapshotGradingSubstrate(view)
    try:
        expected = json.loads((bundle_dir / "final_state_stable.json").read_bytes())
        assert substrate.final_state_stable() == expected
    finally:
        substrate.close()


class _CountingView(GradeBundleView):
    """A view subclass that counts ``open_part`` calls per rel_path.

    :class:`GradeBundleView` is a frozen dataclass; monkeypatching its
    ``open_part`` attribute would fail, so tests that need call-count
    observability subclass the view instead.
    """

    def __init__(self, inner: GradeBundleView) -> None:
        object.__setattr__(self, "bundle_dir", inner.bundle_dir)
        object.__setattr__(self, "manifest", inner.manifest)
        object.__setattr__(self, "_call_counts", {})

    def open_part(self, rel_path: str) -> bytes:
        counts: dict[str, int] = object.__getattribute__(self, "_call_counts")
        counts[rel_path] = counts.get(rel_path, 0) + 1
        return super().open_part(rel_path)

    def count(self, rel_path: str) -> int:
        counts: dict[str, int] = object.__getattribute__(self, "_call_counts")
        return counts.get(rel_path, 0)


def test_final_state_and_final_state_stable_are_memoised(tmp_path: Path) -> None:
    bundle_dir, _ = _write_bundle(tmp_path)
    view = _CountingView(load_grade_bundle(bundle_dir))
    substrate = SnapshotGradingSubstrate(view)
    try:
        substrate.final_state()
        substrate.final_state()
        substrate.final_state_stable()
        substrate.final_state_stable()
        assert view.count("final_state.json") == 1
        assert view.count("final_state_stable.json") == 1
    finally:
        substrate.close()


def test_knowledge_search_returns_none_offline(tmp_path: Path) -> None:
    bundle_dir, _ = _write_bundle(tmp_path)
    view = load_grade_bundle(bundle_dir)
    substrate = SnapshotGradingSubstrate(view)
    try:
        assert substrate.knowledge_search() is None
    finally:
        substrate.close()


def test_db_probe_raises_substrate_unreachable_offline(tmp_path: Path) -> None:
    bundle_dir, _ = _write_bundle(tmp_path)
    view = load_grade_bundle(bundle_dir)
    substrate = SnapshotGradingSubstrate(view)
    try:
        with pytest.raises(SubstrateUnreachableError, match="postgresql://x"):
            substrate.db_probe("postgresql://x", "SELECT 1")
    finally:
        substrate.close()


def test_filesystem_root_materialises_bundle_tar_lazily(tmp_path: Path) -> None:
    bundle_dir, inputs = _write_bundle(tmp_path)
    view = load_grade_bundle(bundle_dir)
    substrate = SnapshotGradingSubstrate(view)
    try:
        root_first = substrate.filesystem_root()
        assert root_first is not None
        root_second = substrate.filesystem_root()
        assert root_second == root_first
        extracted = {
            p.relative_to(root_first).as_posix() for p in root_first.rglob("*") if p.is_file()
        }
        assert "src/main.py" in extracted
        assert "README.md" in extracted
        assert not any(name.startswith(".git/") for name in extracted)
        source_root = inputs["filesystem_root"]
        assert isinstance(source_root, Path)
        assert (source_root / ".git" / "HEAD").exists()
    finally:
        substrate.close()


def test_filesystem_root_not_extracted_when_never_read(tmp_path: Path) -> None:
    bundle_dir, _ = _write_bundle(tmp_path)
    view = _CountingView(load_grade_bundle(bundle_dir))
    substrate = SnapshotGradingSubstrate(view)
    try:
        substrate.db_reader().get_state()
        assert view.count("filesystem.tar") == 0
    finally:
        substrate.close()


def test_filesystem_state_walks_materialised_root(tmp_path: Path) -> None:
    bundle_dir, _ = _write_bundle(tmp_path)
    view = load_grade_bundle(bundle_dir)
    substrate = SnapshotGradingSubstrate(view)
    try:
        fs_state = substrate.filesystem_state()
        assert fs_state is not None
        root = substrate.filesystem_root()
        assert root is not None
        assert fs_state == read_agent_visible_filesystem(root)
    finally:
        substrate.close()


def test_filesystem_state_returns_empty_dict_for_empty_tar(tmp_path: Path) -> None:
    inputs = synthetic_inputs(tmp_path)
    empty_root = tmp_path / "empty-workspace"
    empty_root.mkdir()
    inputs["filesystem_root"] = empty_root
    bundle_dir = tmp_path / "bundle"
    serialize_grade_bundle(bundle_dir, **inputs)
    view = load_grade_bundle(bundle_dir)
    substrate = SnapshotGradingSubstrate(view)
    try:
        assert substrate.filesystem_state() == {}
    finally:
        substrate.close()


def test_close_cleans_up_filesystem_tmpdir(tmp_path: Path) -> None:
    bundle_dir, _ = _write_bundle(tmp_path)
    view = load_grade_bundle(bundle_dir)
    substrate = SnapshotGradingSubstrate(view)
    root = substrate.filesystem_root()
    assert root is not None
    assert root.exists()
    substrate.close()
    assert not root.exists()


def test_close_is_idempotent(tmp_path: Path) -> None:
    bundle_dir, _ = _write_bundle(tmp_path)
    view = load_grade_bundle(bundle_dir)
    substrate = SnapshotGradingSubstrate(view)
    substrate.close()
    substrate.close()


def test_db_reader_get_state_all_tables_matches_final_state(tmp_path: Path) -> None:
    bundle_dir, _ = _write_bundle(tmp_path)
    view = load_grade_bundle(bundle_dir)
    substrate = SnapshotGradingSubstrate(view)
    try:
        assert substrate.db_reader().get_state() == substrate.final_state()
    finally:
        substrate.close()


def test_db_reader_get_state_filtered_by_tables(tmp_path: Path) -> None:
    bundle_dir, _ = _write_bundle(tmp_path)
    view = load_grade_bundle(bundle_dir)
    substrate = SnapshotGradingSubstrate(view)
    try:
        filtered = substrate.db_reader().get_state(tables=["users"])
        assert filtered == {"tables": {"users": [{"id": 1, "name": "alice"}]}}
        missing = substrate.db_reader().get_state(tables=["nonexistent"])
        assert missing == {"tables": {}}
    finally:
        substrate.close()


def test_db_reader_query_runs_jsonpath_locally(tmp_path: Path) -> None:
    bundle_dir, _ = _write_bundle(tmp_path)
    view = load_grade_bundle(bundle_dir)
    substrate = SnapshotGradingSubstrate(view)
    try:
        assert substrate.db_reader().query("$.tables.users[0].id") == {"results": [1]}
    finally:
        substrate.close()


def test_open_part_corrupt_bytes_raises_substrate_unreachable(tmp_path: Path) -> None:
    bundle_dir, _ = _write_bundle(tmp_path)
    view = load_grade_bundle(bundle_dir)
    substrate = SnapshotGradingSubstrate(view)
    try:
        target = bundle_dir / "final_state.json"
        data = bytearray(target.read_bytes())
        data[0] = (data[0] + 1) % 256
        target.write_bytes(bytes(data))
        with pytest.raises(SubstrateUnreachableError) as excinfo:
            substrate.final_state()
        assert "final_state.json" in str(excinfo.value)
        assert str(bundle_dir) in str(excinfo.value)
    finally:
        substrate.close()


def test_bundle_dir_deleted_mid_read_raises_substrate_unreachable(tmp_path: Path) -> None:
    bundle_dir, _ = _write_bundle(tmp_path)
    view = load_grade_bundle(bundle_dir)
    substrate = SnapshotGradingSubstrate(view)
    try:
        shutil.rmtree(bundle_dir)
        with pytest.raises(SubstrateUnreachableError, match="final_state.json"):
            substrate.final_state()
    finally:
        substrate.close()


def test_missing_manifest_entry_raises_substrate_unreachable(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    view = GradeBundleView(
        bundle_dir=bundle_dir,
        manifest=GradeBundleManifest(
            schema_version=BUNDLE_SCHEMA_VERSION,
            trial_id="trial-missing",
            parts={},
        ),
    )
    substrate = SnapshotGradingSubstrate(view)
    try:
        with pytest.raises(SubstrateUnreachableError, match="final_state.json"):
            substrate.final_state()
    finally:
        substrate.close()


def test_symlink_escape_in_tar_rejected(tmp_path: Path) -> None:
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        main_info = tarfile.TarInfo(name="src/main.py")
        body = b"print('hi')\n"
        main_info.size = len(body)
        main_info.mtime = 0
        main_info.type = tarfile.REGTYPE
        tar.addfile(main_info, io.BytesIO(body))
        evil = tarfile.TarInfo(name="evil")
        evil.type = tarfile.SYMTYPE
        evil.linkname = "/etc/passwd"
        evil.mtime = 0
        tar.addfile(evil)
    tar_bytes = tar_buf.getvalue()

    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "filesystem.tar").write_bytes(tar_bytes)
    tar_entry = {
        "sha256": hashlib.sha256(tar_bytes).hexdigest(),
        "size": len(tar_bytes),
    }
    view = GradeBundleView(
        bundle_dir=bundle_dir,
        manifest=GradeBundleManifest(
            schema_version=BUNDLE_SCHEMA_VERSION,
            trial_id="trial-hostile",
            parts={"filesystem.tar": tar_entry},
        ),
    )
    substrate = SnapshotGradingSubstrate(view)
    try:
        with pytest.raises(tarfile.FilterError):
            substrate.filesystem_root()
        tmpdir = substrate._filesystem_tmpdir  # noqa: SLF001 — assert nothing escaped
        assert tmpdir is not None
        assert not (Path(tmpdir.name) / "evil").exists()
    finally:
        substrate.close()
