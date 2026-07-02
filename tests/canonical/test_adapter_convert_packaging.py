"""Packaging + CLI smoke for ``tolokaforge adapter convert``.

Guards two failure modes the in-process tests can't catch:

* A source file (e.g. ``tolokaforge/adapters/bundle_writer.py``) not
  making it into the built wheel — the exact regression that shipped in
  GH #37. In-process tests keep passing because the file exists on
  disk; end-users installing from PyPI hit ``ImportError``.
* The ``tolokaforge`` console script + entry-point-based adapter
  discovery failing when invoked as a real subprocess (import ordering,
  missing dependency at CLI import time, entry-point group name typo).

Both tests run under the ``canonical`` marker so they participate in
the existing CI smoke job without needing dedicated workflow wiring.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.canonical


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _uv_available() -> bool:
    """True if the ``uv`` CLI is on PATH — otherwise these tests skip loud."""
    return shutil.which("uv") is not None


@pytest.mark.skipif(not _uv_available(), reason="uv CLI not available")
def test_bundle_writer_ships_in_the_built_wheel(tmp_path: Path) -> None:
    """``tolokaforge.adapters.bundle_writer`` must be present in the wheel
    hatchling produces.

    A structural regression like PR #37 (module omitted from the built
    wheel because of a hatch config gap) is invisible to any in-process
    test — the source tree still has the file. The only way to catch it
    is to build the wheel and look inside.
    """
    build_result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert build_result.returncode == 0, (
        f"uv build failed (rc={build_result.returncode}):\n"
        f"stdout:\n{build_result.stdout}\nstderr:\n{build_result.stderr}"
    )

    wheels = sorted(tmp_path.glob("tolokaforge-*.whl"))
    assert wheels, f"No tolokaforge wheel produced under {tmp_path}"
    wheel = wheels[-1]

    with zipfile.ZipFile(wheel) as zf:
        members = set(zf.namelist())

    required = {
        "tolokaforge/adapters/bundle_writer.py",
        "tolokaforge/adapters/base.py",
        "tolokaforge/adapters/native.py",
        "tolokaforge/adapters/__init__.py",
        "tolokaforge/cli/adapter_commands.py",
        "tolokaforge/cli/main.py",
    }
    missing = required - members
    assert not missing, (
        f"Wheel {wheel.name} is missing modules that adapter convert depends on: "
        f"{sorted(missing)}. Members starting with 'tolokaforge/adapters/': "
        f"{sorted(m for m in members if m.startswith('tolokaforge/adapters/'))}"
    )


@pytest.mark.skipif(not _uv_available(), reason="uv CLI not available")
def test_adapter_convert_cli_help_runs_via_subprocess() -> None:
    """``uv run tolokaforge adapter convert --help`` exits 0 and prints usage.

    Proves the installed console script wires, ``tolokaforge.cli.main``
    imports cleanly, the ``adapter`` subgroup is registered, and every
    module the CLI reaches at import time (including
    ``tolokaforge.adapters.bundle_writer``) resolves under the real
    Python resolver — not a monkeypatched one.
    """
    result = subprocess.run(
        ["uv", "run", "tolokaforge", "adapter", "convert", "--help"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        f"CLI help failed (rc={result.returncode}):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "--output" in combined, f"Usage missing --output flag:\n{combined}"
    assert "--tasks-glob" in combined, f"Usage missing --tasks-glob flag:\n{combined}"


def test_bundle_writer_module_imports_cleanly() -> None:
    """In-process import guard — the module symbols the CLI reaches at
    call time must resolve. Cheap runtime check that complements the
    wheel-inspection test above."""
    import tolokaforge.adapters.bundle_writer as bw

    assert callable(bw.write_bundle), (
        "tolokaforge.adapters.bundle_writer.write_bundle must be callable — "
        "the CLI adapter-convert path relies on this symbol."
    )
    # sys.modules registration proves the import actually took effect
    # (rather than a stale cache satisfying attribute lookups).
    assert "tolokaforge.adapters.bundle_writer" in sys.modules
