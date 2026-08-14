"""Unit tests for ``resolve_resume_run_directory``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tolokaforge.core.engine_run_state import write_engine_run_state
from tolokaforge.core.model_data_fingerprint import compute_models_fingerprint
from tolokaforge.core.resume import resolve_resume_run_directory

pytestmark = pytest.mark.unit


def _write_run_state(run_dir: Path, run_id: str) -> None:
    (run_dir / "run_state.json").write_text(json.dumps({"run_id": run_id}))


class TestResolveResumeRunDirectory:
    def test_reads_run_id_from_engine_run_state(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "custom_grading_20260714_120000"
        run_dir.mkdir()
        write_engine_run_state(
            run_dir,
            run_id="canonical_run_id",
            presets_file=None,
            models_fingerprint=compute_models_fingerprint(),
            adapter_fingerprints={},
        )
        _write_run_state(run_dir, run_id="stale_id_in_run_state")

        run_id, resolved = resolve_resume_run_directory(run_dir)

        assert run_id == "canonical_run_id"
        assert resolved == run_dir

    def test_falls_back_to_run_state_json_when_engine_state_missing(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "legacy_run"
        run_dir.mkdir()
        _write_run_state(run_dir, run_id="run_state_run_id")

        run_id, resolved = resolve_resume_run_directory(run_dir)

        assert run_id == "run_state_run_id"
        assert resolved == run_dir

    def test_falls_back_to_directory_name_when_run_id_absent(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "dirname_fallback"
        run_dir.mkdir()
        (run_dir / "run_state.json").write_text(json.dumps({}))

        run_id, resolved = resolve_resume_run_directory(run_dir)

        assert run_id == "dirname_fallback"
        assert resolved == run_dir

    def test_raises_when_neither_metadata_file_present(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "empty_dir"
        run_dir.mkdir()

        with pytest.raises(RuntimeError, match="not a resumable run directory"):
            resolve_resume_run_directory(run_dir)

    def test_raises_when_directory_missing_entirely(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist"

        with pytest.raises(RuntimeError, match="not a resumable run directory"):
            resolve_resume_run_directory(missing)

    def test_run_dir_is_not_resolved(self, tmp_path: Path) -> None:
        """Parity with ``resolve_run_directory``: pass-through, no ``.resolve()``."""
        run_dir = tmp_path / "relative_style"
        run_dir.mkdir()
        write_engine_run_state(
            run_dir,
            run_id="rid",
            presets_file=None,
            models_fingerprint=compute_models_fingerprint(),
            adapter_fingerprints={},
        )

        _, resolved = resolve_resume_run_directory(run_dir)

        # Same object shape as input — plain Path, unresolved.
        assert resolved == run_dir
        assert str(resolved) == str(run_dir)
