"""``TrialGraderContext`` carries serialisable configuration only, plus one
optional in-process routing shim.

The grader plug-in seam (``tolokaforge.trial_graders`` entry-point group) exists
so a grader can run on a different machine from the orchestrator (ADR-0038). A
required live ``RuntimeBackend`` instance in the context would defeat that: a
grader crossing an address boundary cannot use an object bound to another
process. The ``runtime_backend`` field on the context is therefore ``Optional``
and MUST be ``None`` whenever the grader will cross an address boundary; when
set (in-process case), only ``RunnerRPCTrialGrader`` reads it, and every
out-of-process factory MUST ignore it. This test pins the shape at the type
level and pins the ignore contract for every bundled out-of-process factory so
a future PR cannot regress the seam.

See ADR-0038 (grader detachment) for the wider design record.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import typing
from dataclasses import fields
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models.run_config import GraderConfig
from tolokaforge.core.plugin_registry import TrialGraderContext
from tolokaforge.core.runtime import RuntimeBackend
from tolokaforge.core.trial_grader import (
    GraderRPCTrialGrader,
    JudgeBackedTrialGrader,
    RunnerRPCTrialGrader,
    grader_rpc_trial_grader_factory,
    judge_backed_trial_grader_factory,
    queue_trial_grader_factory,
    runner_rpc_trial_grader_factory,
)

pytestmark = pytest.mark.canonical


def _resolved_types() -> dict[str, object]:
    """Return ``TrialGraderContext``'s field types with forward refs resolved.

    ``StructuredLogger`` and ``RuntimeBackend`` are imported under
    ``TYPE_CHECKING`` in the plug-in registry to avoid runtime cycles; feed
    them in via ``localns`` so the hints resolve without pulling every
    logger-side or runtime-side dependency at import time.
    """
    return typing.get_type_hints(
        TrialGraderContext,
        localns={"StructuredLogger": StructuredLogger, "RuntimeBackend": RuntimeBackend},
    )


class _SentinelBackend:
    """Sentinel that fails loud if any out-of-process factory reads it.

    Every attribute access raises — including the ``runner_address``
    ``getattr`` in ``grader_rpc``/``queue`` — so an out-of-process factory
    that even *looks* at ``ctx.runtime_backend`` would blow up. Combined with
    the static ``ast`` check below, this pins the ignore contract both at
    static analysis and at runtime.
    """

    def __getattr__(self, name: str) -> None:
        raise AssertionError(
            f"Out-of-process factory read TrialGraderContext.runtime_backend.{name}; "
            "runtime_backend is an in-process-only escape hatch and MUST be ignored "
            "by any factory whose grader can cross an address boundary. See ADR-0038."
        )


class TestTrialGraderContextShape:
    """The context carries serialisable configuration plus one optional in-process shim."""

    def test_no_field_typed_as_runtime_backend(self) -> None:
        """A required live runtime-backend instance in the context couples the
        grader to the orchestrator's channel; the seam breaks when the grader
        runs elsewhere. Regressing this field's type is a compat-level bug —
        it forces a future PR to re-thread a live object through downstream
        registered graders. ``RuntimeBackend | None`` still passes this test
        because the resolved union type is not identical to the bare
        ``RuntimeBackend`` class."""
        types_by_name = _resolved_types()
        for field in fields(TrialGraderContext):
            resolved = types_by_name[field.name]
            assert resolved is not RuntimeBackend, (
                f"TrialGraderContext.{field.name} resolves to RuntimeBackend; "
                "grader context must not carry a required live runtime-backend "
                "instance. Use ``RuntimeBackend | None`` if this is the in-process "
                "escape hatch."
            )

    def test_runner_address_is_a_string_or_none(self) -> None:
        """The address must be serialisable — a plain ``str`` (or ``None`` when
        the backend has no runner surface), not a wrapper object with a
        runtime binding. Downstream graders that read the field are
        responsible for the None-branch."""
        types_by_name = _resolved_types()
        assert types_by_name["runner_address"] == (str | None)

    def test_runtime_backend_is_optional_and_defaults_to_none(self) -> None:
        """The in-process routing escape hatch must be strictly optional. A
        default of ``None`` keeps every out-of-process factory unable to rely
        on it, and keeps the ADR-0038 wire invariant: the field never crosses
        a serialisation boundary. Widening this to a non-Optional type or
        removing the default is a Blocker."""
        types_by_name = _resolved_types()
        assert types_by_name["runtime_backend"] == (RuntimeBackend | None)

        ctx = TrialGraderContext(runner_address="stub:0", logger=MagicMock())
        assert ctx.runtime_backend is None

    def test_construction_with_unknown_kwarg_fails_loud(self) -> None:
        """Frozen dataclass — a stray kwarg from stale code raises
        ``TypeError`` at construction rather than being ignored."""
        with pytest.raises(TypeError):
            TrialGraderContext(  # type: ignore[call-arg]
                runner_address="stub:0",
                logger=MagicMock(),
                some_field_that_will_never_exist=object(),
            )


class TestOutOfProcessFactoriesIgnoreRuntimeBackend:
    """The in-process escape hatch ``ctx.runtime_backend`` MUST be inert for
    every out-of-process factory. A leak of the field into ``grader_rpc``,
    ``queue``, or ``judge_backed`` is a Blocker — a live backend at
    deployment time it cannot marshal. Both static (``ast.walk``) and runtime
    (sentinel-backend) checks are enforced here."""

    _OUT_OF_PROCESS_FACTORIES = (
        grader_rpc_trial_grader_factory,
        queue_trial_grader_factory,
        judge_backed_trial_grader_factory,
    )

    @pytest.mark.parametrize(
        "factory",
        _OUT_OF_PROCESS_FACTORIES,
        ids=lambda f: f.__name__,
    )
    def test_factory_source_does_not_reference_runtime_backend(
        self, factory: typing.Callable[..., object]
    ) -> None:
        """Every out-of-process factory's source MUST NOT load
        ``ctx.runtime_backend``. Static check via ``ast.walk`` so a future
        PR cannot slip in a ``ctx.runtime_backend`` read that the runtime
        sentinel check might miss (e.g. behind a conditional branch)."""
        source = inspect.getsource(factory)
        tree = ast.parse(source)
        leaks: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "runtime_backend":
                if isinstance(node.value, ast.Name) and node.value.id == "ctx":
                    leaks.append(f"line {node.lineno}: ctx.runtime_backend")
        assert not leaks, (
            f"{factory.__name__} reads ctx.runtime_backend at {leaks}; "
            "the in-process escape hatch must not be read by any factory whose "
            "grader can cross an address boundary. See ADR-0038."
        )

    def test_grader_rpc_factory_ignores_runtime_backend_at_runtime(self) -> None:
        """Sentinel-backend runtime check: any attribute read on
        ``ctx.runtime_backend`` inside the factory would raise
        AssertionError. The factory must build without touching it."""
        ctx = dataclasses.replace(
            TrialGraderContext(runner_address="test-runner:9999", logger=MagicMock()),
            runtime_backend=_SentinelBackend(),  # type: ignore[arg-type]
        )
        grader = grader_rpc_trial_grader_factory(ctx)
        assert isinstance(grader, GraderRPCTrialGrader)

    def test_judge_backed_factory_ignores_runtime_backend_at_runtime(self) -> None:
        """Same sentinel check for the judge-only factory. Judge-backed
        grading is a pure trajectory replay; no runtime state needed and
        certainly no in-process backend read."""
        ctx = dataclasses.replace(
            TrialGraderContext(
                runner_address="stub:0",
                logger=MagicMock(),
                grader_config=GraderConfig(),
            ),
            runtime_backend=_SentinelBackend(),  # type: ignore[arg-type]
        )
        grader = judge_backed_trial_grader_factory(ctx, llm_client=MagicMock())
        assert isinstance(grader, JudgeBackedTrialGrader)

    def test_queue_factory_source_check_is_authoritative(self) -> None:
        """The queue factory spawns worker threads and constructs real
        ``GrpcGraderClient`` instances mid-body, so a sentinel-backend
        runtime check would either need broad monkey-patching (opaque) or
        skip the second half of the factory body (weak). The parametrised
        ``test_factory_source_does_not_reference_runtime_backend`` above
        already covers ``queue_trial_grader_factory`` at the strictly
        stronger static level, and this method's presence documents why
        no separate runtime test exists here."""
        assert queue_trial_grader_factory in self._OUT_OF_PROCESS_FACTORIES


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
