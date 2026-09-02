"""Canonical acceptance for the grade bundle producer.

Two same-process serialisations of the same synthetic trial land on
byte-identical bytes across every part and on a matching
``sha256(manifest.json)``. The lock: the manifest IS the bundle's name;
identity across storage moves depends on the producer being deterministic
inside one interpreter.

Also covers the tar-format lock: the emitted tar carries no PAX headers
at the archive level or on any member. Python's ``tarfile.DEFAULT_FORMAT``
is PAX and auto-injects extension headers for long paths or non-ASCII
names — a producer that fell back to the default would silently diverge
bytes on the first long-path task pack, and the same-process round-trip
alone would not catch it.

Cross-Python-version / cross-OS / cross-language drift is out of scope
for this lane.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from tolokaforge.core.grading.bundle import (
    BUNDLE_SCHEMA_VERSION,
    GradeBundleManifest,
    manifest_digest,
    normalise_floats,
    serialize_grade_bundle,
)

pytestmark = pytest.mark.canonical


LONG_SEGMENT_NAME = "a_deeply_nested_subdir_with_a_very_long_leading_segment_name_that_exceeds_100_characters_easily"
"""A directory segment > 100 chars — a PAX-default producer would inject
extension headers here, tripping :func:`test_tar_carries_no_pax_headers`."""


def _write_synthetic_filesystem(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "src").mkdir()
    (root / "src" / "main.py").write_bytes(b"print('hello')\n")
    (root / "README.md").write_bytes(b"# hello\n")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_bytes(b"ref: refs/heads/main\n")
    long_dir = root / LONG_SEGMENT_NAME
    long_dir.mkdir()
    (long_dir / "leaf.py").write_bytes(b"# long-path member\n")


def _synthetic_inputs(tmp_path: Path) -> dict[str, object]:
    fs_root = tmp_path / "workspace"
    _write_synthetic_filesystem(fs_root)
    return {
        "trial_id": "trial-round-trip-1",
        "initial_state": {"tables": {"users": []}, "score": 0.123456789},
        "final_state": {"tables": {"users": [{"id": 1, "name": "alice"}]}},
        "final_state_stable": {"tables": {"users": [{"id": 1, "name": "alice"}]}},
        "filesystem_root": fs_root,
        "checks": {"greet_ok.py": b"def check(): return True\n"},
        "kb": {"policy.md": b"# policy\n"},
        "trajectory": {"llm_messages": [{"role": "user", "content": "hi"}]},
        "grading_config": {"combine_method": "weighted", "weights": {"custom": 1.0}},
    }


def test_two_serialisations_produce_byte_identical_bytes(tmp_path: Path) -> None:
    inputs = _synthetic_inputs(tmp_path)
    out_a = tmp_path / "bundle_a"
    out_b = tmp_path / "bundle_b"

    manifest_a = serialize_grade_bundle(out_a, **inputs)
    manifest_b = serialize_grade_bundle(out_b, **inputs)

    assert isinstance(manifest_a, GradeBundleManifest)
    assert isinstance(manifest_b, GradeBundleManifest)
    assert manifest_a.schema_version == BUNDLE_SCHEMA_VERSION
    assert set(manifest_a.parts.keys()) == set(manifest_b.parts.keys())

    for rel in sorted(manifest_a.parts):
        bytes_a = (out_a / rel).read_bytes()
        bytes_b = (out_b / rel).read_bytes()
        assert bytes_a == bytes_b, f"part {rel!r} diverged between runs"

    digest_a = manifest_digest((out_a / "manifest.json").read_bytes())
    digest_b = manifest_digest((out_b / "manifest.json").read_bytes())
    assert digest_a == digest_b


def test_producer_refuses_non_empty_out_dir(tmp_path: Path) -> None:
    inputs = _synthetic_inputs(tmp_path)
    out_dir = tmp_path / "bundle"
    out_dir.mkdir()
    (out_dir / "stray.txt").write_bytes(b"pre-existing")

    with pytest.raises(FileExistsError):
        serialize_grade_bundle(out_dir, **inputs)


def test_tar_carries_no_pax_headers(tmp_path: Path) -> None:
    inputs = _synthetic_inputs(tmp_path)
    out_dir = tmp_path / "bundle"
    serialize_grade_bundle(out_dir, **inputs)

    tar_path = out_dir / "filesystem.tar"
    with tarfile.open(tar_path, mode="r") as tar:
        assert tar.pax_headers == {}
        members = tar.getmembers()
        long_path_present = any(LONG_SEGMENT_NAME in member.name for member in members)
        assert long_path_present, "fixture must exercise the long-path member"
        for member in members:
            pax_pseudo = member.type in (tarfile.XHDTYPE, tarfile.XGLTYPE)
            assert not pax_pseudo, f"PAX extension pseudo-member: {member.name!r}"
            assert member.pax_headers == {}, f"PAX headers on {member.name!r}"
            assert member.mtime == 0
            assert member.uid == 0
            assert member.gid == 0
            assert member.uname == ""
            assert member.gname == ""
            assert member.mode == 0o644
            assert member.type == tarfile.REGTYPE


def test_filesystem_tar_respects_exclude_dirs(tmp_path: Path) -> None:
    inputs = _synthetic_inputs(tmp_path)
    out_dir = tmp_path / "bundle"
    serialize_grade_bundle(out_dir, **inputs)

    with tarfile.open(out_dir / "filesystem.tar", mode="r") as tar:
        names = {member.name for member in tar.getmembers()}
    assert "src/main.py" in names
    assert "README.md" in names
    dotgit_leaked = any(name.startswith(".git/") for name in names)
    assert not dotgit_leaked, f".git/ subtree leaked into filesystem.tar: {names}"


def test_float_normaliser_matches_parity_harness_import() -> None:
    from tests.utils.grader_parity_harness import normalise_floats as harness_ref

    assert harness_ref is normalise_floats
    assert normalise_floats({"score": 0.123456789}) == {"score": 0.123457}
