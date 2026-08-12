"""Runtime-level check that :mod:`tolokaforge.core.model_data` stays light.

The module is the runner-subset-safe seam for the bundled model-data
files and the fingerprint schema. Its import must not pull in the
orchestrator-only siblings (:mod:`tolokaforge.core.llm.presets`,
:mod:`tolokaforge.core.pricing`, :mod:`tolokaforge.testing.certify`) or
its own fingerprint compute sibling
(:mod:`tolokaforge.core.model_data_fingerprint`).

Complementary to the AST-based partition guard in
``tests/canonical/test_runner_subset_partition.py``: the static scan
catches ``ImportFrom`` nodes at any nesting depth; this test catches
transitive imports the AST cannot see — e.g. a Pydantic model that
lazily grew a heavy validator import in a future edit.

Runs in a fresh interpreter so the assertion is unaffected by whatever
modules the pytest process itself has loaded.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.canonical


_PROBE = """\
import sys
import tolokaforge.core.model_data  # noqa: F401

forbidden = (
    "tolokaforge.core.llm.presets",
    "tolokaforge.core.pricing",
    "tolokaforge.testing.certify",
    "tolokaforge.core.model_data_fingerprint",
)
loaded = sorted(name for name in forbidden if name in sys.modules)
if loaded:
    raise SystemExit("LOADED: " + ",".join(loaded))
"""


def test_model_data_module_import_is_light() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, (
        f"importing tolokaforge.core.model_data pulled in heavy siblings:\n"
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
