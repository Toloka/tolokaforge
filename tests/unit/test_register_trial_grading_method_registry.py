"""``RegisterTrial`` refuses an unregistered ``grading.grading_method``.

The wire model accepts any string so a downstream adapter can register
its own dispatch under ``tolokaforge.grading_methods`` without a
framework PR (see ``tests/canonical/test_grading_methods_registry.py``).
The safety net lives in ``RegisterTrial``: every value crossing the wire
must resolve against the entry-point registry, and an unknown name is
refused with a message naming both the offending key and the registered
set — matching the fail-loud shape
:func:`~tolokaforge.adapters.ensure_registered_adapter` sets for the
adapter registry.

The registered marker only asserts the name exists; runtime dispatch on
:meth:`RunnerServiceImpl.GradeTrial` still branches on the wire string.
An adapter that ships a marker but no matching branch would still hit
the composite fold at grade time.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.utils.runner_requests import register_request, trial_spec_json
from tolokaforge.runner.service import RunnerServiceImpl

pytestmark = pytest.mark.unit


@pytest.fixture
def service(db_client: Any) -> Any:
    impl = RunnerServiceImpl(db_client)
    try:
        yield impl
    finally:
        impl.shutdown()


def _task(grading_method: str | None) -> dict[str, Any]:
    task: dict[str, Any] = {
        "task_id": "grading_method_gate",
        "name": "grading_method_gate",
        "category": "test",
        "description": "Exercises the RegisterTrial registry gate on grading_method.",
        "adapter_type": "native",
        "system_prompt": "You are a test assistant.",
        "initial_state": {"tables": {}, "schemas": []},
        "agent_tools": [],
        "user_tools": [],
    }
    if grading_method is not None:
        task["grading"] = {"grading_method": grading_method}
    return task


def _register(service: Any, context: Any, trial_id: str, task: dict[str, Any]) -> Any:
    return service.RegisterTrial(
        register_request(trial_spec_json(task, trial_id=trial_id), trial_id=trial_id), context
    )


def test_register_trial_rejects_unknown_grading_method(
    service: Any, mock_grpc_context: Any
) -> None:
    response = _register(
        service, mock_grpc_context, "unknown:0", _task(grading_method="totally_unknown")
    )

    assert response.success is False
    assert "totally_unknown" in response.error
    assert "composite" in response.error
    assert "test_execution" in response.error
    assert "tolokaforge.grading_methods" in response.error


def test_register_trial_accepts_known_grading_method(service: Any, mock_grpc_context: Any) -> None:
    response = _register(
        service, mock_grpc_context, "known:0", _task(grading_method="test_execution")
    )

    assert response.success is True, response.error


def test_register_trial_accepts_composite_grading_method(
    service: Any, mock_grpc_context: Any
) -> None:
    response = _register(
        service, mock_grpc_context, "composite:0", _task(grading_method="composite")
    )

    assert response.success is True, response.error


def test_register_trial_accepts_none_grading_method(service: Any, mock_grpc_context: Any) -> None:
    response = _register(service, mock_grpc_context, "none:0", _task(grading_method=None))

    assert response.success is True, response.error
