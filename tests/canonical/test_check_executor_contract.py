"""Pin the ``CheckExecutor`` Protocol contract (ADR-0012, Pattern A).

Two implementations must satisfy the Protocol: :class:`CheckRunner` (the
in-process production impl — constructed here but its per-trial ``run()``
is exercised by ``tests/unit/grading/test_custom_checks_runner.py`` and
``tests/canonical/test_custom_checks_canon.py`` against a real
``checks.py``) and :class:`InMemoryCheckExecutor` (the deterministic test
fixture; its call-log and failure-knob semantics are pinned here).
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from tolokaforge.core.grading.check_runner import (
    CheckExecutor,
    CheckExecutorCallLog,
    CheckRunner,
    InMemoryCheckExecutor,
)
from tolokaforge.core.grading.checks_interface import (
    CheckContext,
    CheckResult,
    CheckResultSet,
    CheckStatus,
    CustomChecksConfig,
    EnvironmentState,
    Message,
    TaskContext,
    Transcript,
)

pytestmark = pytest.mark.canonical


def _ctx(*, task_id: str = "task-42", final: dict | None = None, msgs: int = 0) -> CheckContext:
    """Synthetic :class:`CheckContext` — deterministic, no file I/O.

    The fixture never inspects the checks module, so the initial/final/
    transcript shapes matter only for the call-log echo — the contract
    test asserts on presence + surface, not per-check behaviour.
    """
    return CheckContext(
        initial_state=EnvironmentState(data={}),
        final_state=EnvironmentState(data=final or {}),
        transcript=Transcript(
            messages=[Message(role="user", content=f"m{i}") for i in range(msgs)]
        ),
        task=TaskContext(task_id=task_id),
    )


def _cfg() -> CustomChecksConfig:
    return CustomChecksConfig(
        enabled=True,
        file="checks.py",
        interface_version="1.0",
        timeout_seconds=7.5,
        fail_on_error=False,
    )


class TestProtocolRuntimeCheck:
    """The Protocol is ``@runtime_checkable``; both implementations satisfy it
    via ``isinstance`` (not just by structural type-hint compatibility)."""

    def test_check_runner_passes_isinstance(self) -> None:
        assert isinstance(CheckRunner(), CheckExecutor)

    def test_in_memory_check_executor_passes_isinstance(self) -> None:
        assert isinstance(InMemoryCheckExecutor(), CheckExecutor)

    def test_random_object_does_not_pass_isinstance(self) -> None:
        class _NotACheckExecutor:
            pass

        assert not isinstance(_NotACheckExecutor(), CheckExecutor)

    def test_object_with_matching_shape_passes_isinstance(self) -> None:
        class _DuckExecutor:
            def run(
                self,
                checks_file: Path,
                task_dir: Path,
                ctx: CheckContext,
                config: CustomChecksConfig,
            ) -> CheckResultSet:  # pragma: no cover — never called
                return CheckResultSet()

        assert isinstance(_DuckExecutor(), CheckExecutor)


class TestRunMethodSignature:
    """All three surfaces expose the same ``run()`` parameter names AND defaults
    (drop-in substitutability — an impl that drops or renames a positional param
    would force call-site edits at every construction site)."""

    def test_run_signatures_match_across_protocol_and_impls(self) -> None:
        def _shape(fn) -> list[tuple[str, object]]:
            return [(p.name, p.default) for p in inspect.signature(fn).parameters.values()]

        protocol_shape = _shape(CheckExecutor.run)
        assert protocol_shape == _shape(CheckRunner.run)
        assert protocol_shape == _shape(InMemoryCheckExecutor.run)

    def test_run_carries_the_four_evidence_params(self) -> None:
        params = list(inspect.signature(CheckExecutor.run).parameters)
        assert params == ["self", "checks_file", "task_dir", "ctx", "config"]


class TestInMemoryExecutorSemantics:
    """The in-memory executor records every ``run()`` and returns a configurable
    :class:`CheckResultSet` — no file I/O, no thread pool, no module load."""

    def test_default_run_returns_empty_result_set_and_records_call(self) -> None:
        executor = InMemoryCheckExecutor()
        result = executor.run(
            checks_file=Path("/does/not/exist/checks.py"),
            task_dir=Path("/does/not/exist"),
            ctx=_ctx(task_id="t-1", final={"agent": {"x": 1}}, msgs=3),
            config=_cfg(),
        )
        assert isinstance(result, CheckResultSet)
        assert result.results == []
        assert result.error is None
        assert executor.call_log.runs == [
            {
                "checks_file": Path("/does/not/exist/checks.py"),
                "task_dir": Path("/does/not/exist"),
                "interface_version": "1.0",
                "timeout_seconds": 7.5,
                "fail_on_error": False,
                "task_id": "t-1",
                "transcript_len": 3,
                "final_state_keys": ("agent",),
            }
        ]

    def test_configured_result_set_is_returned_verbatim(self) -> None:
        preset = CheckResultSet(
            results=[
                CheckResult(
                    check_name="my_check",
                    status=CheckStatus.PASSED,
                    score=0.75,
                    message="fixture verdict",
                )
            ],
            execution_time_ms=1.5,
        )
        executor = InMemoryCheckExecutor(result_set=preset)
        result = executor.run(
            checks_file=Path("/x/checks.py"),
            task_dir=Path("/x"),
            ctx=_ctx(),
            config=_cfg(),
        )
        assert result is preset
        assert result.results[0].check_name == "my_check"
        assert result.results[0].score == 0.75

    def test_return_error_populates_result_set_error_and_records_the_call(self) -> None:
        executor = InMemoryCheckExecutor(return_error="module not found")
        result = executor.run(
            checks_file=Path("/x/checks.py"),
            task_dir=Path("/x"),
            ctx=_ctx(),
            config=_cfg(),
        )
        assert result.error == "module not found"
        assert result.results == []
        assert len(executor.call_log.runs) == 1

    def test_raise_on_run_propagates_and_records_the_call_first(self) -> None:
        boom = RuntimeError("simulated executor crash")
        executor = InMemoryCheckExecutor(raise_on_run=boom)
        with pytest.raises(RuntimeError, match="simulated executor crash"):
            executor.run(
                checks_file=Path("/x/checks.py"),
                task_dir=Path("/x"),
                ctx=_ctx(),
                config=_cfg(),
            )
        # Call is recorded before the raise so a test can assert what the
        # caller submitted even on a crash.
        assert len(executor.call_log.runs) == 1

    def test_call_log_accumulates_across_multiple_runs(self) -> None:
        executor = InMemoryCheckExecutor()
        for i in range(3):
            executor.run(
                checks_file=Path(f"/x/{i}/checks.py"),
                task_dir=Path(f"/x/{i}"),
                ctx=_ctx(task_id=f"t-{i}"),
                config=_cfg(),
            )
        assert [r["task_id"] for r in executor.call_log.runs] == ["t-0", "t-1", "t-2"]

    def test_fresh_executor_has_empty_call_log(self) -> None:
        assert InMemoryCheckExecutor().call_log == CheckExecutorCallLog()

    def test_mutually_exclusive_failure_knobs_reject_at_construction(self) -> None:
        with pytest.raises(ValueError):
            InMemoryCheckExecutor(raise_on_run=RuntimeError("x"), return_error="y")
        with pytest.raises(ValueError):
            InMemoryCheckExecutor(raise_on_run=RuntimeError("x"), result_set=CheckResultSet())
        with pytest.raises(ValueError):
            InMemoryCheckExecutor(return_error="y", result_set=CheckResultSet())


class TestProductionImplConstructs:
    """The published :class:`CheckRunner` constructor accepts zero required
    arguments — callers that construct it as ``CheckRunner()`` must keep
    working, and its defaults land on the in-process ThreadPoolExecutor."""

    def test_check_runner_no_arg_construct_still_works(self) -> None:
        runner = CheckRunner()
        assert runner.executor_type == "thread"
        assert runner.max_workers == 1


class TestBothImplsExported:
    """``tolokaforge.core.grading`` re-exports the seam surface so downstream
    callers reach it via the package (``from tolokaforge.core.grading import
    CheckExecutor, InMemoryCheckExecutor, CheckRunner``)."""

    def test_seam_symbols_are_exported_from_package(self) -> None:
        from tolokaforge.core import grading as grading_pkg

        assert grading_pkg.CheckExecutor is CheckExecutor
        assert grading_pkg.CheckRunner is CheckRunner
        assert grading_pkg.InMemoryCheckExecutor is InMemoryCheckExecutor
        assert grading_pkg.CheckExecutorCallLog is CheckExecutorCallLog

    def test_seam_symbols_are_listed_in_dunder_all(self) -> None:
        from tolokaforge.core import grading as grading_pkg

        assert "CheckExecutor" in grading_pkg.__all__
        assert "CheckExecutorCallLog" in grading_pkg.__all__
        assert "CheckRunner" in grading_pkg.__all__
        assert "InMemoryCheckExecutor" in grading_pkg.__all__
