"""``TestExecutionGraderKind`` — pack-declared verifier over ``substrate.run_test_suite``.

Reads the pack's reference test suite through
:meth:`~tolokaforge.core.grading.substrate.GradingSubstrate.run_test_suite`,
parses the reward file, and produces a :class:`Grade` matching the
runner-side pre-move behaviour byte-for-byte across every outcome:

- **Successful run (rc=0 OR rc≠0)** — the reward is parsed from
  ``result.reward_bytes`` (last line, ``float(...)``, clamped ``[0.0, 1.0]``,
  ``(ValueError, IndexError)`` fallback to ``0.0``). The kind does NOT
  gate on ``exit_code``: a script that legitimately exits non-zero but
  wrote a valid reward is scored by the reward.
- **Script exec error** — the substrate's ``script_exec_error`` field is
  populated (subprocess timeout, OSError, ...); the kind returns
  ``Grade(score=0.0, reasons=f"test.sh execution failed: {msg}")``, the
  pre-move at :meth:`RunnerServiceImpl._grade_via_test_execution` shape.
- **Tool absent** — the substrate's ``tool_absent`` flag is set (the
  adapter shipped no exec-capable lifecycle tool); the kind raises
  :class:`GraderKindRefusedError` with the substrate's actionable message.
  The dispatcher maps this to ``GradeTrialResponse(success=False,
  error=exc.reason)``, byte-identical to pre-move.

Per-task configuration rides ``kind_config`` — validated into
:class:`TestExecutionKindConfig` (``extra="forbid"``). Defaults preserve
the pre-move paths (``/tests/test.sh`` script, ``/logs/verifier/reward.txt``
reward, 300s script timeout, 10s reward-cat timeout, 2000-char output
truncation).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, Field

from tolokaforge.core.grading.kinds._protocol import GraderKindRefusedError
from tolokaforge.core.models.grade import Grade
from tolokaforge.core.models.grade_components import GradeComponents

if TYPE_CHECKING:
    from tolokaforge.core.grading.substrate import GradingSubstrate
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.runner.models import RunnerGradingConfig

__all__ = [
    "TestExecutionGraderKind",
    "TestExecutionKindConfig",
]


class TestExecutionKindConfig(BaseModel):
    """Per-task configuration for :class:`TestExecutionGraderKind`.

    Defaults preserve the runner-side pre-move behaviour: verifier at
    ``/tests/test.sh``, reward file at ``/logs/verifier/reward.txt``, 300s
    script timeout, 10s reward-cat timeout, 2000-char output truncation
    for the reasons string.
    """

    model_config = {"extra": "forbid"}

    script_path: str = "/tests/test.sh"
    reward_path: str = "/logs/verifier/reward.txt"
    timeout_s: float = Field(default=300.0, gt=0.0)
    reward_read_timeout_s: float = Field(default=10.0, gt=0.0)
    output_truncation_chars: int = Field(default=2000, ge=0)


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
        result = substrate.run_test_suite(
            script_path=cfg.script_path,
            reward_path=cfg.reward_path,
            timeout_s=cfg.timeout_s,
            reward_read_timeout_s=cfg.reward_read_timeout_s,
        )
        if result.tool_absent:
            raise GraderKindRefusedError(result.tool_absent_reason)
        if result.script_exec_error:
            return Grade(
                binary_pass=False,
                score=0.0,
                components=GradeComponents(custom_checks=0.0),
                reasons=f"test.sh execution failed: {result.script_exec_error}",
            )
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
