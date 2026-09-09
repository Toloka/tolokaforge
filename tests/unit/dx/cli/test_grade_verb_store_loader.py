"""``_load_store`` — ``--store-config`` YAML parsing + URI store-name mismatch.

Locks the discriminated-union parse path (``BundleStoreBackend``), the
lazy-import S3 path (``boto3`` optional), the ``local_disk`` default,
and the CLI-level refusal on URI/store-name mismatch. Every case runs
against the shipped :func:`_load_store` helper without a CliRunner
where possible — the store loader is unit-testable directly.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from tests.unit.dx.cli.conftest import _FixedScoreKind
from tolokaforge.core.grading.bundle_store import (
    LocalDiskBundleStore,
    S3BundleStore,
    build_bundle_uri,
)
from tolokaforge.dx.cli.grade import _load_store
from tolokaforge.dx.cli.main import cli

pytestmark = pytest.mark.unit


def _write_yaml(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_load_store_local_disk_explicit(tmp_path: Path) -> None:
    root = tmp_path / "explicit-root"
    config = _write_yaml(
        tmp_path / "store.yaml",
        f"type: local_disk\nroot_dir: {root}\n",
    )

    store = _load_store(config)

    assert isinstance(store, LocalDiskBundleStore)
    assert store.root_dir == root.resolve()


def test_load_store_default_returns_local_disk_under_cwd(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    store = _load_store(None)

    assert isinstance(store, LocalDiskBundleStore)
    assert store.root_dir == tmp_path.resolve()


def test_load_store_s3_constructs_without_boto3(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "boto3", None)
    config = _write_yaml(
        tmp_path / "s3.yaml",
        "type: s3\nbucket: my-bucket\nprefix: run-2026\n",
    )

    store = _load_store(config)

    assert isinstance(store, S3BundleStore)
    assert store.bucket == "my-bucket"
    assert store.prefix == "run-2026"


def test_load_store_rejects_unknown_type(tmp_path: Path) -> None:
    import click

    config = _write_yaml(tmp_path / "bad.yaml", "type: unknown\nroot_dir: /nope\n")

    with pytest.raises(click.BadParameter):
        _load_store(config)


def test_uri_store_name_mismatch_fails_at_arg_time(
    tmp_path: Path,
    stored_bundle: tuple[str, Path],
    register_grader_kind: Callable[[str, type], None],
) -> None:
    _, store_root = stored_bundle
    register_grader_kind(_FixedScoreKind.NAME, _FixedScoreKind)
    config = _write_yaml(
        tmp_path / "store.yaml",
        f"type: local_disk\nroot_dir: {store_root}\n",
    )
    mismatched_uri = build_bundle_uri("s3", "0" * 64)
    out_dir = tmp_path / "out"

    runner = CliRunner(mix_stderr=False)
    result = runner.invoke(
        cli,
        [
            "grade",
            mismatched_uri,
            "--grader-kind",
            _FixedScoreKind.NAME,
            "--store-config",
            str(config),
            "--out",
            str(out_dir),
        ],
    )

    assert result.exit_code == 2, result.stderr
    assert "URI names store" in result.stderr
    assert "'s3'" in result.stderr
    assert "'local_disk'" in result.stderr
