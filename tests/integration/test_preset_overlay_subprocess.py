"""Cross-process overlay handoff — proves engine_run_state.json bridges
``tolokaforge prepare`` and ``tolokaforge worker`` across actual Python
interpreter boundaries.

The unit tests in ``tests/unit/llm/test_preset_overlay_worker_propagation.py``
cover the resolution layer (``_activate_presets_overlay``) inside one
process. These tests run a **fresh subprocess** so module-level overlay
state cannot have leaked from the parent — the only carrier across the
boundary is the JSON file written by ``prepare``. If that bridge is
broken, the subprocess overlay path comes back as ``None``.

No Docker / no LLM credentials needed: the subprocess just resolves the
overlay path and prints it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tolokaforge.core.engine_run_state import (
    write_engine_run_state,
)

pytestmark = pytest.mark.integration


_RESOLVER_SCRIPT = textwrap.dedent(
    """
    import json, sys
    from pathlib import Path
    from tolokaforge.dx.cli.main import _activate_presets_overlay
    from tolokaforge.core.llm.presets import get_overlay_path
    from tolokaforge.core.models import EngineConfig, RunConfig

    run_dir = Path(sys.argv[1]) if sys.argv[1] != "NONE" else None
    cli_value = sys.argv[2] if sys.argv[2] != "NONE" else None
    config_value = sys.argv[3] if sys.argv[3] != "NONE" else None

    run_config = RunConfig(
        models={},
        orchestrator={"workers": 1},
        evaluation={"output_dir": "/tmp"},
        engine=EngineConfig(presets_file=config_value) if config_value else None,
    )
    resolved = _activate_presets_overlay(
        cli_presets_file=cli_value, run_config=run_config, run_dir=run_dir
    )
    print(json.dumps({"resolved": resolved, "installed": get_overlay_path()}))
    """
).strip()


def _run_resolver(
    *,
    run_dir: Path | None,
    cli_value: str | None,
    config_value: str | None,
) -> dict:
    """Spawn a clean subprocess that drives the worker's overlay resolution.

    Returns the resolver's JSON output as a dict, so tests can assert both
    the resolved path and the actually-installed path inside the subprocess.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            _RESOLVER_SCRIPT,
            str(run_dir) if run_dir is not None else "NONE",
            cli_value if cli_value is not None else "NONE",
            config_value if config_value is not None else "NONE",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"resolver subprocess failed: rc={proc.returncode}\n"
        f"stderr={proc.stderr}\nstdout={proc.stdout}"
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _write_empty_overlay(tmp_path: Path, name: str) -> str:
    """Write a minimal valid overlay so the CLI's eager validation passes.

    Resolution-precedence tests only care about *which* file the resolver
    picked, not what's in it. An empty YAML body is a valid overlay (the
    loader treats it as the default empty registry) and lets the CLI's
    eager ``validate_overlay_file`` call succeed.
    """
    path = tmp_path / name
    path.write_text("")
    return str(path)


class TestSubprocessInheritance:
    """Parent writes ``engine_run_state.json``; subprocess reads it without
    any other channel. Proves the file is the actual cross-process carrier.

    Each test materialises real (minimal but valid) overlay files because
    ``_activate_presets_overlay`` now eagerly validates every resolved
    overlay — a CLI-boundary improvement that catches typo'd paths before
    Docker auto-starts.
    """

    def test_subprocess_reads_queue_state_overlay(self, tmp_path: Path) -> None:
        queue_overlay = _write_empty_overlay(tmp_path, "queue.yaml")
        write_engine_run_state(tmp_path, presets_file=queue_overlay)
        result = _run_resolver(run_dir=tmp_path, cli_value=None, config_value=None)
        assert result["resolved"] == queue_overlay
        assert result["installed"] == queue_overlay

    def test_subprocess_cli_flag_beats_queue_state(self, tmp_path: Path) -> None:
        queue_overlay = _write_empty_overlay(tmp_path, "queue.yaml")
        cli_overlay = _write_empty_overlay(tmp_path, "cli.yaml")
        write_engine_run_state(tmp_path, presets_file=queue_overlay)
        result = _run_resolver(run_dir=tmp_path, cli_value=cli_overlay, config_value=None)
        assert result["resolved"] == cli_overlay

    def test_subprocess_queue_state_beats_config_field(self, tmp_path: Path) -> None:
        queue_overlay = _write_empty_overlay(tmp_path, "queue.yaml")
        config_overlay = _write_empty_overlay(tmp_path, "config.yaml")
        write_engine_run_state(tmp_path, presets_file=queue_overlay)
        result = _run_resolver(run_dir=tmp_path, cli_value=None, config_value=config_overlay)
        assert result["resolved"] == queue_overlay

    def test_subprocess_no_queue_state_falls_through_to_config(self, tmp_path: Path) -> None:
        # No engine_run_state.json written.
        config_overlay = _write_empty_overlay(tmp_path, "config.yaml")
        result = _run_resolver(run_dir=tmp_path, cli_value=None, config_value=config_overlay)
        assert result["resolved"] == config_overlay

    def test_subprocess_persisted_none_falls_through_to_config(self, tmp_path: Path) -> None:
        # ``prepare`` ran without an overlay → persisted as null →
        # subprocess falls through to engine.presets_file.
        config_overlay = _write_empty_overlay(tmp_path, "config.yaml")
        write_engine_run_state(tmp_path, presets_file=None)
        result = _run_resolver(run_dir=tmp_path, cli_value=None, config_value=config_overlay)
        assert result["resolved"] == config_overlay

    def test_subprocess_no_overlay_anywhere_yields_none(self) -> None:
        result = _run_resolver(run_dir=None, cli_value=None, config_value=None)
        assert result["resolved"] is None
        assert result["installed"] is None


