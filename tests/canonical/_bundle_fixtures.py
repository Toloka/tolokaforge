"""Shared synthetic-bundle fixtures for the canonical grading-bundle tests.

The bundle round-trip lock, the content-addressability lock, and any
future bundle-shape test all want the same synthetic trial inputs.
Extracted here so a fixture tweak lands in one place and every consumer
sees the same shape.
"""

from __future__ import annotations

from pathlib import Path

LONG_SEGMENT_NAME = "a_deeply_nested_subdir_with_a_very_long_leading_segment_name_that_exceeds_100_characters_easily"
"""A directory segment > 100 chars — a PAX-default tar producer would
inject extension headers here, tripping the round-trip test's PAX-header
lock."""


def write_synthetic_filesystem(root: Path) -> None:
    """Populate ``root`` with the standard synthetic workspace tree.

    Contains a normal source file, a README, a ``.git`` subtree (which the
    filesystem-view exclude list must strip from the tar), and a long-path
    member exercising the USTAR-format guard.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "src").mkdir()
    (root / "src" / "main.py").write_bytes(b"print('hello')\n")
    (root / "README.md").write_bytes(b"# hello\n")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_bytes(b"ref: refs/heads/main\n")
    long_dir = root / LONG_SEGMENT_NAME
    long_dir.mkdir()
    (long_dir / "leaf.py").write_bytes(b"# long-path member\n")


def synthetic_inputs(tmp_path: Path) -> dict[str, object]:
    """Return the standard synthetic ``serialize_grade_bundle`` kwargs.

    The returned mapping is ready to splat into
    :func:`tolokaforge.core.grading.bundle.serialize_grade_bundle` (except
    for ``out_dir``, which each caller owns).
    """
    fs_root = tmp_path / "workspace"
    write_synthetic_filesystem(fs_root)
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
