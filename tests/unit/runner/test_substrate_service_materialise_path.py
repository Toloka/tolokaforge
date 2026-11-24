"""The ``MaterialisePath`` wire lane carries the identity grading runs as.

The detached (wire) lane and the in-process grading substrate reach a trial's
containers through one implementation — :meth:`RunnerServiceImpl.
_materialise_path` — so the destination preparation cannot run as one user in
process and another across gRPC. On a verifier image declaring a non-root
``USER`` over a destination the pack owns, the one that loses the user cannot
wipe the path or recreate its parent, and the copy lands on a half-prepared
tree: the agent's work never reaches the container that scores it.

These pin the servicer edge of that lane: the user threads back off the request
onto the helper, and an unpreparable destination still reports through ``error``.
The client edge and the wire itself are pinned by a real round-trip in
``tests/canonical/test_substrate_service_user_roundtrip.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.grading.substrate import SubstrateUnreachableError
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.substrate_service import SubstrateServicer

pytestmark = pytest.mark.unit


class _FakeRunner:
    """Stands in for ``RunnerServiceImpl``: a recording ``_materialise_path``."""

    def __init__(self) -> None:
        self.trials = {"trial-1": object()}
        self.calls: list[dict[str, Any]] = []

    def _materialise_path(
        self,
        *,
        src_service: str | None,
        dst_service: str | None,
        path: str,
        exclude: list[str],
        trial_id: str,
        user: str | None = None,
    ) -> None:
        self.calls.append(
            {
                "src_service": src_service,
                "dst_service": dst_service,
                "path": path,
                "exclude": exclude,
                "user": user,
                "trial_id": trial_id,
            }
        )


def test_the_servicer_threads_the_user_onto_the_helper() -> None:
    runner = _FakeRunner()
    servicer = SubstrateServicer(runner)  # type: ignore[arg-type]
    request = pb2.MaterialisePathRequest(
        trial_id="trial-1",
        src_service="",
        dst_service="verifier",
        path="/app/output",
        exclude=["__pycache__"],
        user="root",
    )

    response = servicer.MaterialisePath(request, MagicMock())

    assert runner.calls == [
        {
            "src_service": None,
            "dst_service": "verifier",
            "path": "/app/output",
            "exclude": ["__pycache__"],
            "user": "root",
            "trial_id": "trial-1",
        }
    ]
    assert response.ok


def test_an_unset_user_arrives_as_the_container_default() -> None:
    """proto3 has no null string, so empty means "the container's own user"."""
    runner = _FakeRunner()
    servicer = SubstrateServicer(runner)  # type: ignore[arg-type]
    request = pb2.MaterialisePathRequest(trial_id="trial-1", path="/tests")

    servicer.MaterialisePath(request, MagicMock())

    assert runner.calls[0]["user"] is None


def test_a_servicer_side_unreachable_still_rides_the_error_field() -> None:
    """The user does not change how an unpreparable destination is reported."""
    runner = _FakeRunner()
    runner._materialise_path = MagicMock(  # type: ignore[method-assign]
        side_effect=SubstrateUnreachableError("preparing '/app/output' as root failed")
    )
    servicer = SubstrateServicer(runner)  # type: ignore[arg-type]
    request = pb2.MaterialisePathRequest(trial_id="trial-1", path="/app/output", user="root")

    response = servicer.MaterialisePath(request, MagicMock())

    assert not response.ok
    assert response.error == "preparing '/app/output' as root failed"
