"""Worker propagation — ``prepare`` persists the overlay path to the queue
state file; the worker CLI's overlay resolution prefers that value over
``engine.presets_file`` (because ``prepare`` reflects the operator's most
recent intent) while still letting ``--presets-file`` override it.

These are unit tests for the persistence + resolution mechanism. The
end-to-end worker path (subprocess actually consuming the persisted overlay)
is exercised indirectly: we drive ``_activate_presets_overlay`` from the CLI
module with the same inputs the ``worker`` command builds.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tolokaforge.cli.main import _activate_presets_overlay
from tolokaforge.core.engine_run_state import (
    read_engine_run_state,
    read_persisted_presets_file,
    write_engine_run_state,
)
from tolokaforge.core.llm.presets import get_overlay_path
from tolokaforge.core.models import EngineConfig, RunConfig

pytestmark = pytest.mark.unit


def _minimal_run_config(presets_file: str | None = None) -> RunConfig:
    """Build a syntactically valid ``RunConfig`` for the resolver tests."""
    return RunConfig(
        models={},
        orchestrator={"workers": 1},
        evaluation={"output_dir": "/tmp"},
        engine=EngineConfig(presets_file=presets_file) if presets_file else None,
    )


class TestEngineRunStatePersistence:
    def test_round_trip(self, tmp_path: Path) -> None:
        write_engine_run_state(tmp_path, presets_file="/some/overlay.yaml")
        assert read_persisted_presets_file(tmp_path) == "/some/overlay.yaml"
        assert read_engine_run_state(tmp_path) == {"presets_file": "/some/overlay.yaml"}

    def test_writing_none_persists_as_none(self, tmp_path: Path) -> None:
        write_engine_run_state(tmp_path, presets_file=None)
        assert read_persisted_presets_file(tmp_path) is None
        # File exists but the value is null — distinct from "no file at all".
        assert read_engine_run_state(tmp_path) == {"presets_file": None}

    def test_absent_file_returns_empty_state(self, tmp_path: Path) -> None:
        assert read_engine_run_state(tmp_path) == {}
        assert read_persisted_presets_file(tmp_path) is None

    def test_overwrite_replaces_previous_value(self, tmp_path: Path) -> None:
        write_engine_run_state(tmp_path, presets_file="/first.yaml")
        write_engine_run_state(tmp_path, presets_file="/second.yaml")
        assert read_persisted_presets_file(tmp_path) == "/second.yaml"

    def test_malformed_state_file_raises_loudly(self, tmp_path: Path) -> None:
        # Loud-fail discipline: silently ignoring malformed engine state
        # would let workers run with the wrong overlay.
        (tmp_path / "engine_run_state.json").write_text("{not json")
        with pytest.raises(json.JSONDecodeError):
            read_engine_run_state(tmp_path)


class TestWorkerOverlayResolution:
    """The worker CLI calls ``_activate_presets_overlay(cli, run_config, run_dir)``.
    The resolution precedence is CLI > queue-state > engine.presets_file.

    Tests use real (minimal but valid) overlay YAMLs because
    ``_activate_presets_overlay`` now eagerly validates the resolved overlay
    — a CLI-boundary improvement that catches typo'd paths before any
    orchestrator / Docker work runs.
    """

    def test_queue_state_beats_engine_config(self, tmp_path: Path, write_overlay) -> None:
        queue_overlay = write_overlay({}, name="queue.yaml")
        config_overlay = write_overlay({}, name="config.yaml")
        write_engine_run_state(tmp_path, presets_file=queue_overlay)
        run_config = _minimal_run_config(presets_file=config_overlay)
        resolved = _activate_presets_overlay(
            cli_presets_file=None, run_config=run_config, run_dir=tmp_path
        )
        assert resolved == queue_overlay
        assert get_overlay_path() == queue_overlay

    def test_cli_beats_queue_state(self, tmp_path: Path, write_overlay) -> None:
        queue_overlay = write_overlay({}, name="queue.yaml")
        cli_overlay = write_overlay({}, name="cli.yaml")
        write_engine_run_state(tmp_path, presets_file=queue_overlay)
        resolved = _activate_presets_overlay(
            cli_presets_file=cli_overlay,
            run_config=_minimal_run_config(),
            run_dir=tmp_path,
        )
        assert resolved == cli_overlay

    def test_no_queue_state_falls_through_to_engine_config(
        self, tmp_path: Path, write_overlay
    ) -> None:
        # No engine_run_state.json in tmp_path.
        config_overlay = write_overlay({}, name="config.yaml")
        run_config = _minimal_run_config(presets_file=config_overlay)
        resolved = _activate_presets_overlay(
            cli_presets_file=None, run_config=run_config, run_dir=tmp_path
        )
        assert resolved == config_overlay

    def test_persisted_none_falls_through_to_engine_config(
        self, tmp_path: Path, write_overlay
    ) -> None:
        # ``prepare`` ran without an overlay → persisted value is None →
        # fall through to engine.presets_file rather than treating the file's
        # presence as "no overlay".
        config_overlay = write_overlay({}, name="config.yaml")
        write_engine_run_state(tmp_path, presets_file=None)
        run_config = _minimal_run_config(presets_file=config_overlay)
        resolved = _activate_presets_overlay(
            cli_presets_file=None, run_config=run_config, run_dir=tmp_path
        )
        assert resolved == config_overlay


class TestEagerValidation:
    """Pin the High-2 CLI-boundary behaviour: ``_activate_presets_overlay``
    eagerly validates the resolved overlay so a typo'd path / malformed
    overlay fails immediately — before the orchestrator is constructed,
    ``load_tasks()`` runs, or the Docker stack auto-starts.

    The lazy-read contract on ``set_overlay_path`` itself is unchanged (the
    fixture-based unit tests still exercise it), but the CLI helper layer
    triggers validation at the host boundary.
    """

    def test_cli_helper_raises_on_missing_overlay_immediately(self, tmp_path: Path) -> None:
        run_config = _minimal_run_config(presets_file=str(tmp_path / "missing.yaml"))
        with pytest.raises(FileNotFoundError, match=r"missing\.yaml"):
            _activate_presets_overlay(cli_presets_file=None, run_config=run_config, run_dir=None)

    def test_cli_helper_raises_on_malformed_overlay_immediately(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("presets:\n  bad:\n    match: ['x/*']\n    response_policy: not_real\n")
        run_config = _minimal_run_config(presets_file=str(bad))
        with pytest.raises(ValueError, match=r"unknown response_policy 'not_real'"):
            _activate_presets_overlay(cli_presets_file=None, run_config=run_config, run_dir=None)

    def test_cli_helper_no_validation_when_no_overlay_resolved(self, tmp_path: Path) -> None:
        # No overlay anywhere → no validation runs, no error.
        run_config = _minimal_run_config()
        resolved = _activate_presets_overlay(
            cli_presets_file=None, run_config=run_config, run_dir=None
        )
        assert resolved is None
