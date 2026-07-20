"""Downstream plug-in discovered end-to-end.

Installs an out-of-tree fixture package (``tests/fixtures/tolokaforge_plugin_fixture``,
not a uv workspace member) into an isolated target directory and probes it in a
**fresh subprocess** — so ``importlib.metadata`` state never leaks between the
positive and negative cases:

* installed (its target on ``PYTHONPATH``) → ``fixture_backend`` is listed by
  ``entry_points`` and ``load_runtime_backend`` builds the fixture impl;
* not installed → the name is absent and ``load_runtime_backend`` raises
  ``UnknownImplementationError``.

The pairing proves the discovery is driven by installed package metadata, not by
anything in tolokaforge's own tree. Does not run a full ``tolokaforge run``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "tolokaforge_plugin_fixture"

_PROBE_INSTALLED = """
import importlib.metadata as im
from tolokaforge.core.plugin_registry import RuntimeBackendBuildContext, load_runtime_backend

names = [ep.name for ep in im.entry_points(group="tolokaforge.runtime_backends")]
assert "fixture_backend" in names, f"fixture_backend not discovered: {sorted(names)}"

factory = load_runtime_backend("fixture_backend")
assert factory.__name__ == "fixture_backend_factory", factory
backend = factory(
    RuntimeBackendBuildContext(
        runner_address="fixture:0", env_manifest=None, run_id="r", seeds={}, log_capture=None
    )
)
assert type(backend).__name__ == "FixtureRuntimeBackend", type(backend).__name__
"""

_PROBE_ABSENT = """
import importlib.metadata as im
from tolokaforge.core.plugin_registry import load_runtime_backend, UnknownImplementationError

names = [ep.name for ep in im.entry_points(group="tolokaforge.runtime_backends")]
assert "fixture_backend" not in names, f"fixture_backend leaked into env: {sorted(names)}"

try:
    load_runtime_backend("fixture_backend")
except UnknownImplementationError:
    pass
else:
    raise AssertionError("expected UnknownImplementationError for an uninstalled name")
"""


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not available")
def test_downstream_plugin_discoverable_only_when_installed(tmp_path: Path) -> None:
    target = tmp_path / "site"
    install = subprocess.run(
        ["uv", "pip", "install", "--target", str(target), str(_FIXTURE_DIR)],
        capture_output=True,
        text=True,
    )
    assert install.returncode == 0, f"fixture install failed:\n{install.stderr}"

    installed_env = dict(os.environ)
    installed_env["PYTHONPATH"] = os.pathsep.join(
        [str(target), installed_env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    positive = subprocess.run(
        [sys.executable, "-c", _PROBE_INSTALLED],
        capture_output=True,
        text=True,
        env=installed_env,
    )
    assert positive.returncode == 0, f"installed probe failed:\n{positive.stderr}"

    # The fresh tmp target is never on the ambient path, so the default
    # environment is the uninstalled case — isolation is structural.
    negative = subprocess.run(
        [sys.executable, "-c", _PROBE_ABSENT],
        capture_output=True,
        text=True,
    )
    assert negative.returncode == 0, f"uninstalled probe failed:\n{negative.stderr}"
