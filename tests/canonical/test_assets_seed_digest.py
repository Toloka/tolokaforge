"""Canonical coverage for :func:`tolokaforge.core.assets.compute_seed_digest`
and its use in the project loader's seed-digest verifier.

Three properties are locked:

1. **Directory seeds** — the tree digest is stable across calls,
   independent of filesystem iteration order, and flips on any content
   edit or file rename.
2. **File seeds** — a real ``.sql`` fixture hashes to a hard-coded
   sha256 (regression guard: if this fails, either the fixture bytes
   changed or the file-hash path silently regressed).
3. **Load-verify** — ``load_project_config`` accepts a ``filesystem_dir``
   seed whose stamped digest matches, and raises the digest-mismatch
   ``RuntimeError`` when the tree is mutated without re-stamping.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tolokaforge.core.assets import compute_seed_digest
from tolokaforge.core.project_loader import load_project_config

pytestmark = pytest.mark.canonical

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Byte-stream sha256 of the postgres_reset pack's baseline dump, pinned
# so an accidental change to the file — or a regression in the file-hash
# path — is caught. Re-derive with:
#   shasum -a 256 examples/native/multi_service_postgres_reset/assets/postgres_baseline.sql
_POSTGRES_BASELINE_SQL = (
    _REPO_ROOT
    / "examples"
    / "native"
    / "multi_service_postgres_reset"
    / "assets"
    / "postgres_baseline.sql"
)
_POSTGRES_BASELINE_DIGEST = (
    "sha256:6ef10f37326ab35c4fa4c20efbe902a7ce961be7c8916db2bef89dd5fb655580"
)


def _build_tree(root: Path, files: dict[str, bytes]) -> None:
    """Materialise *files* (posix-relative path → bytes) under *root*."""
    for relative_path, content in files.items():
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


_SAMPLE_TREE = {
    "app.py": b"print('hi')\n",
    "pkg/__init__.py": b"",
    "pkg/util.py": b"X = 1\n",
    "data/seed.json": b'{"k": 1}\n',
}


class TestDirectorySeedDigest:
    def test_stable_across_calls(self, tmp_path: Path) -> None:
        root = tmp_path / "tree"
        _build_tree(root, _SAMPLE_TREE)
        assert compute_seed_digest(root) == compute_seed_digest(root)

    def test_independent_of_filesystem_iteration_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The sort by relative path must neutralise the OS's directory
        # walk order. Force ``rglob`` to yield in reverse and assert the
        # digest is unchanged — this directly locks the sort.
        root = tmp_path / "tree"
        _build_tree(root, _SAMPLE_TREE)
        baseline = compute_seed_digest(root)

        real_rglob = Path.rglob

        def reversed_rglob(self: Path, pattern: str):  # type: ignore[no-untyped-def]
            return reversed(list(real_rglob(self, pattern)))

        monkeypatch.setattr(Path, "rglob", reversed_rglob)
        assert compute_seed_digest(root) == baseline

    def test_flips_on_content_edit(self, tmp_path: Path) -> None:
        root = tmp_path / "tree"
        _build_tree(root, _SAMPLE_TREE)
        before = compute_seed_digest(root)
        (root / "pkg" / "util.py").write_bytes(b"X = 2\n")
        assert compute_seed_digest(root) != before

    def test_flips_on_rename(self, tmp_path: Path) -> None:
        root = tmp_path / "tree"
        _build_tree(root, _SAMPLE_TREE)
        before = compute_seed_digest(root)
        (root / "pkg" / "util.py").rename(root / "pkg" / "helpers.py")
        assert compute_seed_digest(root) != before

    def test_same_layout_same_content_matches_across_roots(self, tmp_path: Path) -> None:
        # Two independent roots with identical logical content produce
        # identical digests — the digest is a function of the tree, not
        # of the absolute location or inode identity.
        root_a = tmp_path / "a"
        root_b = tmp_path / "b"
        _build_tree(root_a, _SAMPLE_TREE)
        _build_tree(root_b, dict(reversed(list(_SAMPLE_TREE.items()))))
        assert compute_seed_digest(root_a) == compute_seed_digest(root_b)


class TestFileSeedDigestRegressionGuard:
    def test_postgres_baseline_sql_matches_pinned_digest(self) -> None:
        assert _POSTGRES_BASELINE_SQL.is_file()
        assert compute_seed_digest(_POSTGRES_BASELINE_SQL) == _POSTGRES_BASELINE_DIGEST


class TestLoadProjectConfigDirectorySeed:
    def _write_project(self, tmp_path: Path, digest: str) -> Path:
        project_yaml = tmp_path / "project.yaml"
        project_yaml.write_text(
            yaml.safe_dump(
                {
                    "name": "dir-seed-fixture",
                    "assets": {
                        "seeds": {
                            "pristine_source": {
                                "path": "assets/source",
                                "kind": "filesystem_dir",
                                "digest": digest,
                            },
                        },
                    },
                },
                sort_keys=False,
            ),
        )
        return project_yaml

    def test_correct_digest_loads_clean(self, tmp_path: Path) -> None:
        source = tmp_path / "assets" / "source"
        _build_tree(source, _SAMPLE_TREE)
        project_yaml = self._write_project(tmp_path, compute_seed_digest(source))

        project = load_project_config(project_yaml)
        assert project.assets is not None
        assert project.assets.seeds["pristine_source"].kind == "filesystem_dir"

    def test_mutated_tree_without_restamp_raises(self, tmp_path: Path) -> None:
        source = tmp_path / "assets" / "source"
        _build_tree(source, _SAMPLE_TREE)
        project_yaml = self._write_project(tmp_path, compute_seed_digest(source))
        # Mutate a file so the on-disk tree no longer matches the
        # stamped digest — load-verify must fail loud.
        (source / "app.py").write_bytes(b"print('tampered')\n")

        with pytest.raises(RuntimeError, match="digest mismatch"):
            load_project_config(project_yaml)
