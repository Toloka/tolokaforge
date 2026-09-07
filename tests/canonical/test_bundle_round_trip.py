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

from tests.canonical._bundle_fixtures import LONG_SEGMENT_NAME, synthetic_inputs
from tolokaforge.core.grading.bundle import (
    BUNDLE_SCHEMA_VERSION,
    GradeBundleManifest,
    manifest_digest,
    normalise_floats,
    serialize_grade_bundle,
)

pytestmark = pytest.mark.canonical


def test_two_serialisations_produce_byte_identical_bytes(tmp_path: Path) -> None:
    inputs = synthetic_inputs(tmp_path)
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
    inputs = synthetic_inputs(tmp_path)
    out_dir = tmp_path / "bundle"
    out_dir.mkdir()
    (out_dir / "stray.txt").write_bytes(b"pre-existing")

    with pytest.raises(FileExistsError):
        serialize_grade_bundle(out_dir, **inputs)


def test_tar_carries_no_pax_headers(tmp_path: Path) -> None:
    inputs = synthetic_inputs(tmp_path)
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
    inputs = synthetic_inputs(tmp_path)
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


def test_v1_1_optional_parts_absent_when_kwargs_omitted(tmp_path: Path) -> None:
    """A v1.0-shape caller (neither ``task_description`` nor
    ``judge_model_config`` passed) writes no such parts. The manifest
    inventory matches what v1.0 emitted; the bundle bytes are what a
    downstream v1.0 consumer would see."""
    inputs = synthetic_inputs(tmp_path)
    out_dir = tmp_path / "bundle"
    manifest = serialize_grade_bundle(out_dir, **inputs)

    assert "task_description.json" not in manifest.parts
    assert "judge_model_config.json" not in manifest.parts
    assert not (out_dir / "task_description.json").exists()
    assert not (out_dir / "judge_model_config.json").exists()


def test_v1_1_optional_parts_written_when_provided(tmp_path: Path) -> None:
    inputs = synthetic_inputs(tmp_path)
    task_description = {"task_id": "t", "grading": {"combine": {"method": "weighted"}}}
    judge_model_config = {"provider": "openrouter", "name": "anthropic/claude-sonnet-4-6"}
    out_dir = tmp_path / "bundle"
    manifest = serialize_grade_bundle(
        out_dir,
        task_description=task_description,
        judge_model_config=judge_model_config,
        **inputs,
    )

    assert "task_description.json" in manifest.parts
    assert "judge_model_config.json" in manifest.parts
    assert (out_dir / "task_description.json").exists()
    assert (out_dir / "judge_model_config.json").exists()

    # Round-trip determinism holds for v1.1 too — two same-process
    # serialisations of the same v1.1-shape inputs bit-identical.
    out_b = tmp_path / "bundle_b"
    serialize_grade_bundle(
        out_b,
        task_description=task_description,
        judge_model_config=judge_model_config,
        **inputs,
    )
    for rel in sorted(manifest.parts):
        assert (out_dir / rel).read_bytes() == (out_b / rel).read_bytes()
