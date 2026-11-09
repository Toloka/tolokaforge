"""Rules a trial's inputs satisfy before it spends tokens, enforced on every entry point.

The same trial runs three ways: inside ``tolokaforge run`` (the orchestrator),
dispatched to a node, or from :class:`~tolokaforge.core.trial.TrialNodeInputs`
an external orchestrator built with ``plan_trials`` (ADR-0063, ADR-0064). A rule
written as an ``Orchestrator`` method reaches only the first. The rules here are
written on the trial's own contract, so the orchestrator applies them to its
whole run up front and :func:`~tolokaforge.core.trial_node.run_trial_from_inputs`
applies them to the one trial it was handed (ADR-0072).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tolokaforge.core.models import GraderConfig
    from tolokaforge.core.trial import TrialNodeInputs
    from tolokaforge.runner.models import TaskDescription

__all__ = [
    "GRADERS_OFF_THE_RUNNER",
    "delivers_by_reference",
    "refuse_a_mount_no_trial_runner_can_take",
    "refuse_references_the_grader_cannot_read",
    "validate_trial_inputs",
]


GRADERS_OFF_THE_RUNNER = frozenset({"grader_rpc", "queue"})
"""Graders that do not run on the trial's own runner."""


def delivers_by_reference(description: TaskDescription) -> bool:
    """Whether any of the trial's data moves by reference instead of on the wire.

    Its files, as ``pack:`` references (ADR-0068), or its final state, written
    to a directory the engine mounts into the runner (ADR-0069).
    """
    return description.tool_artifact_refs is not None or description.trial_data_delivery == "mount"


def refuse_references_the_grader_cannot_read(
    grader: GraderConfig | None,
    grader_name: str | None,
    referencing_task_ids: Iterable[str],
) -> None:
    """Refuse a grader that cannot read the trial data these tasks deliver by reference.

    A task's ``pack:`` references (ADR-0068, ADR-0072) resolve only on the
    trial's runner, where the engine mounted the task's directory, and a final
    state delivered by reference (ADR-0069) is a file the engine never parses.
    The ``grader_rpc`` and ``queue`` graders run elsewhere and read the final
    state whole over the wire, and a snapshot grade bundle carries inline
    artefacts and the whole final state, so each would rebuild the copies the
    delivery removes or refuse the trial at grade time, after its tokens were
    spent. Every offending task is named in one error.

    Args:
        grader: The run's grader block, if any.
        grader_name: The grader the trials use: the block's name, else the
            adapter's default.
        referencing_task_ids: The tasks whose descriptions carry references.

    Raises:
        ValueError: the grader cannot read references and a task carries them.
    """
    remote_grader = grader_name in GRADERS_OFF_THE_RUNNER
    snapshot = grader is not None and grader.snapshot is not None and grader.snapshot.enabled
    if not (remote_grader or snapshot):
        return
    offending = sorted(referencing_task_ids)
    if not offending:
        return
    how = f"grader {grader_name!r}" if remote_grader else "grader.snapshot.enabled=true"
    raise ValueError(
        f"{how} cannot read trial data delivered by reference "
        f"(compute.trial_data_delivery: mount, ADR-0068/0069), which tasks {offending} use: "
        "only the trial's runner has the task directory and the final state. Grade with the "
        "runner_rpc grader without snapshot mode, or set compute.trial_data_delivery: inline."
    )


def refuse_a_mount_no_trial_runner_can_take(description: TaskDescription) -> None:
    """Refuse ``trial_data_delivery: mount`` on a description whose runner the engine does not make.

    The engine mounts per-trial directories only into a runner a ``trial``-scope stack
    of the task's own manifest owns. A description built by hand, or for another
    environment, could ask for ``mount`` without one; it would provision, spend the
    trial's tokens and lose its final state at capture.

    Raises:
        ValueError: the description asks for ``mount`` and no trial-scope stack owns
            its runner.
    """
    from tolokaforge.runner.models import runner_outside_trial_scope

    if description.trial_data_delivery != "mount":
        return
    manifest = description.environment_manifest
    found = (
        runner_outside_trial_scope(manifest.stacks)
        if manifest is not None
        else "no environment manifest"
    )
    if found is not None:
        raise ValueError(
            f"task {description.task_id!r} delivers its data by reference (trial_data_delivery: "
            f"mount), which needs a trial-scope stack of its own manifest to own the runner; the "
            f"runner here: {found}"
        )


def validate_trial_inputs(inputs: TrialNodeInputs, *, default_grader_name: str | None) -> None:
    """Apply the trial-contract rules to one trial's inputs, before it runs.

    Args:
        inputs: The trial, as a node receives it.
        default_grader_name: The grader the task's adapter names when the
            inputs carry no grader block.

    Raises:
        ValueError: the inputs break a rule; the message names it.
    """
    grader = inputs.grader_config
    grader_name = grader.name if grader is not None and grader.name else default_grader_name
    task = inputs.trial_spec.task
    refuse_a_mount_no_trial_runner_can_take(task)
    refuse_references_the_grader_cannot_read(
        grader,
        grader_name,
        [task.task_id] if delivers_by_reference(task) else [],
    )
