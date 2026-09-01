"""Reusable pytest class pinning every adapter to the grading contract.

An adapter subclasses :class:`AdapterGradingContractSuite`, provides an
``adapter`` fixture and a ``task_and_dir`` fixture, and (optionally)
overrides ``expected_*`` class attributes for the three capability flags and
the preferred grader kind whose adapter declaration disagrees with the
shipped defaults (all three flags default ``False``; preferred kind defaults
``"composite"``). The subclass then collects the 11 test methods below,
pinning:

- The six methods :class:`~tolokaforge.adapters.grading_contract.AdapterGradingContract`
  declares (four readers + emit + preferred-kind), each returning the
  declared type against a real task.
- The three capability flags matching the subclass's declared expectation
  (``requires_docker_cli_in_runner``, ``grades_from_task_grading_file``,
  ``syncs_adapter_env_to_state``).
- ``emit_runner_grading_payload(task_id)`` returning a ``dict``; when
  non-empty, constructing a valid
  :class:`~tolokaforge.runner.models.RunnerGradingConfig` (empty payloads
  short-circuit via ``pytest.skip`` so the branch reads honestly rather than
  passing on an unreached body).
- ``preferred_grader_kind()`` resolving through
  :func:`~tolokaforge.core.plugin_registry.load_grading_method` — that is,
  the adapter's declared kind is a registered
  ``tolokaforge.grading_methods`` entry.

The base class name has no ``Test`` prefix so pytest does not collect it
directly; subclasses use ``Test<Adapter>GradingContract``.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from tolokaforge.adapters._task_loader import GradingSource, GradingSourceKind
from tolokaforge.adapters.base import BaseAdapter
from tolokaforge.adapters.grading_contract import AdapterGradingContract
from tolokaforge.core.grading.config_validation import (
    ReplayWorld,
    SeededTablesLayer,
    ToolInventory,
)
from tolokaforge.core.models import TaskConfig
from tolokaforge.core.plugin_registry import load_grading_method
from tolokaforge.runner.models import RunnerGradingConfig


class AdapterGradingContractSuite:
    """Subclass to lock one adapter against the grading contract.

    Override the two fixtures ``adapter`` and ``task_and_dir``; override
    the four ``expected_*`` class attributes only when the adapter's
    declaration diverges from the shipped default.
    """

    expected_requires_docker_cli_in_runner: ClassVar[bool] = False
    expected_grades_from_task_grading_file: ClassVar[bool] = False
    expected_syncs_adapter_env_to_state: ClassVar[bool] = False
    expected_preferred_grader_kind: ClassVar[str] = "composite"

    @pytest.fixture
    def adapter(self) -> BaseAdapter:
        raise NotImplementedError(
            "subclasses of AdapterGradingContractSuite must override the "
            "`adapter` fixture to return a constructed adapter instance"
        )

    @pytest.fixture
    def task_and_dir(self) -> tuple[TaskConfig, Path]:
        raise NotImplementedError(
            "subclasses of AdapterGradingContractSuite must override the "
            "`task_and_dir` fixture to return a (TaskConfig, Path) pair the "
            "adapter can resolve"
        )

    def test_adapter_satisfies_the_grading_contract_protocol(self, adapter: BaseAdapter) -> None:
        assert isinstance(adapter, AdapterGradingContract)

    def test_requires_docker_cli_in_runner_matches_declared_expectation(
        self, adapter: BaseAdapter
    ) -> None:
        assert adapter.requires_docker_cli_in_runner is self.expected_requires_docker_cli_in_runner

    def test_grades_from_task_grading_file_matches_declared_expectation(
        self, adapter: BaseAdapter
    ) -> None:
        assert adapter.grades_from_task_grading_file is self.expected_grades_from_task_grading_file

    def test_syncs_adapter_env_to_state_matches_declared_expectation(
        self, adapter: BaseAdapter
    ) -> None:
        assert adapter.syncs_adapter_env_to_state is self.expected_syncs_adapter_env_to_state

    def test_grading_source_returns_a_grading_source(
        self, adapter: BaseAdapter, task_and_dir: tuple[TaskConfig, Path]
    ) -> None:
        task, task_dir = task_and_dir
        source = adapter.grading_source(task, task_dir)
        assert isinstance(source, GradingSource)
        if source.kind in (GradingSourceKind.WITHHELD, GradingSourceKind.UNINTERROGABLE):
            assert source.reason, (
                f"{type(adapter).__name__}.grading_source returned "
                f"{source.kind.value} with an empty reason: the reason field "
                "carries the sentence the absence is reported by"
            )

    def test_grading_tool_inventory_returns_a_tool_inventory(
        self, adapter: BaseAdapter, task_and_dir: tuple[TaskConfig, Path]
    ) -> None:
        task, task_dir = task_and_dir
        assert isinstance(adapter.grading_tool_inventory(task, task_dir), ToolInventory)

    def test_grading_replay_world_returns_a_replay_world(
        self, adapter: BaseAdapter, task_and_dir: tuple[TaskConfig, Path]
    ) -> None:
        task, task_dir = task_and_dir
        assert isinstance(adapter.grading_replay_world(task, task_dir), ReplayWorld)

    def test_grading_seeded_tables_returns_a_seeded_tables_layer(
        self, adapter: BaseAdapter, task_and_dir: tuple[TaskConfig, Path]
    ) -> None:
        task, task_dir = task_and_dir
        assert isinstance(adapter.grading_seeded_tables(task, task_dir), SeededTablesLayer)

    def test_emit_runner_grading_payload_returns_a_dict(
        self, adapter: BaseAdapter, task_and_dir: tuple[TaskConfig, Path]
    ) -> None:
        task, _ = task_and_dir
        payload = adapter.emit_runner_grading_payload(task.task_id)
        assert isinstance(payload, dict)

    def test_emit_runner_grading_payload_constructs_a_valid_runner_grading_config(
        self, adapter: BaseAdapter, task_and_dir: tuple[TaskConfig, Path]
    ) -> None:
        task, _ = task_and_dir
        payload = adapter.emit_runner_grading_payload(task.task_id)
        if not payload:
            pytest.skip(
                "adapter returns the empty-payload default; runner falls "
                "through to the historical dispatch"
            )
        RunnerGradingConfig(**payload)
        method_name = payload.get("grading_method")
        if method_name is not None:
            load_grading_method(method_name)

    def test_preferred_grader_kind_resolves_in_the_grading_methods_registry(
        self, adapter: BaseAdapter
    ) -> None:
        kind = adapter.preferred_grader_kind()
        assert kind == self.expected_preferred_grader_kind
        load_grading_method(kind)
