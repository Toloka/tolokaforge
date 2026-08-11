"""Integration-esque unit test — subprocess bridge for ``--paths-from-cached``.

Creates a throwaway git repo under ``tmp_path``, stages a synthetic
touched-file set, shells out to ``uv run automation classify-paths
--paths-from-cached``, and asserts the JSON matches what the classifier
would return on the same paths in-process. This locks the ``git diff
--cached --name-only`` bridge the ``integrate-model.yml`` finalize step
depends on — the shape most likely to regress silently under a git
version change or a subprocess-flag typo.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    (repo / "seed.txt").write_text("seed\n")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-q", "-m", "seed")


def _stage_file(repo: Path, relpath: str, content: str = "x\n") -> None:
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _git(repo, "add", relpath)


def _run_classify(repo: Path) -> dict:
    completed = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(_REPO_ROOT),
            "automation",
            "classify-paths",
            "--paths-from-cached",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_paths_from_cached_bucket_a_only(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _stage_file(
        tmp_path, "tolokaforge_models/src/tolokaforge_models/data/model_presets.yaml", "presets:\n"
    )
    _stage_file(
        tmp_path, "tolokaforge_models/src/tolokaforge_models/certificates/registry.py", "R = {}\n"
    )
    payload = _run_classify(tmp_path)
    assert payload["bucket"] == "A"
    assert payload["engine_paths"] == []
    assert payload["reason"] == "all touched paths in Bucket-A allow-list"


def test_paths_from_cached_bucket_b_engine_path(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _stage_file(
        tmp_path, "tolokaforge_models/src/tolokaforge_models/data/model_presets.yaml", "presets:\n"
    )
    _stage_file(tmp_path, "tolokaforge/core/llm/schema_sanitizer.py", "def f(): pass\n")
    payload = _run_classify(tmp_path)
    assert payload["bucket"] == "B"
    assert payload["engine_paths"] == ["tolokaforge/core/llm/schema_sanitizer.py"]
    assert payload["reason"] == "1 engine-side path(s) outside Bucket-A allow-list"


def test_paths_from_cached_empty_stage_is_bucket_a_empty_diff(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    payload = _run_classify(tmp_path)
    assert payload == {"bucket": "A", "reason": "empty diff", "engine_paths": []}
