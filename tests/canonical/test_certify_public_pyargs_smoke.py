"""Smoke: an out-of-tree caller can drive the suite via ``--pyargs``.

Asserts the acceptance-criterion contract:

    pytest --pyargs tolokaforge.testing.certify.suite --collect-only -q

returns exit code 0 and produces a non-empty list of nodeids. No live
provider calls — just the collection path, so this is safe for CI.

Distinct from :mod:`test_certify_suite_collection`: that test pins the
byte-identical *set* of nodeids the suite must expose. This test only
guarantees that the ``--pyargs`` entry point works at all, so a broken
package layout — e.g. a missing ``__init__.py`` under the suite dir —
fails here loudly rather than presenting a mysterious zero-item run.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.canonical


def test_pyargs_collection_returns_zero_and_nonempty() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--pyargs",
            "tolokaforge.testing.certify.suite",
            "--collect-only",
            "-q",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"pytest --pyargs collection failed with rc={proc.returncode}\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    nodeids = [line for line in proc.stdout.splitlines() if "::" in line]
    assert nodeids, (
        "pytest --pyargs collection produced zero nodeids — the suite "
        "package is empty or unreachable via --pyargs."
    )