def _collapse(text: str) -> str:
    """Drop whitespace from rich-rendered console output so long paths that
    wrap across visual lines still match a single-line expected substring."""
    return "".join(text.split())


class TestRealCLISubprocessConfigValidate:
    """Exercise the actual ``tolokaforge config validate`` CLI in a subprocess
    with ``--presets-file``. Verifies the operator-facing surface — flag is
    accepted, good overlays surface a ✓ line, bad overlays surface the
    file-path-bearing error and exit non-zero.

    Uses the public ``tool_use`` example config as a host run-config so we
    exercise the same code path operators use.
    """

    EXAMPLE_CONFIG = "examples/native/tool_use/run_config.yaml"

    def test_good_overlay_exits_zero(self, tmp_path: Path) -> None:
        overlay = tmp_path / "good_overlay.yaml"
        overlay.write_text(
            "presets:\n"
            "  subproc_test:\n"
            "    match: ['subproctest/*']\n"
            "    response_policy: array_dict_map\n"
        )
        proc = subprocess.run(
            [
                "uv",
                "run",
                "tolokaforge",
                "config",
                "validate",
                "--config",
                self.EXAMPLE_CONFIG,
                "--presets-file",
                str(overlay),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        # We accept exit 0 (no errors) — API-key warnings don't fail validate
        # without --strict.
        assert proc.returncode == 0, f"validate failed unexpectedly:\n{proc.stdout}\n{proc.stderr}"
        normalized = _collapse(proc.stdout)
        assert "PresetoverlayOK" in normalized
        assert _collapse(str(overlay)) in normalized

    def test_bad_overlay_loud_fails(self, tmp_path: Path) -> None:
        overlay = tmp_path / "bad_overlay.yaml"
        overlay.write_text(
            "presets:\n"
            "  subproc_bad:\n"
            "    match: ['subprocbad/*']\n"
            "    response_policy: not_a_real_policy\n"
        )
        proc = subprocess.run(
            [
                "uv",
                "run",
                "tolokaforge",
                "config",
                "validate",
                "--config",
                self.EXAMPLE_CONFIG,
                "--presets-file",
                str(overlay),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode != 0, "validate should exit non-zero on bad overlay"
        normalized = _collapse(proc.stdout)
        assert "unknownresponse_policy" in normalized
        # Error message must name the file path.
        assert _collapse(str(overlay)) in normalized

    def test_missing_overlay_loud_fails(self, tmp_path: Path) -> None:
        proc = subprocess.run(
            [
                "uv",
                "run",
                "tolokaforge",
                "config",
                "validate",
                "--config",
                self.EXAMPLE_CONFIG,
                "--presets-file",
                str(tmp_path / "does_not_exist.yaml"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode != 0
        normalized = _collapse(proc.stdout).lower()
        assert "notfound" in normalized or "does_not_exist" in normalized


class TestSubprocessIsolation:
    """Sanity: the subprocess starts with no installed overlay even if the
    parent test process has one. This is the property we rely on for
    ``engine_run_state.json`` to be the only carrier across the boundary.
    """

    def test_subprocess_starts_with_no_overlay(self) -> None:
        # Parent process may or may not have an overlay installed; the
        # subprocess MUST start fresh. Run with explicit None inputs.
        result = _run_resolver(run_dir=None, cli_value=None, config_value=None)
        assert result["installed"] is None
