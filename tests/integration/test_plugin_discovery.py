"""Downstream plug-in discovered end-to-end.

Installs an out-of-tree fixture package (``tests/fixtures/tolokaforge_plugin_fixture``,
not a uv workspace member) into an isolated target directory and probes it in a
**fresh subprocess** — so ``importlib.metadata`` state never leaks between the
positive and negative cases. The fixture registers one name in each of the three
seam groups (``fixture_backend`` / ``fixture_grader`` / ``fixture_conductor``):

* installed (its target on ``PYTHONPATH``) → each name is listed by
  ``entry_points`` and its loader builds the fixture impl;
* not installed → each name is absent and its loader raises
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
from types import SimpleNamespace
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.plugin_registry import (
    RuntimeBackendBuildContext,
    TrialGraderContext,
    load_conductor,
    load_runtime_backend,
    load_trial_grader,
)

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

grader_names = [ep.name for ep in im.entry_points(group="tolokaforge.trial_graders")]
assert "fixture_grader" in grader_names, f"fixture_grader not discovered: {sorted(grader_names)}"

grader_factory = load_trial_grader("fixture_grader")
assert grader_factory.__name__ == "fixture_grader_factory", grader_factory
grader = grader_factory(TrialGraderContext(runtime_backend=backend, logger=StructuredLogger("probe")))
assert type(grader).__name__ == "FixtureGrader", type(grader).__name__

conductor_names = [ep.name for ep in im.entry_points(group="tolokaforge.conductors")]
assert "fixture_conductor" in conductor_names, f"fixture_conductor not discovered: {sorted(conductor_names)}"

conductor_factory = load_conductor("fixture_conductor")
assert conductor_factory.__name__ == "fixture_conductor_factory", conductor_factory
# fixture_conductor_factory reads only ctx.trial_grader; a full ConductorContext
# would drag in the Docker/LLM deps this keyless discovery lane must avoid.
conductor = conductor_factory(SimpleNamespace(trial_grader=grader))
assert type(conductor).__name__ == "FixtureConductor", type(conductor).__name__
"""

_PROBE_ABSENT = """
import importlib.metadata as im
from tolokaforge.core.plugin_registry import (
    UnknownImplementationError,
    load_conductor,
    load_runtime_backend,
    load_trial_grader,
)

for group, seam in (
    ("tolokaforge.runtime_backends", "fixture_backend"),
    ("tolokaforge.trial_graders", "fixture_grader"),
    ("tolokaforge.conductors", "fixture_conductor"),
):
    names = [ep.name for ep in im.entry_points(group=group)]
    assert seam not in names, f"{seam} leaked into env: {sorted(names)}"

for loader, seam in (
    (load_runtime_backend, "fixture_backend"),
    (load_trial_grader, "fixture_grader"),
    (load_conductor, "fixture_conductor"),
):
    try:
        loader(seam)
    except UnknownImplementationError:
        pass
    else:
        raise AssertionError(f"expected UnknownImplementationError for uninstalled {seam!r}")
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
