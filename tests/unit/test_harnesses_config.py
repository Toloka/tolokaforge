"""Unit tests for the ``harnesses:`` run-config block (issue #1750, slice A1).

The block declares one adapter per entry for a multi-harness run. This suite
pins its parse-time contract:

- a two-entry config parses and dispatches distinct adapters,
- ``extra="forbid"`` rejects a typo'd entry key,
- ``harnesses`` and ``evaluation.harness_adapter`` are mutually exclusive,
- entry names are derived deterministically, de-duplicated, and validated
  unique + filesystem-safe,
- the ``task_packs`` → ``projects`` alias is coerced like ``EvaluationConfig``,
- blank ``projects`` / ``tasks_glob`` inherit the ``evaluation`` defaults.
"""

from __future__ import annotations

import warnings

import pytest

from tolokaforge.core.execution_mode import ExecutionMode
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


class TestTwoEntryParse:
    def test_two_entries_parse_with_distinct_adapters(self) -> None:
        cfg = RunConfig(
            **_base(
                harnesses={
                    "entries": [
                        {"adapter": "native", "task_ids": ["t1"]},
                        {"adapter": "tau", "task_ids": ["t2"]},
                    ]
                },
            )
        )
        assert cfg.harnesses is not None
        assert [e.adapter for e in cfg.harnesses.entries] == ["native", "tau"]
        assert cfg.harnesses.entries[0].task_ids == ["t1"]

    def test_min_length_one_entry(self) -> None:
        with pytest.raises(ValueError):
            RunConfig(**_base(harnesses={"entries": []}))

    def test_per_entry_model_and_mode(self) -> None:
        cfg = RunConfig(
            **_base(
                harnesses={
                    "entries": [
                        {
                            "adapter": "native",
                            "mode": "delegated",
                            "model": {"agent": {"provider": "openai", "name": "gpt-5"}},
                        }
                    ]
                },
            )
        )
        entry = cfg.harnesses.entries[0]
        assert entry.mode is ExecutionMode.DELEGATED
        assert entry.model is not None
        assert entry.model["agent"].name == "gpt-5"


class TestExtraForbid:
    def test_typo_entry_key_rejected(self) -> None:
        with pytest.raises(ValueError):
            RunConfig(
                **_base(
                    harnesses={"entries": [{"adapter": "native", "task_idz": ["t1"]}]},
                )
            )

    def test_typo_block_key_rejected(self) -> None:
        with pytest.raises(ValueError):
            RunConfig(**_base(harnesses={"entriez": [{"adapter": "native"}]}))


class TestMutualExclusion:
    def test_both_harnesses_and_harness_adapter_raise(self) -> None:
        with pytest.raises(ValueError, match="mutually exclusive"):
            RunConfig(
                **_base(
                    evaluation={
                        "output_dir": "results/x",
                        "harness_adapter": {"type": "native"},
                    },
                    harnesses={"entries": [{"adapter": "native"}]},
                )
            )

    def test_harness_adapter_alone_still_parses(self) -> None:
        cfg = RunConfig(
            **_base(
                evaluation={
                    "output_dir": "results/x",
                    "harness_adapter": {"type": "native"},
                },
            )
        )
        assert cfg.harnesses is None
        assert cfg.evaluation.harness_adapter is not None


class TestNameDerivation:
    def test_derivation_stable_and_unique(self) -> None:
        cfg = RunConfig(
            **_base(
                harnesses={
                    "entries": [
                        {"adapter": "native"},
                        {"adapter": "native"},
                        {"adapter": "tau"},
                    ]
                },
            )
        )
        assert [e.name for e in cfg.harnesses.entries] == ["native", "native-2", "tau"]

    def test_mode_slug_in_derived_name(self) -> None:
        cfg = RunConfig(
            **_base(
                harnesses={"entries": [{"adapter": "native", "mode": "delegated"}]},
            )
        )
        assert cfg.harnesses.entries[0].name == "native-delegated"

    def test_explicit_name_reserved_before_derived(self) -> None:
        cfg = RunConfig(
            **_base(
                harnesses={
                    "entries": [
                        {"adapter": "native"},
                        {"adapter": "native", "name": "native"},
                    ]
                },
            )
        )
        # The explicit "native" is reserved first; the unnamed one bumps.
        assert {e.name for e in cfg.harnesses.entries} == {"native", "native-2"}

    def test_duplicate_explicit_names_raise(self) -> None:
        with pytest.raises(ValueError, match="duplicate entry name"):
            RunConfig(
                **_base(
                    harnesses={
                        "entries": [
                            {"adapter": "native", "name": "dup"},
                            {"adapter": "tau", "name": "dup"},
                        ]
                    },
                )
            )

    @pytest.mark.parametrize("bad", ["../escape", "a/b", ".", "..", "has space", ""])
    def test_filesystem_unsafe_name_rejected(self, bad: str) -> None:
        with pytest.raises(ValueError):
            RunConfig(
                **_base(harnesses={"entries": [{"adapter": "native", "name": bad}]}),
            )


class TestTaskPacksAlias:
    def test_task_packs_coerced_to_projects(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cfg = RunConfig(
                **_base(
                    harnesses={"entries": [{"adapter": "native", "task_packs": ["/p/a", "/p/b"]}]},
                )
            )
        entry = cfg.harnesses.entries[0]
        assert entry.projects == ["/p/a", "/p/b"]
        assert entry.task_packs == []
        assert any(issubclass(w.category, DeprecationWarning) for w in caught)


class TestDefaultsFill:
    def test_blank_projects_and_glob_inherit_evaluation(self) -> None:
        cfg = RunConfig(
            **_base(
                evaluation={
                    "output_dir": "results/x",
                    "projects": ["/eval/root"],
                    "tasks_glob": "custom/**/task.yaml",
                },
                harnesses={"entries": [{"adapter": "native"}]},
            )
        )
        entry = cfg.harnesses.entries[0]
        assert entry.projects == ["/eval/root"]
        assert entry.tasks_glob == "custom/**/task.yaml"

    def test_entry_overrides_are_kept(self) -> None:
        cfg = RunConfig(
            **_base(
                evaluation={
                    "output_dir": "results/x",
                    "projects": ["/eval/root"],
                    "tasks_glob": "**/task.yaml",
                },
                harnesses={
                    "entries": [
                        {
                            "adapter": "native",
                            "projects": ["/entry/root"],
                            "tasks_glob": "entry/**/task.yaml",
                        }
                    ]
                },
            )
        )
        entry = cfg.harnesses.entries[0]
        assert entry.projects == ["/entry/root"]
        assert entry.tasks_glob == "entry/**/task.yaml"
