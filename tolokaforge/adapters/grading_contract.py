"""The grading contract every adapter satisfies.

:class:`AdapterGradingContract` is the single addressable name for the readers,
emit seams, and capability flags the harness reaches through to grade a trial
under any adapter. Every adapter shipping today satisfies it structurally via
the defaults on :class:`~tolokaforge.adapters.base.BaseAdapter`; adapters that
grade by their own kind or from their own source override.

The four readers and two emit seams answer the two questions grading asks: what
the block a run reads is (and where it came from), and what the runner needs to
build ``RunnerGradingConfig``. The three capability flags answer the runtime
questions the surrounding stack asks — Docker CLI in the runner, task-file
grading source, adapter-env sync — off data on the class, so callers can read
the flag instead of name-branching on ``AdapterType.NATIVE.value``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, Protocol, runtime_checkable

from tolokaforge.adapters._task_loader import GradingSource
from tolokaforge.core.grading.config_validation import (
    ReplayWorld,
    SeededTablesLayer,
    ToolInventory,
)
from tolokaforge.core.models import TaskConfig


@runtime_checkable
class AdapterGradingContract(Protocol):
    """The six methods + three capability flags every adapter answers grading with.

    Matched structurally by :class:`~tolokaforge.adapters.base.BaseAdapter`'s
    defaults. A concrete adapter overrides only the slots whose default it
    diverges from; every shipped adapter satisfies the contract without
    changing shape.
    """

    requires_docker_cli_in_runner: ClassVar[bool]
    grades_from_task_grading_file: ClassVar[bool]
    syncs_adapter_env_to_state: ClassVar[bool]

    def grading_source(self, task: TaskConfig, task_dir: Path) -> GradingSource: ...

    def grading_tool_inventory(self, task: TaskConfig, task_dir: Path) -> ToolInventory: ...

    def grading_replay_world(self, task: TaskConfig, task_dir: Path) -> ReplayWorld: ...

    def grading_seeded_tables(self, task: TaskConfig, task_dir: Path) -> SeededTablesLayer: ...

    def emit_runner_grading_payload(self, task_id: str) -> dict[str, Any]: ...

    def preferred_grader_kind(self) -> str: ...
