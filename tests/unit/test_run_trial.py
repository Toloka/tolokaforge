"""Error contract, ``auto`` selection, and the no-disk seam of ``run_trial``.

Hermetic: the single-task adapter and the three registry loaders are stubbed /
patched at the ``run_trial``-module binding, so no fixture pack, Docker, or LLM
key is needed. Composition equivalence against the orchestrator lives in the
canonical tier (``tests/canonical/test_run_trial_composition.py``).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

import tolokaforge.core.run_trial as run_trial_mod
from tests.canonical._factories import make_task_config, make_task_description
from tolokaforge.core.conductor import ConductorContext, InMemoryConductor
from tolokaforge.core.output.artifacts import FileArtifactWriter, InMemoryArtifactWriter
from tolokaforge.core.plugin_registry import UnknownImplementationError
from tolokaforge.core.run_trial import run_trial
from tolokaforge.core.runtime import InMemoryRuntimeBackend

pytestmark = pytest.mark.unit

_AGENT = {"provider": "openai", "name": "gpt-4"}
_USER = {"provider": "openrouter", "name": "anthropic/claude-sonnet-4.6"}


class _FakeAdapter:
    """Stands in for the single-task native adapter; returns a fixed desc."""

    def __init__(self, task_desc: Any) -> None:
        self._task_desc = task_desc

    def to_task_description(self, task_id: str) -> Any:
        return self._task_desc


def _patch_adapter(monkeypatch: pytest.MonkeyPatch, task_desc: Any) -> None:
    monkeypatch.setattr(
        run_trial_mod,
        "_build_single_task_adapter",
        lambda task: _FakeAdapter(task_desc),
    )


# ---------------------------------------------------------------------------
# (b) Model resolution — ValidationError before any registry / backend work
# ---------------------------------------------------------------------------


class TestModelValidation:
    def test_malformed_agent_value_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            run_trial(
                task=make_task_config(),
                models={"agent": {"provider": "openai", "name": "gpt-4", "temperature": "nope"}},
            )

    def test_missing_agent_key_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            run_trial(task=make_task_config(), models={"user": _AGENT})

    def test_unexpected_role_key_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            run_trial(task=make_task_config(), models={"agent": _AGENT, "bogus": _AGENT})


# ---------------------------------------------------------------------------
# (a) Registry resolution — unknown name → UnknownImplementationError
# ---------------------------------------------------------------------------


class TestUnknownImplementationErrors:
    def test_unknown_runtime_lists_known_names(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_adapter(monkeypatch, make_task_description())
        with pytest.raises(UnknownImplementationError) as exc:
            run_trial(task=make_task_config(), models={"agent": _AGENT}, runtime="bogus")
        assert "shared" in str(exc.value)
        assert "bogus" in str(exc.value)

    def test_unknown_grader_lists_known_names(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_adapter(monkeypatch, make_task_description())
        with pytest.raises(UnknownImplementationError) as exc:
            run_trial(
                task=make_task_config(),
                models={"agent": _AGENT},
                runtime="in_memory",
                grader="bogus",
            )
        assert "runner_rpc" in str(exc.value)

    def test_unknown_conductor_lists_known_names(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_adapter(monkeypatch, make_task_description())
        with pytest.raises(UnknownImplementationError) as exc:
            run_trial(
                task=make_task_config(),
                models={"agent": _AGENT},
                runtime="in_memory",
                conductor="bogus",
            )
        assert "in_process" in str(exc.value)


# ---------------------------------------------------------------------------
# (c) auto selection — task-driven per-trial signal for the run_trial subprocess seam
# ---------------------------------------------------------------------------


class _StopBeforeBackend(Exception):
    pass


class TestAutoRuntimeSelection:
    def _record_runtime_name(
        self, monkeypatch: pytest.MonkeyPatch, *, requires_per_trial: bool
    ) -> dict[str, str]:
        recorded: dict[str, str] = {}
        manifest = SimpleNamespace(requires_per_trial=requires_per_trial)
        _patch_adapter(monkeypatch, SimpleNamespace(environment_manifest=manifest))

        def fake_loader(name: str) -> Any:
            recorded["name"] = name
            raise _StopBeforeBackend

        monkeypatch.setattr(run_trial_mod, "load_runtime_backend", fake_loader)
        with pytest.raises(_StopBeforeBackend):
            run_trial(task=make_task_config(), models={"agent": _AGENT}, runtime="auto")
        return recorded

    def test_auto_picks_per_trial_when_manifest_requires_isolation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded = self._record_runtime_name(monkeypatch, requires_per_trial=True)
        assert recorded["name"] == "per_trial"

    def test_auto_picks_shared_otherwise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded = self._record_runtime_name(monkeypatch, requires_per_trial=False)
        assert recorded["name"] == "shared"


# ---------------------------------------------------------------------------
# (d) output_dir seam — InMemoryArtifactWriter and no disk when None
# ---------------------------------------------------------------------------


class TestOutputDirSeam:
    def _capture_context(
        self, monkeypatch: pytest.MonkeyPatch, *, output_dir: Path | None
    ) -> ConductorContext:
        _patch_adapter(monkeypatch, make_task_description(task_id="task-1"))
        captured: list[ConductorContext] = []

        def capture_conductor(name: str) -> Any:
            def factory(ctx: ConductorContext) -> InMemoryConductor:
                captured.append(ctx)
                return InMemoryConductor()

            return factory

        monkeypatch.setattr(run_trial_mod, "load_conductor", capture_conductor)
        result = run_trial(
            task=make_task_config(task_id="task-1"),
            models={"agent": _AGENT, "user": _USER},
            runtime="in_memory",
            output_dir=output_dir,
        )
        assert result.trial_id == "task-1:0"
        (ctx,) = captured
        return ctx

    def test_output_dir_none_uses_in_memory_writer_and_writes_no_disk(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        ctx = self._capture_context(monkeypatch, output_dir=None)
        assert isinstance(ctx.artifact_writer, InMemoryArtifactWriter)
        # run_trial anchors its in-memory output on a sentinel path but must
        # never materialise it on disk when output_dir is None.
        assert ctx.output_dir == Path("run_trial")
        assert not (tmp_path / "run_trial").exists()

    def test_output_dir_set_uses_file_writer_anchored_there(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        out = tmp_path / "results"
        ctx = self._capture_context(monkeypatch, output_dir=out)
        assert isinstance(ctx.artifact_writer, FileArtifactWriter)
        assert ctx.output_dir == out


# ---------------------------------------------------------------------------
# (e) Backend lifecycle — connect() must be paired with close() on every exit
# ---------------------------------------------------------------------------


class _RaisingConductor:
    """Conductor whose ``run`` raises, exercising the ``finally`` path."""

    def run(self, spec: Any, task_config: Any) -> Any:
        raise RuntimeError("conductor blew up")


class TestBackendLifecycle:
    def _capture_backend(self, monkeypatch: pytest.MonkeyPatch) -> list[InMemoryRuntimeBackend]:
        captured: list[InMemoryRuntimeBackend] = []
        original_loader = run_trial_mod.load_runtime_backend

        def wrap_loader(name: str) -> Any:
            real_factory = original_loader(name)

            def capturing_factory(ctx: Any) -> Any:
                backend = real_factory(ctx)
                captured.append(backend)
                return backend

            return capturing_factory

        monkeypatch.setattr(run_trial_mod, "load_runtime_backend", wrap_loader)
        return captured

    def _install_conductor(self, monkeypatch: pytest.MonkeyPatch, conductor_impl: Any) -> None:
        monkeypatch.setattr(
            run_trial_mod,
            "load_conductor",
            lambda name: (lambda ctx: conductor_impl),
        )

    def test_close_called_after_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_adapter(monkeypatch, make_task_description(task_id="task-1"))
        captured = self._capture_backend(monkeypatch)
        self._install_conductor(monkeypatch, InMemoryConductor())

        run_trial(
            task=make_task_config(task_id="task-1"),
            models={"agent": _AGENT, "user": _USER},
            runtime="in_memory",
        )

        (backend,) = captured
        assert backend.call_log.close_calls == 1

    def test_close_called_when_conductor_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_adapter(monkeypatch, make_task_description(task_id="task-1"))
        captured = self._capture_backend(monkeypatch)
        self._install_conductor(monkeypatch, _RaisingConductor())

        with pytest.raises(RuntimeError, match="conductor blew up"):
            run_trial(
                task=make_task_config(task_id="task-1"),
                models={"agent": _AGENT, "user": _USER},
                runtime="in_memory",
            )

        (backend,) = captured
        assert backend.call_log.close_calls == 1
