"""``TrialGraderContext`` carries serialisable configuration only.

The grader plug-in seam (``tolokaforge.trial_graders`` entry-point group) exists
so a grader can run on a different machine from the orchestrator. A live
``RuntimeBackend`` instance in the context — a gRPC channel bound to a specific
runner — would defeat that: the grader receives an object it cannot use from a
different address space. This test pins the shape at the type level so a future
PR cannot regress the seam.

See ADR-0035 (grader detachment) for the wider design record.
"""

from __future__ import annotations

import typing
from dataclasses import fields
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.plugin_registry import TrialGraderContext
from tolokaforge.core.runtime import RuntimeBackend
from tolokaforge.core.trial_grader import RunnerRPCTrialGrader, runner_rpc_trial_grader_factory

pytestmark = pytest.mark.canonical


def _resolved_types() -> dict[str, object]:
    """Return ``TrialGraderContext``'s field types with forward refs resolved.

    ``StructuredLogger`` is imported under ``TYPE_CHECKING`` in the plug-in
    registry to avoid a runtime cycle; feed it in via ``localns`` so the
    hint resolves without pulling every logger-side dependency at import time.
    """
    return typing.get_type_hints(TrialGraderContext, localns={"StructuredLogger": StructuredLogger})


class TestTrialGraderContextShape:
    """The context carries a serialisable ``runner_address`` and a logger — nothing else."""

    def test_no_field_typed_as_runtime_backend(self) -> None:
        """A live runtime-backend instance in the context couples the grader to
        the orchestrator's channel; the seam breaks when the grader runs
        elsewhere. Regressing this field's type is a compat-level bug — it
        forces a future PR to re-thread a live object through downstream
        registered graders."""
        types_by_name = _resolved_types()
        for field in fields(TrialGraderContext):
            resolved = types_by_name[field.name]
            assert resolved is not RuntimeBackend, (
                f"TrialGraderContext.{field.name} resolves to RuntimeBackend; "
                "grader context must not carry live runtime-backend instances."
            )

    def test_runner_address_is_a_string_or_none(self) -> None:
        """The address must be serialisable — a plain ``str`` (or ``None`` when
        the backend has no runner surface), not a wrapper object with a
        runtime binding. Downstream graders that read the field are
        responsible for the None-branch."""
        types_by_name = _resolved_types()
        assert types_by_name["runner_address"] == (str | None)

    def test_construction_with_unknown_kwarg_fails_loud(self) -> None:
        """Frozen dataclass — a stray ``runtime_backend=`` from stale code
        raises ``TypeError`` at construction rather than being ignored."""
        with pytest.raises(TypeError):
            TrialGraderContext(  # type: ignore[call-arg]
                runner_address="stub:0",
                logger=MagicMock(),
                runtime_backend=object(),
            )


class TestRunnerRpcTrialGraderFactory:
    """The built-in factory builds a grader that owns its own runner client — no
    channel reuse from the orchestrator."""

    def test_factory_returns_grader_with_owned_runner_client(self) -> None:
        """``runner_rpc_trial_grader_factory(ctx)`` must return a
        :class:`RunnerRPCTrialGrader` whose ``runner_client`` was built from
        the context's ``runner_address`` — a fresh client per grader."""

        class _StubClient:
            def __init__(self, runner_address: str) -> None:
                self.runner_address = runner_address

        import tolokaforge.core.shared_stack_runtime as ssr

        original = ssr.GrpcRunnerClient
        ssr.GrpcRunnerClient = _StubClient  # type: ignore[misc,assignment]
        try:
            ctx = TrialGraderContext(runner_address="test-runner:9999", logger=MagicMock())
            grader = runner_rpc_trial_grader_factory(ctx)
        finally:
            ssr.GrpcRunnerClient = original  # type: ignore[misc]

        assert isinstance(grader, RunnerRPCTrialGrader)
        assert grader.runner_address == "test-runner:9999"
        assert isinstance(grader.runner_client, _StubClient)
        assert grader.runner_client.runner_address == "test-runner:9999"
