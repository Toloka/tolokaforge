"""The ``ExecInService`` wire lane carries the identity grading runs as.

The detached (wire) lane and the in-process grading substrate reach a trial's
containers through one implementation — :meth:`RunnerServiceImpl.
_exec_in_service` — so the staging probe, the collect hooks and the report
reads cannot run as one user in process and another across gRPC. A pack that
seals its reward directory against its agent is denied on exactly the paths it
grades by when they do, and the denial is recorded as the command's output.

These pin the servicer edge of that lane: the user threads back off the request
onto the helper, and an unreachable container still reports through ``error``.
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
    """Stands in for ``RunnerServiceImpl``: a recording ``_exec_in_service``."""

    def __init__(self, result: tuple[int, str] = (0, "ok")) -> None:
        self.trials = {"trial-1": object()}
        self._result = result
        self.calls: list[dict[str, Any]] = []

    def _exec_in_service(
        self,
        *,
        service: str | None,
        command: str,
        timeout_s: float,
        trial_id: str,
        user: str | None = None,
    ) -> tuple[int, str]:
        self.calls.append(
            {
                "service": service,
                "command": command,
                "timeout_s": timeout_s,
                "user": user,
                "trial_id": trial_id,
            }
        )
        return self._result


def test_the_servicer_threads_the_user_onto_the_helper() -> None:
    runner = _FakeRunner()
    servicer = SubstrateServicer(runner)  # type: ignore[arg-type]
    request = pb2.ExecInServiceRequest(
        trial_id="trial-1",
        service="verifier",
        command="cat /logs/verifier/ctrf.json",
        timeout_s=7.0,
        user="root",
    )

    response = servicer.ExecInService(request, MagicMock())

    assert runner.calls == [
        {
            "service": "verifier",
            "command": "cat /logs/verifier/ctrf.json",
            "timeout_s": 7.0,
            "user": "root",
            "trial_id": "trial-1",
        }
    ]
    assert (response.exit_code, response.output) == (0, "ok")


def test_an_unset_user_arrives_as_the_container_default() -> None:
    """proto3 has no null string, so empty means "the container's own user"."""
    runner = _FakeRunner()
    servicer = SubstrateServicer(runner)  # type: ignore[arg-type]
    request = pb2.ExecInServiceRequest(trial_id="trial-1", command="collect", timeout_s=0.0)

    servicer.ExecInService(request, MagicMock())

    assert runner.calls[0]["user"] is None
    assert runner.calls[0]["service"] is None
    assert runner.calls[0]["timeout_s"] == 300.0  # 0.0 → servicer default


def test_a_servicer_side_unreachable_still_rides_the_error_field() -> None:
    """The user does not change how an unreachable container is reported."""
    runner = _FakeRunner()
    runner._exec_in_service = MagicMock(  # type: ignore[method-assign]
        side_effect=SubstrateUnreachableError("no container")
    )
    servicer = SubstrateServicer(runner)  # type: ignore[arg-type]
    request = pb2.ExecInServiceRequest(trial_id="trial-1", command="collect", user="root")

    response = servicer.ExecInService(request, MagicMock())

    assert response.error == "no container"
    assert response.exit_code == 0
