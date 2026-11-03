"""``JudgeTrialOptions`` — the per-trial options every :class:`JudgeKind` receives.

The judge's per-trial customization travels to :meth:`JudgeKind.evaluate` as one
frozen object, the judge analogue of
:class:`~tolokaforge.core.loop.AgentLoopContext` and
:class:`~tolokaforge.core.actors.user_simulator.UserSimulatorContext`: the
Protocol's signature carries ``options`` and does not change when a knob is added.

``options.customization`` is the trial's effective
:class:`~tolokaforge.runner.models.JudgeCustomization`, after every layer the
caller resolved (:func:`~tolokaforge.core.project_loader.layer_judge_customization`).
:func:`resolve_judge_trial_options` is the one place it becomes options.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tolokaforge.core.tool_effects import ToolEffects
from tolokaforge.runner.models import EvidenceInput, JudgeCustomization

if TYPE_CHECKING:
    from tolokaforge.runner.models import ToolSchema

__all__ = [
    "JudgeTrialOptions",
    "resolve_judge_trial_options",
]


@dataclass(frozen=True)
class JudgeTrialOptions:
    """How a judge kind grades one trial, beyond the evidence it reads.

    * ``customization`` — the trial's effective judge customization: its system
      prompt and the parts of its ``evidence``.
    * ``tool_effects`` — the agent tools' ``mutates_state`` declarations, from
      which the ``state_changing_calls`` part tells the agent's state-changing calls
      from its reads. Built only while that part is on; empty otherwise.

    A kind honours both by building
    :class:`~tolokaforge.core.grading.judge.LLMJudge` from them, and a kind wrapping
    another passes the object on unchanged.
    """

    customization: JudgeCustomization = field(default_factory=JudgeCustomization)
    tool_effects: ToolEffects = field(default_factory=ToolEffects.empty)


def resolve_judge_trial_options(
    customization: JudgeCustomization | None,
    *,
    agent_tools: Iterable[ToolSchema],
) -> JudgeTrialOptions:
    """The options a trial is judged with, from its effective customization.

    ``None`` is a task with no ``customization`` block. ``agent_tools`` are the
    trial's agent tool schemas, whose ``mutates_state`` declarations become
    ``tool_effects`` while ``evidence.needs(TOOL_EFFECTS)``. Required: a caller that
    forgot them would have the judge list every read as a state change.
    """
    effective = customization if customization is not None else JudgeCustomization()
    return JudgeTrialOptions(
        customization=effective,
        tool_effects=(
            ToolEffects.from_schemas(agent_tools)
            if effective.evidence.needs(EvidenceInput.TOOL_EFFECTS)
            else ToolEffects.empty()
        ),
    )
