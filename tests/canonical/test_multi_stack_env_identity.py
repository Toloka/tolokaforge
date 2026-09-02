"""Canonical env-identity digest for a multi-stack composition plan.

Locks the ADR-0044 § 7 byte-parity contract: a two-stack manifest
resolved through :func:`tolokaforge.core.project_loader.resolve` produces
a stable ``sha256:<hex>`` digest whose exact bytes are pinned here. The
digest shifts under any per-stack input change (plan order, scope, runner
service, inputs, compose bytes) — the two-shape distinction is exercised
by ``tests/unit/test_env_identity.py::TestMultiStackDigest``; this test
locks the emitted digest string for a canonical multi-stack shape.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.core.env_identity import resolve_environment_identity
from tolokaforge.core.project_loader import resolve
from tolokaforge.runner.models import EnvironmentPatch, StackPatch

pytestmark = pytest.mark.canonical


_FIXTURES = Path(__file__).parent / "fixtures" / "environment_manifest"


_PINNED_MULTI_STACK_DIGEST = (
    "sha256:f73a8a910b6ed82aa273c85014a72c6bb71ba1f1480aae4f29e5d224c06f25fd"
)


def _two_stack_manifest():
    """Resolve a canonical two-stack manifest — one ``run``-scope engine
    stack (owns the runner) and one ``trial``-scope task stack."""
    project_patch = EnvironmentPatch(
        stacks={
            "engine": StackPatch(
                compose_file=_FIXTURES / "safe_two_service.yaml",
                stack_scope="run",
                runner_service="default",
                inputs={"PG": "16"},
            ),
            "task": StackPatch(
                compose_file=_FIXTURES / "safe_one_service.yaml",
                stack_scope="trial",
                inputs={"WORKSPACE": "clean"},
            ),
        }
    )
    manifest = resolve(project_env=project_patch, task_env=None)
    assert manifest is not None
    return manifest


class TestMultiStackDigestPinned:
    def test_two_stack_manifest_pins_expected_digest(self) -> None:
        manifest = _two_stack_manifest()
        assert resolve_environment_identity(manifest, None) == _PINNED_MULTI_STACK_DIGEST

    def test_two_stack_manifest_digest_is_stable(self) -> None:
        manifest = _two_stack_manifest()
        assert resolve_environment_identity(manifest, None) == resolve_environment_identity(
            manifest, None
        )
