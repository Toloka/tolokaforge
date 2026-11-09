"""``task_dir_of`` reaches the substrate every runtime backend hands its composer (ADR-0072).

The composer mounts the directory ``pack:`` references resolve against only if the backend
threaded the lookup from its build context; a hop that drops it turns every by-reference
trial into a provision refusal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge.core.per_trial_runtime import per_trial_runtime_backend_factory
from tolokaforge.core.plugin_registry import RuntimeBackendBuildContext
from tolokaforge.core.shared_stack_runtime import shared_runtime_backend_factory

pytestmark = pytest.mark.unit


def _locate(task_id: str) -> Path:
    return Path("/packs") / task_id


def _ctx(**fields: object) -> RuntimeBackendBuildContext:
    return RuntimeBackendBuildContext(
        runner_address="runner:50051",
        env_manifest=None,
        run_id="run-1",
        seeds={},
        log_capture=None,
        task_dir_of=_locate,
        **fields,  # type: ignore[arg-type]
    )


def test_the_shared_backend_in_per_trial_mode_threads_it() -> None:
    backend = shared_runtime_backend_factory(_ctx(per_trial_mode=True))

    assert backend._run_substrate_or_empty().task_dir_of is _locate


def test_the_per_trial_backend_threads_it() -> None:
    backend = per_trial_runtime_backend_factory(_ctx())

    assert backend._delegate._run_substrate_or_empty().task_dir_of is _locate


def test_the_run_context_carries_it_to_the_composer(monkeypatch: pytest.MonkeyPatch) -> None:
    """``connect`` hands it to ``materialise_run`` on a plan with run-scope stacks."""
    from types import SimpleNamespace

    from tolokaforge.core.shared_stack_runtime import SharedStackRuntimeBackend

    seen: list[object] = []

    class _Composer:
        def materialise_run(self, plan: object, ctx: object) -> object:
            seen.append(ctx)
            return SimpleNamespace(runner_client=None)

    backend = SharedStackRuntimeBackend(
        env_manifest=SimpleNamespace(stacks=()),  # type: ignore[arg-type]
        composer=_Composer(),  # type: ignore[arg-type]
        task_dir_of=_locate,
    )
    backend.connect()

    (ctx,) = seen
    assert ctx.task_dir_of is _locate  # type: ignore[attr-defined]
