"""Pin the ``TrialExecutor`` Protocol contract.

Every concrete executor must satisfy the Protocol via ``isinstance`` (not
just structural type-hint compatibility) and produce a
:class:`TrialResult` bracketing whatever substrate-lifecycle it owns.
This file is the load-bearing contract when future implementations land
(RemoteTrialExecutor over gRPC, non-provisioning stub executors for
smoke tests).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.conductor import InMemoryConductor
from tolokaforge.core.runtime import InMemoryRuntimeBackend
from tolokaforge.core.trial import TrialResult
from tolokaforge.core.trial_executor import ProvisioningTrialExecutor, TrialExecutor

pytestmark = pytest.mark.canonical


def _make_executor() -> ProvisioningTrialExecutor:
    return ProvisioningTrialExecutor(
        runtime_backend=InMemoryRuntimeBackend(),
        conductor=InMemoryConductor(),
        logger=MagicMock(),
        output_dir=Path("/nonexistent-run-dir"),
    )


class TestProtocolRuntimeCheck:
    """The Protocol is ``@runtime_checkable``; every implementation
    satisfies it structurally.
    """

    def test_provisioning_trial_executor_passes_isinstance(self) -> None:
        assert isinstance(_make_executor(), TrialExecutor)

    def test_random_object_does_not_pass_isinstance(self) -> None:
        class _NotAnExecutor:
            pass

        assert not isinstance(_NotAnExecutor(), TrialExecutor)

    def test_object_with_matching_shape_passes_isinstance(self) -> None:
        class _DuckExecutor:
            def execute(self, spec: object, task_config: object) -> TrialResult:  # pragma: no cover
                raise NotImplementedError

        assert isinstance(_DuckExecutor(), TrialExecutor)


class TestBracketedExecutionShape:
    """Any :class:`TrialExecutor` produces a :class:`TrialResult` carrying
    a matching ``trial_id`` and a :class:`Trajectory`. A regression here
    would silently break downstream aggregation.
    """

    def test_execute_returns_trial_result_with_matching_trial_id(self) -> None:
        from tests.canonical._factories import make_task_config, make_trial_spec

        executor = _make_executor()
        spec = make_trial_spec(trial_id="task-x:2")

        result = executor.execute(spec, make_task_config(task_id="task-x"))

        assert isinstance(result, TrialResult)
        assert result.trial_id == "task-x:2"
        assert result.trajectory is not None
