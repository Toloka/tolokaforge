"""``RegisterTrial`` refuses an unregistered ``grading.grading_method``.

The wire model accepts any string so a downstream adapter can register
its own dispatch under both ``tolokaforge.grading_methods`` and
``tolokaforge.grader_kinds`` without a framework PR
(see ``tests/canonical/test_grading_methods_registry.py`` +
``tests/canonical/test_grader_kinds_registry.py``). The safety net lives
in ``RegisterTrial``: every value crossing the wire must resolve against
BOTH entry-point registries, and an unknown name — or one registered in
only one of the two groups — is refused with a message naming both group
names + the offending key + the union of registered names.

Runtime dispatch on :meth:`RunnerServiceImpl.GradeTrial` routes every
non-composite name through the typed ``GraderKind`` via
:meth:`RunnerServiceImpl._dispatch_via_grader_kind`; composite (or
``None``) stays on the runner-side fold.
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


def test_register_trial_refuses_name_missing_from_grader_kinds_group(
    service: Any, mock_grpc_context: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dual-lookup lock: a name registered in ``tolokaforge.grading_methods``
    but MISSING from ``tolokaforge.grader_kinds`` fails at ``RegisterTrial``
    with an error naming both group names + the union of registered names.
    Exercises the D5 dual-registration invariant."""
    from tolokaforge.core import plugin_registry as pr

    original_discover = pr.discover_entry_points

    def scoped_discover(group: str):
        if group == pr.GRADER_KINDS_GROUP:
            # Return a mapping missing 'test_execution' — simulates a
            # downstream adapter that registered under grading_methods only.
            return {
                name: ep
                for name, ep in original_discover(group).items()
                if name != "test_execution"
            }
        return original_discover(group)

    monkeypatch.setattr(pr, "_discovery_cache", {})
    monkeypatch.setattr(pr, "discover_entry_points", scoped_discover)

    response = _register(
        service, mock_grpc_context, "dual_lookup:0", _task(grading_method="test_execution")
    )

    assert response.success is False
    assert "test_execution" in response.error
    assert "tolokaforge.grading_methods" in response.error
    assert "tolokaforge.grader_kinds" in response.error
    # Union of registered names still names 'composite' (present in both groups).
    assert "composite" in response.error
