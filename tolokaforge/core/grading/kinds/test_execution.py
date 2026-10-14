"""``TestExecutionGraderKind`` — pack-declared verifier over ``substrate.run_test_suite``.

Reads the pack's reference test suite through
:meth:`~tolokaforge.core.grading.substrate.GradingSubstrate.run_test_suite`,
parses the reward file, and produces a :class:`Grade` across three outcome
shapes:

- **Successful run (rc=0 OR rc≠0)** — the reward is parsed from
  ``result.reward_bytes`` (last line, ``float(...)``, clamped ``[0.0, 1.0]``,
  ``(ValueError, IndexError)`` fallback to ``0.0``). The kind does NOT
  gate on ``exit_code``: a script that legitimately exits non-zero but
  wrote a valid reward is scored by the reward.
- **Script exec error** — the substrate's ``script_exec_error`` field is
  populated (subprocess timeout, OSError, ...). The tests never ran, so the
  kind has no verdict to report and raises :class:`GraderKindRefusedError`
  with ``f"test.sh execution failed: {msg}"``. ``Grade.score`` is a
  non-nullable float, so a refusal is the only way to say "nothing was
  measured" here; the trial lands in ``ungradeable`` rather than carrying a
  ``custom_checks: 0.0`` that asserts the suite ran and scored nothing.
- **Tool absent** — the substrate's ``tool_absent`` flag is set (the
  adapter shipped no exec-capable lifecycle tool); the kind raises
  :class:`GraderKindRefusedError` with the substrate's actionable message.

Both refusals are mapped by the runner's dispatcher to
``GradeTrialResponse(success=False, error=exc.reason)``.

Per-task configuration rides ``kind_config`` — validated into
:class:`TestExecutionKindConfig` (``extra="forbid"``). Defaults are
``/tests/test.sh`` (script), ``/logs/verifier/reward.txt`` (reward), 300s
script timeout, 10s reward-cat timeout, 65536-char output truncation.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, Field, model_validator

from tolokaforge.core.grading.kinds._protocol import GraderKindRefusedError
from tolokaforge.core.models.grade import Grade
from tolokaforge.core.models.grade_components import GradeComponents

if TYPE_CHECKING:
    from tolokaforge.core.grading.substrate import GradingSubstrate
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.runner.models import RunnerGradingConfig

__all__ = [
    "ArtifactSpec",
    "CollectSpec",
    "TestExecutionGraderKind",
    "TestExecutionKindConfig",
]


class ArtifactSpec(BaseModel):
    """One path the verifier reads, carried into the verifier container.

    ``source`` is an absolute path; ``exclude`` lists glob patterns pruned from
    the copy (caches, regenerable build output). ``src_service`` names the trial
    service to copy FROM — ``None`` means the agent's own container. A named
    service covers a path that lives in a sidecar (e.g. a database dump a collect
    step wrote inside a postgres sidecar).
    """

    model_config = {"extra": "forbid"}

    source: str
    exclude: list[str] = Field(default_factory=list)
    src_service: str | None = None


class CollectSpec(BaseModel):
    """A command run in a trial service container before artifacts are collected.

    The separate-verifier shape uses it to snapshot a stateful sidecar (a
    database) to a file inside that sidecar, which a matching
    :class:`ArtifactSpec` (``src_service`` = this ``service``) then carries into
    the verifier. A non-zero exit fails grading as ungradeable, never a 0.
    """

    model_config = {"extra": "forbid"}

    service: str
    command: str
    timeout_s: float = Field(default=300.0, gt=0.0)


class TestExecutionKindConfig(BaseModel):
    """Per-task configuration for :class:`TestExecutionGraderKind`.

    Defaults preserve the runner-side pre-move behaviour: verifier at
    ``/tests/test.sh``, reward file at ``/logs/verifier/reward.txt``, 300s
    script timeout, 10s reward-cat timeout, 65536-char output truncation
    for the reasons string — the runner's wire cap on verifier stdout, so
    the grade keeps the whole traceback the runner shipped.

    ``service`` and ``artifacts`` cover the separate-verifier shape: the suite
    runs in ``service``'s container rather than the agent's, and each
    ``artifacts`` path is re-materialised from the agent container into it
    first. Both default to the single-container behaviour — no service, nothing
    to carry across.
    """

    model_config = {"extra": "forbid"}

    script_path: str = "/tests/test.sh"
    reward_path: str = "/logs/verifier/reward.txt"
    timeout_s: float = Field(default=300.0, gt=0.0)
    reward_read_timeout_s: float = Field(default=10.0, gt=0.0)
    output_truncation_chars: int = Field(default=65_536, ge=0)
    service: str | None = None
    artifacts: list[ArtifactSpec] = Field(default_factory=list)
    collect: list[CollectSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _artifacts_and_collect_need_a_service(self) -> TestExecutionKindConfig:
        if (self.artifacts or self.collect) and self.service is None:
            raise ValueError(
                "artifacts and collect hooks belong to the separate-verifier shape, so "
                "a 'service' (the verifier) must be set when either is non-empty."
            )
        return self


class TestExecutionGraderKind:
    """Grade a trial by running the pack's ``test.sh`` and reading the reward."""

    NAME: ClassVar[str] = "test_execution"

    def evaluate(
        self,
        *,
        substrate: GradingSubstrate,
        task_config: RunnerGradingConfig,  # noqa: ARG002
        kind_config: Mapping[str, Any] | None,
        trial_id: str,  # noqa: ARG002
        agent_tools: Mapping[str, Any],  # noqa: ARG002
        logger: StructuredLogger,  # noqa: ARG002
    ) -> Grade | None:
        cfg = TestExecutionKindConfig(**(dict(kind_config) if kind_config else {}))
        for hook in cfg.collect:
            # Snapshot a stateful sidecar to a file inside it (e.g. pg_dumpall),
            # which the matching artifact below carries into the verifier. A
            # non-zero exit means the snapshot never landed — ungradeable, not 0.
            exit_code, output = substrate.exec_in_service(
                service=hook.service,
                command=hook.command,
                timeout_s=hook.timeout_s,
            )
            if exit_code != 0:
                raise GraderKindRefusedError(
                    f"collect command in service {hook.service!r} exited {exit_code}: "
                    f"{output[: cfg.output_truncation_chars]}"
                )
        for artifact in cfg.artifacts:
            # The suite runs in a container the agent never touched, so the files
            # it is scored against are carried across first (empty-then-copy),
            # from the agent (src_service None) or a named sidecar.
            substrate.materialise_path(
                src_service=artifact.src_service,
                dst_service=cfg.service,
                path=artifact.source,
                exclude=artifact.exclude,
            )
        result = substrate.run_test_suite(
            script_path=cfg.script_path,
            reward_path=cfg.reward_path,
            timeout_s=cfg.timeout_s,
            reward_read_timeout_s=cfg.reward_read_timeout_s,
            service=cfg.service,
        )
        if result.tool_absent:
            raise GraderKindRefusedError(result.tool_absent_reason)
        if result.script_exec_error:
            raise GraderKindRefusedError(f"test.sh execution failed: {result.script_exec_error}")
        try:
            reward = float(result.reward_bytes.decode(errors="ignore").strip().split("\n")[-1])
            reward = max(0.0, min(1.0, reward))
        except (ValueError, IndexError):
            reward = 0.0
        return Grade(
            binary_pass=(reward >= 0.5),
            score=reward,
            components=GradeComponents(custom_checks=reward),
            reasons=(
                f"test-execution reward: {reward:.4f}\n\n"
                f"test output (truncated):\n{result.stdout[: cfg.output_truncation_chars]}"
            ),
        )
