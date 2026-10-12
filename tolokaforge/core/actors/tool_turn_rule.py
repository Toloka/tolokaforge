"""How a trial runs its user simulator's tool calls.

The runner reads it; the conductor builds it once per trial from the resolved
``actors.user``. Orchestrator-only, like :mod:`tolokaforge.core.actors.user_stop`:
the runner container never runs a simulator. What a transcript's steps look like,
and who reads them, is :mod:`tolokaforge.core.actors.tool_turns`.
"""

from __future__ import annotations

from dataclasses import dataclass

from tolokaforge.core.models.task_config import (
    DEFAULT_MAX_USER_TOOL_STEPS,
    UserSimulatorConfig,
    UserToolTurns,
)

__all__ = ["UserToolTurnRule"]


@dataclass(frozen=True)
class UserToolTurnRule:
    """The resolved actor's ``tool_turns`` and ``max_tool_steps``."""

    mode: UserToolTurns = "shared"
    max_steps: int = DEFAULT_MAX_USER_TOOL_STEPS

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ValueError(f"max_steps is {self.max_steps}; a user turn needs at least one.")

    @property
    def isolated(self) -> bool:
        return self.mode == "isolated"

    @classmethod
    def from_config(cls, config: UserSimulatorConfig) -> UserToolTurnRule:
        return cls(mode=config.tool_turns, max_steps=config.max_tool_steps)
