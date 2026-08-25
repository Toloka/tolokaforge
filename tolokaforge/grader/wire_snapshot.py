"""Client-side snapshot builder for the standalone grader wire.

``GraderService.Grade`` is stateless per call: every field the grader-side
composite dispatcher needs to grade the trial rides on the request. The
orchestrator-side ``TrialGrader`` implementations (``GraderRPCTrialGrader``,
``QueueTrialGrader``) call :func:`build_grade_request_fields` to project a
completed trial's :class:`~tolokaforge.core.trial.TrialSpec` into the wire
fields the grader consumes above ``trial_id`` / ``llm_messages_json`` /
``termination_reason``.

The builder reads only from the in-memory :class:`~tolokaforge.core.trial.TrialSpec`
(``spec.task`` — the :class:`TaskDescription` that carries ``initial_state``,
``state_checks.id_fields``, ``initial_state.unstable_fields``, and
``tool_artifacts``) and :attr:`spec.judge_model_config`. It never opens a gRPC
channel and never touches the filesystem — the ``runner_substrate_address`` is
threaded through as a passthrough so the caller has one struct with every
non-trajectory wire field ready to hand to :meth:`GrpcGraderClient.grade`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tolokaforge.core.trial import TrialSpec


@dataclass(frozen=True)
class GradeRequestFields:
    """The four ``GradeRequest`` wire fields the client-side snapshot builder
    populates from a :class:`TrialSpec`.

    Fields correspond to :class:`grader_pb2.GradeRequest` numbers 4-7. The
    trajectory-shaped trio (``trial_id`` / ``llm_messages_json`` /
    ``termination_reason``) rides on the individual ``TrialGrader``
    implementations because it depends on the completed :class:`Trajectory`,
    which the snapshot builder does not receive.
    """

    task_config_json: str
    """The task's :class:`RunnerGradingConfig` as a JSON string —
    ``spec.task.grading.model_dump_json()``. Carries the ``combine_method``,
    ``weights``, ``pass_threshold``, and every sub-component config block
    (``state_checks``, ``transcript_rules``, ``trace_checks``, ``llm_judge``,
    ``custom_checks``) the composite dispatcher branches on."""

    judge_model_config_json: str
    """The judge :class:`ModelConfig` as JSON, or empty when the trial's
    ``spec.judge_model_config`` is unset (the task declares no ``llm_judge``
    component). Empty here is the wire's fail-loud signal that the composite
    dispatcher must not construct a judge — an ``llm_judge``-carrying task with
    an empty ``judge_model_config_json`` is a client bug the grader surfaces."""

    task_description_json: str
    """The whole :class:`TaskDescription` as JSON —
    ``spec.task.model_dump_json()``. One field carries ``initial_state``,
    ``state_checks.id_fields``, ``initial_state.unstable_fields``, and
    ``tool_artifacts`` (the base64-encoded artefact bundle the grader extracts
    for custom-check imports); the composite dispatcher deserialises once and
    derives every downstream input off the parsed model."""

    runner_substrate_address: str
    """gRPC address of the runner's ``SubstrateService``. The grader-side
    dispatcher builds a :class:`LiveRunnerCallbackGradingSubstrate` against
    this address per trial. Passed through verbatim from the ``TrialGrader``'s
    stored value — the snapshot builder never dials it."""


def build_grade_request_fields(
    *,
    spec: TrialSpec,
    runner_substrate_address: str,
) -> GradeRequestFields:
    """Project a completed trial's :class:`TrialSpec` into the wire fields
    :meth:`GraderServiceImpl.Grade` v2 consumes above the trajectory-shaped
    trio.

    Reads only from ``spec.task`` (the :class:`TaskDescription` that carries
    every derived input the grader needs: ``initial_state``,
    ``state_checks.id_fields``, ``initial_state.unstable_fields``, and the
    ``tool_artifacts`` blob) and ``spec.judge_model_config``. Never opens a
    gRPC channel; the ``runner_substrate_address`` is a passthrough. No
    filesystem access — every field is either a ``.model_dump_json()`` on an
    in-memory Pydantic model or a plain-string passthrough.
    """
    judge_model_config_json = (
        spec.judge_model_config.model_dump_json() if spec.judge_model_config is not None else ""
    )
    return GradeRequestFields(
        task_config_json=spec.task.grading.model_dump_json(),
        judge_model_config_json=judge_model_config_json,
        task_description_json=spec.task.model_dump_json(),
        runner_substrate_address=runner_substrate_address,
    )
