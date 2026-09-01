"""Behaviour locks for ``tolokaforge.core.grading.filesystem_view``.

Three groups:

* Walker semantics on real ``tmp_path`` trees — logical relabelling,
  binary + symlink skip, empty-workspace handling.
* Exclusion policy — five directory basenames prune subtrees at any
  depth; a leaf file whose name matches an excluded directory is kept.
* :func:`is_excluded_rel_path` unit locks — the predicate the substrate
  service's per-path read uses. Both callers agree at every emitted
  path (the paired-invariant test).

The unit-conftest autouses a fake ``tolokaforge-*.whl`` at the ``tmp_path``
root; every walker case here runs against a dedicated ``work``
subdirectory to keep that artefact out of the walk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.core.grading.filesystem_view import (
    AGENT_VISIBLE_EXCLUDES,
    is_excluded_rel_path,
    read_agent_visible_filesystem,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def work(tmp_path: Path) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    return root


# ---------------------------------------------------------------------------
# Walker semantics
# ---------------------------------------------------------------------------


def test_read_agent_visible_filesystem_relabels_files_under_logical_root(
    work: Path,
) -> None:
    (work / "buggy_math.py").write_text("amount * (1 + tax_rate)\n")
    (work / "sub").mkdir()
    (work / "sub" / "helper.py").write_text("def x(): return 1\n")

    assert read_agent_visible_filesystem(work) == {
        "/env/fs/agent-visible/buggy_math.py": "amount * (1 + tax_rate)\n",
        "/env/fs/agent-visible/sub/helper.py": "def x(): return 1\n",
    }


def test_read_agent_visible_filesystem_skips_binary_and_returns_empty_when_absent(
    tmp_path: Path,
) -> None:
    assert read_agent_visible_filesystem(tmp_path / "nope") == {}

    work = tmp_path / "work"
    work.mkdir()
    (work / "image.bin").write_bytes(b"\x89PNG\x00\x01\x02\x03\xff\xfe")
    (work / "readme.txt").write_text("hello")
    assert read_agent_visible_filesystem(work) == {
        "/env/fs/agent-visible/readme.txt": "hello",
    }


def test_read_agent_visible_filesystem_skips_symlinks(work: Path) -> None:
    # A symlink under the workspace could point at any container-readable
    # path; the assertion vocabulary is not a general-purpose container
    # filesystem probe, so the walk must ignore the link.
    outside = work.parent / "outside.txt"
    outside.write_text("must not leak")
    (work / "readme.txt").write_text("ok")
    (work / "link").symlink_to(outside)

    assert read_agent_visible_filesystem(work) == {
        "/env/fs/agent-visible/readme.txt": "ok",
    }


# ---------------------------------------------------------------------------
# Exclusion policy — walker
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("excluded", sorted(AGENT_VISIBLE_EXCLUDES))
def test_walker_prunes_each_excluded_dir_at_root(work: Path, excluded: str) -> None:
    (work / excluded).mkdir()
    (work / excluded / "inner.txt").write_text("hidden")

    assert read_agent_visible_filesystem(work) == {}


@pytest.mark.parametrize("excluded", sorted(AGENT_VISIBLE_EXCLUDES))
def test_walker_prunes_each_excluded_dir_at_nested_depth(work: Path, excluded: str) -> None:
    nested = work / "packages" / "foo" / excluded
    nested.mkdir(parents=True)
    (nested / "inner.js").write_text("hidden")
    (work / "src").mkdir()
    (work / "src" / "main.py").write_text("kept\n")

    assert read_agent_visible_filesystem(work) == {
        "/env/fs/agent-visible/src/main.py": "kept\n",
    }


def test_walker_keeps_a_leaf_file_whose_name_matches_an_excluded_dir(
    work: Path,
) -> None:
    # A workspace-root file literally named "dist" (or ".git") is a leaf,
    # not a subtree; the exclusion policy prunes subtrees rooted at excluded
    # dirs, not leaf files with matching names.
    (work / "dist").write_text("i am a file\n")
    (work / ".git").write_text("also a file\n")

    assert read_agent_visible_filesystem(work) == {
        "/env/fs/agent-visible/dist": "i am a file\n",
        "/env/fs/agent-visible/.git": "also a file\n",
    }


def test_walker_keeps_a_substring_match_directory(work: Path) -> None:
    # Component equality, not substring: "my_dist_folder" is not "dist".
    folder = work / "my_dist_folder"
    folder.mkdir()
    (folder / "x.py").write_text("kept\n")

    assert read_agent_visible_filesystem(work) == {
        "/env/fs/agent-visible/my_dist_folder/x.py": "kept\n",
    }


def test_walker_ignores_symlink_pointing_at_an_excluded_directory(
    work: Path,
) -> None:
    # Symlinks are skipped independent of exclusion; the composite check
    # locks that behaviour on an excluded target too.
    outside = work.parent / "other_node_modules"
    outside.mkdir()
    (outside / "huge.js").write_text("hidden")
    (work / "src.py").write_text("kept\n")
    (work / "node_modules").symlink_to(outside)

    assert read_agent_visible_filesystem(work) == {
        "/env/fs/agent-visible/src.py": "kept\n",
    }


def test_walker_returns_empty_for_an_empty_workspace(work: Path) -> None:
    assert read_agent_visible_filesystem(work) == {}


# ---------------------------------------------------------------------------
# Exclusion policy — is_excluded_rel_path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("packages/foo/node_modules/x.js", True),
        ("packages/foo/src/x.js", False),
        (".git/HEAD", True),
        (".git", False),
        ("dist", False),
        ("dist/bundle.js", True),
        (".venv/lib/py.py", True),
        (".next/build/x.js", True),
        ("my_dist/x", False),
        ("/.git/HEAD", True),
    ],
)
def test_is_excluded_rel_path_cases(rel: str, expected: bool) -> None:
    assert is_excluded_rel_path(rel) is expected


# ---------------------------------------------------------------------------
# Paired invariant — walker and predicate agree at every emitted path
# ---------------------------------------------------------------------------


def test_walker_output_and_read_predicate_agree_on_every_emitted_path(
    work: Path,
) -> None:
    (work / "dist").write_text("root file named dist\n")
    (work / ".git-as-file").write_text("similar-looking file\n")
    git_dir = work / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    node_dir = work / "packages" / "foo" / "node_modules"
    node_dir.mkdir(parents=True)
    (node_dir / "bar.js").write_text("hidden\n")
    (work / "src").mkdir()
    (work / "src" / "main.py").write_text("kept\n")

    emitted = read_agent_visible_filesystem(work)

    for logical_key in emitted:
        rel = logical_key.removeprefix("/env/fs/agent-visible/")
        assert not is_excluded_rel_path(rel), (
            f"walker emitted {logical_key!r} but is_excluded_rel_path refuses it "
            f"— the two callers disagree at rel_path {rel!r}"
        )
    assert set(emitted) == {
        "/env/fs/agent-visible/dist",
        "/env/fs/agent-visible/.git-as-file",
        "/env/fs/agent-visible/src/main.py",
    }
