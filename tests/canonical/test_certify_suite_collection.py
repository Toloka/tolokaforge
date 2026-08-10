"""Canonical snapshot of the certification suite's collected nodeids.

Pins the byte-identical acceptance bar for
:mod:`tolokaforge.testing.certify.suite`. Any change that adds a
capability probe, drops a certificate, alters the ``ids=lambda c:
c.model_id`` parametrise shape, or otherwise perturbs the ``(test file,
test function, parameters)`` set surfaced by ``pytest --collect-only``
against the suite lands here — a matched delta is regenerated with
``--update-canon``, an unmatched one fails the canon.

The snapshot lists the collected node ids with the package-path prefix
stripped, so the same set is asserted whether the collection came from
``pytest --pyargs tolokaforge.testing.certify.suite`` (installed wheel)
or from an in-source ``pytest tolokaforge/testing/certify/suite/``
invocation.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.canonical

_SUITE_PYARGS = "tolokaforge.testing.certify.suite"
_SUITE_PATH_PREFIX = "tolokaforge/testing/certify/suite/"


def _collect_via_pyargs() -> list[str]:
    """Return the sorted, path-prefix-stripped nodeids collected by pytest.

    Uses a subprocess so pytest's collection runs in a clean interpreter
    — no test-time mutation of ``sys.modules`` leaks between the outer
    canon test and the inner collection.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--pyargs",
            _SUITE_PYARGS,
            "--collect-only",
            "-q",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    nodeids: list[str] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if "::" not in line:
            continue
        idx = line.find(_SUITE_PATH_PREFIX)
        if idx == -1:
            raise AssertionError(
                f"collected nodeid does not carry the expected package prefix "
                f"{_SUITE_PATH_PREFIX!r}: {line!r}"
            )
        nodeids.append(line[idx + len(_SUITE_PATH_PREFIX) :])
    return sorted(set(nodeids))


def test_certify_suite_collection_snapshot(canon_snapshot) -> None:
    """Assert the ``--pyargs`` collection set matches the committed snapshot."""
    collected = _collect_via_pyargs()
    assert collected, "pytest --collect-only produced zero nodeids for the certify suite"
    canon_snapshot("certify_suite_collection").assert_match(
        {"nodeids": collected}, "collection.json"
    )
