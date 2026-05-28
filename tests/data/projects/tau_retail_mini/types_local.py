"""Local types for tau_retail_mini test project.

These replicate tau_bench types to make the project standalone.
"""

from typing import Any

from pydantic import BaseModel


class Action(BaseModel):
    """Action definition for a task."""

    name: str
    kwargs: dict[str, Any]


class Task(BaseModel):
    """Task definition for Tau-bench format."""

    user_id: str
    actions: list[Action]
    instruction: str
    outputs: list[str]
    task_id: str | None = None
    annotator: str | None = None
