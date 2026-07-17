"""Entry-point discovery smoke for ``tolokaforge adapter convert``.

Installs the built ``tolokaforge`` wheel plus a separately-built demo adapter
(registered through a ``tolokaforge.adapters`` entry point) into an isolated
scratch venv, then drives ``tolokaforge adapter convert --validate`` through
that venv's own console script.

This is the only test that exercises the production adapter-discovery path —
``importlib.metadata.entry_points(group="tolokaforge.adapters")`` + ``ep.load()``
— against a genuinely separate distribution, the same mechanism external
adapters use. It is the class of packaging regression GH #37 was.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.canonical

_FIXTURE_PKG = Path(__file__).parent / "fixtures" / "tolokaforge-adapter-demo"


def _run(cmd: list[str], *, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    assert result.returncode == 0, (
        f"command failed (rc={result.returncode}): {' '.join(cmd)}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv CLI not available")
def test_entry_point_adapter_convert_smoke(built_wheel: Path, tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    sources.mkdir()
    task_ids = ["alpha", "beta"]
    for task_id in task_ids:
        (sources / f"{task_id}.json").write_text(
            json.dumps({"name": f"Demo {task_id}"}), encoding="utf-8"
        )

    venv_dir = tmp_path / "venv"
    _run(["uv", "venv", str(venv_dir)])
    _run(["uv", "pip", "install", "--python", str(venv_dir), str(built_wheel)])
    _run(["uv", "pip", "install", "--python", str(venv_dir), "--no-deps", str(_FIXTURE_PKG)])

    tolokaforge_bin = venv_dir / "bin" / "tolokaforge"
    python_bin = venv_dir / "bin" / "python"
    out_dir = tmp_path / "out"

    # COLUMNS=200 stops rich from soft-wrapping the validation summary line in a
    # non-tty, so the ", 0 invalid" anchor stays on one line.
    result = subprocess.run(
        [
            str(tolokaforge_bin),
            "adapter",
            "convert",
            "--name",
            "demo",
            "--tasks-glob",
            str(sources / "*.json"),
            "--output",
            str(out_dir),
            "--validate",
        ],
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, "COLUMNS": "200"},
        check=False,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, f"convert failed (rc={result.returncode}):\n{combined}"

    # Sole validation signal: #487 makes the exit code 0 and writes bundles
    # regardless of validation, so neither exit-0 nor the on-disk files below
    # corroborate this. The leading comma anchors the count to exactly 0.
    assert ", 0 invalid" in combined, f"validation did not report clean:\n{combined}"

    for task_id in task_ids:
        assert (out_dir / task_id / "task.yaml").exists()
        assert (out_dir / task_id / "grading.yaml").exists()
        assert (out_dir / task_id / "fixtures" / "tools.json").exists()

    assert (out_dir / "_shared_marker").exists()

    from tolokaforge.adapters import available_adapters

    assert "demo" not in available_adapters(), (
        "demo adapter leaked into the repo venv — the fixture package must never "
        "be a uv workspace member or enter the tolokaforge wheel"
    )

    discovery = _run(
        [
            str(python_bin),
            "-c",
            "from tolokaforge.adapters import available_adapters; print(available_adapters())",
        ],
        timeout=60,
    )
    discovered = discovery.stdout
    assert "demo" in discovered, f"demo not discovered by scratch venv: {discovered}"
