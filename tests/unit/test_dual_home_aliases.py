"""Unit tests for the dual-home compute/storage.queue aliases.

Six ``OrchestratorConfig`` fields (workers, max_budget_usd,
max_requests_per_second, max_attempt_retries, queue_backend,
queue_postgres_dsn) once lived on the orchestrator alone; the Project
layer moved them to ``ComputeConfig`` / ``StorageConfig``. This suite
pins the alias behaviour: legacy-only lifts + warns, canonical-only
passes through, both-agree warns once, both-disagree fails loud.

Also covers the effective-* accessors that give consumers a single
place to read the resolved value without knowing whether the author
used the legacy field or the canonical field.
"""

from __future__ import annotations

import warnings

import pytest
from pydantic import ValidationError

from tolokaforge.core.models import RunConfig

pytestmark = pytest.mark.unit


def _base(**overrides) -> dict:
    """Minimal valid run-config kwargs; callers layer overrides in."""
    return {
        "models": {"user": {"provider": "openai", "name": "gpt-4o"}},
        "orchestrator": {},
        "evaluation": {"output_dir": "results/x"},
        **overrides,
    }


class TestComputeWorkersAlias:
    def test_legacy_only_lifts_and_warns(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cfg = RunConfig(**_base(orchestrator={"workers": 4}))
        assert cfg.compute is not None
        assert cfg.compute.workers == 4
        assert cfg.effective_workers == 4
        assert any(
            issubclass(w.category, DeprecationWarning) and "orchestrator.workers" in str(w.message)
            for w in caught
        )

    def test_canonical_only_no_warning(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cfg = RunConfig(**_base(compute={"workers": 4}))
        assert cfg.effective_workers == 4
        assert not any(
            issubclass(w.category, DeprecationWarning) and "orchestrator.workers" in str(w.message)
            for w in caught
        )

    def test_both_agree_warns_once(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cfg = RunConfig(
                **_base(orchestrator={"workers": 4}, compute={"workers": 4}),
            )
        assert cfg.effective_workers == 4
        deprecation_hits = [
            w
            for w in caught
            if issubclass(w.category, DeprecationWarning)
            and "orchestrator.workers" in str(w.message)
        ]
        # Warn once — noisy re-warning on load would be worse than a
        # single "you have both, move on" nudge.
        assert len(deprecation_hits) == 1

    def test_both_disagree_fails_loud(self) -> None:
        with pytest.raises(
            (ValueError, ValidationError),
            match="orchestrator.workers=4.*conflicts with compute.workers=8",
        ):
            RunConfig(
                **_base(orchestrator={"workers": 4}, compute={"workers": 8}),
            )

    def test_unset_falls_back_to_orchestrator_default(self) -> None:
        cfg = RunConfig(**_base())
        # OrchestratorConfig.workers default is 8; compute.workers is None.
        assert cfg.effective_workers == 8


class TestQueueBackendAlias:
    def test_legacy_queue_backend_lifts_to_storage_queue(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cfg = RunConfig(
                **_base(
                    orchestrator={
                        "queue_backend": "postgres",
                        "queue_postgres_dsn": "postgres://x",
                    },
                ),
            )
        assert cfg.storage is not None
        assert cfg.storage.queue is not None
        assert cfg.storage.queue.backend == "postgres"
        assert cfg.storage.queue.postgres_dsn == "postgres://x"
        assert cfg.effective_queue_backend == "postgres"
        assert cfg.effective_queue_postgres_dsn == "postgres://x"
        assert (
            sum(
                1
                for w in caught
                if issubclass(w.category, DeprecationWarning)
                and "orchestrator.queue_backend" in str(w.message)
            )
            == 1
        )

    def test_canonical_storage_queue_no_warning(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cfg = RunConfig(
                **_base(
                    storage={
                        "queue": {"backend": "postgres", "postgres_dsn": "postgres://y"},
                    },
                ),
            )
        assert cfg.effective_queue_backend == "postgres"
        assert cfg.effective_queue_postgres_dsn == "postgres://y"
        assert not any(
            issubclass(w.category, DeprecationWarning) and "orchestrator.queue" in str(w.message)
            for w in caught
        )

    def test_both_disagree_fails_loud(self) -> None:
        with pytest.raises(
            (ValueError, ValidationError),
            match="orchestrator.queue_backend.*conflicts with storage.queue.backend",
        ):
            RunConfig(
                **_base(
                    orchestrator={"queue_backend": "sqlite"},
                    storage={"queue": {"backend": "postgres", "postgres_dsn": "x"}},
                ),
            )


class TestMaxBudgetAndThrottleAliases:
    def test_max_budget_usd_lifts(self) -> None:
        cfg = RunConfig(**_base(orchestrator={"max_budget_usd": 20.0}))
        assert cfg.compute is not None
        assert cfg.compute.max_budget_usd == 20.0
        assert cfg.effective_max_budget_usd == 20.0

    def test_max_requests_per_second_lifts(self) -> None:
        cfg = RunConfig(**_base(orchestrator={"max_requests_per_second": 10.0}))
        assert cfg.compute is not None
        assert cfg.compute.max_requests_per_second == 10.0
        assert cfg.effective_max_requests_per_second == 10.0

    def test_max_attempt_retries_lifts(self) -> None:
        cfg = RunConfig(**_base(orchestrator={"max_attempt_retries": 3}))
        assert cfg.compute is not None
        assert cfg.compute.max_attempt_retries == 3
        assert cfg.effective_max_attempt_retries == 3


class TestDeprecatedTaskScopeFields:
    def test_stuck_heuristics_on_orchestrator_warns(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            RunConfig(
                **_base(
                    orchestrator={
                        "stuck_heuristics": {"enabled": True, "max_repeated_tool_calls": 5},
                    },
                ),
            )
        assert any(
            issubclass(w.category, DeprecationWarning)
            and "OrchestratorConfig.stuck_heuristics" in str(w.message)
            for w in caught
        )

    def test_continue_prompt_on_orchestrator_warns(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            RunConfig(**_base(orchestrator={"continue_prompt": "next"}))
        assert any(
            issubclass(w.category, DeprecationWarning)
            and "OrchestratorConfig.continue_prompt" in str(w.message)
            for w in caught
        )

    def test_task_defaults_stuck_heuristics_is_canonical_home(self) -> None:
        # Set it on task_defaults (the canonical home) — no warning fires,
        # since the deprecation only applies to the run-side copy.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            from tolokaforge.core.models import ProjectConfig

            ProjectConfig(
                name="p",
                task_defaults={
                    "stuck_heuristics": {"enabled": True, "max_repeated_tool_calls": 5},
                },
            )
        assert not any(
            issubclass(w.category, DeprecationWarning) and "stuck_heuristics" in str(w.message)
            for w in caught
        )
