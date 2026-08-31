"""What :class:`BaseAdapter` answers for the grading-contract slots by default.

A minimal :class:`BaseAdapter` subclass that stubs the abstract lifecycle
methods and inherits every grading-contract slot from :class:`BaseAdapter`.
Each test reads one default and locks it: three ``False`` capability flags,
an :attr:`~GradingSourceKind.UNINTERROGABLE` grading source with a non-empty
reason, an empty runner payload, and the ``composite`` grader kind.

The stub adapter registers *nothing* — the tests do not go through the
registry, so no fixture is needed. The adapter's only purpose is to make
:class:`BaseAdapter` instantiable so the instance-method defaults can be
called.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tolokaforge.adapters._task_loader import GradingSource, GradingSourceKind
from tolokaforge.adapters.base import AdapterEnvironment, BaseAdapter
from tolokaforge.core.models import Grade, GradingConfig, TaskConfig, Trajectory
from tolokaforge.tools.registry import Tool

pytestmark = pytest.mark.unit


class _AStubAdapter(BaseAdapter):
    """The minimum :class:`BaseAdapter` subclass a defaults test can construct.

    Every abstract method raises: the tests below never call one, and a raise
    surfaces the miss loudly if a future default starts reading through one.
    """

    def get_task_ids(self) -> list[str]:
        raise NotImplementedError

    def get_task(self, task_id: str) -> TaskConfig:
        raise NotImplementedError

    def get_task_dir(self, task_id: str) -> Path:
        raise NotImplementedError

    def create_environment(self, task_id: str) -> AdapterEnvironment:
        raise NotImplementedError

    def get_tools(self, task_id: str) -> list[Any]:
        raise NotImplementedError

    def get_registry_tools(self, task_id: str, env: AdapterEnvironment) -> list[Tool]:
        raise NotImplementedError

    def get_system_prompt(self, task_id: str) -> str:
        raise NotImplementedError

    def get_grading_config(self, task_id: str) -> GradingConfig:
        raise NotImplementedError

    def reset_environment(self, env: AdapterEnvironment) -> None:
        raise NotImplementedError

    def compute_golden_hash(self, task_id: str, env: AdapterEnvironment) -> str | None:
        raise NotImplementedError

    def to_task_description(self, task_id: str) -> Any:
        raise NotImplementedError

    def grade(  # type: ignore[override]
        self,
        task_id: str,
        trajectory: Trajectory,
        final_state: dict[str, Any],
        env: AdapterEnvironment,
    ) -> Grade:
        raise NotImplementedError


@pytest.fixture
def a_stub_adapter(tmp_path: Path) -> _AStubAdapter:
    """A stub adapter constructed in an isolated dir, for reading the defaults."""
    return _AStubAdapter({"base_dir": str(tmp_path)})


def test_the_three_capability_flags_default_to_false_on_base_adapter(
    a_stub_adapter: _AStubAdapter,
) -> None:
    """The shipped defaults are ``False`` for every capability the flags name."""
    assert a_stub_adapter.requires_docker_cli_in_runner is False
    assert a_stub_adapter.grades_from_task_grading_file is False
    assert a_stub_adapter.syncs_adapter_env_to_state is False


def test_the_grading_source_default_is_uninterrogable_with_a_non_empty_reason(
    a_stub_adapter: _AStubAdapter,
    tmp_path: Path,
) -> None:
    """The default :meth:`grading_source` answers the honest "cannot say" shape.

    The kind is :attr:`~GradingSourceKind.UNINTERROGABLE` (nothing here can
    pronounce on the absence), the path is ``None`` (no file resolved), and
    the reason is non-empty so the ``unchecked`` channel that reads this
    carries a sentence naming the absence.
    """
    task = TaskConfig.model_construct(task_id="none")

    source = a_stub_adapter.grading_source(task, tmp_path)

    assert isinstance(source, GradingSource)
    assert source.kind is GradingSourceKind.UNINTERROGABLE
    assert source.path is None
    assert source.reason


def test_the_emit_runner_grading_payload_default_is_empty(
    a_stub_adapter: _AStubAdapter,
) -> None:
    """The default is an empty dict — the runner falls through to the historical dispatch."""
    assert a_stub_adapter.emit_runner_grading_payload("some_task_id") == {}


def test_the_preferred_grader_kind_default_is_composite(
    a_stub_adapter: _AStubAdapter,
) -> None:
    """The default kind is ``composite`` — the shipped default grader kind."""
    assert a_stub_adapter.preferred_grader_kind() == "composite"
