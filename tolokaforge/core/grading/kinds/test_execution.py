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

import math
import shlex
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
    "StagedPathSpec",
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


class StagedPathSpec(BaseModel):
    """One path carried INTO the container the suite runs in, before it runs.

    The mirror image of :class:`ArtifactSpec`. An artifact moves the *agent's
    work* to where the suite is; a staged path moves *the suite* to where the
    work is — the single-container shape, where the verifier execs in the
    agent's own container and so the suite has to be in it.

    It exists because the suite must NOT be in it any earlier. Bind-mounting
    the suite onto the agent service makes the assertions the agent is scored
    by, and any reference solution shipped beside them, readable for the whole
    episode. The mount goes on a sidecar the agent cannot reach instead, and
    this carries it across once the episode is over.

    ``source`` is an absolute path, identical in both containers. ``src_service``
    names the sidecar holding it and is required — a staged path whose source
    defaulted to the agent's own container would copy the agent's ``/tests`` over
    itself and assert nothing. ``exclude`` lists glob patterns pruned from the
    copy.
    """

    model_config = {"extra": "forbid"}

    source: str
    src_service: str
    exclude: list[str] = Field(default_factory=list)


class CollectSpec(BaseModel):
    """A command run in a trial service container before artifacts are collected.

    The separate-verifier shape uses it to snapshot a stateful sidecar (a
    database) to a file inside that sidecar, which a matching
    :class:`ArtifactSpec` (``src_service`` = this ``service``) then carries into
    the verifier. A non-zero exit fails grading as ungradeable, never a 0.
    """

    model_config = {"extra": "forbid"}

    command: str
    service: str | None = None
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
    stage_probe_timeout_s: float = Field(default=30.0, gt=0.0)
    """Wall-clock cap on the one ``test -e`` that confirms a staged path landed."""
    service: str | None = None
    artifacts: list[ArtifactSpec] = Field(default_factory=list)
    collect: list[CollectSpec] = Field(default_factory=list)
    stage_paths: list[StagedPathSpec] = Field(default_factory=list)
    """Paths carried into the verifier's container before anything else runs.

    Unlike ``artifacts`` this does NOT belong to the separate-verifier shape —
    it is what lets the single-container shape keep its suite away from the
    agent until grading. So it is deliberately outside the ``service``
    requirement below: with no ``service`` the destination is the agent's own
    container, which is exactly the case this field exists for.
    """
    user: str | None = None
    """Unix user the verifier runs as, or ``None`` to inherit the container's
    default user.

    A task pack may run its suite as a different user than the agent — commonly
    ``root``, so the suite can read an oracle and write a reward into a
    directory the agent cannot touch. Nothing infers this: an adapter that
    knows its pack's contract passes the user, and every adapter that does not
    keeps the pre-existing behaviour of inheriting the image's ``USER``.
    """
    require_reward: bool = False
    """Treat a missing or unparseable reward as ungradeable instead of 0.0.

    Off, an absent reward folds to a score of zero, which is indistinguishable
    from a verifier that ran and failed the agent. On, the trial is refused the
    way ``script_exec_error`` already is, because a suite whose verdict never
    landed has not scored the agent at all.
    """

    @model_validator(mode="after")
    def _blank_user_means_unset(self) -> TestExecutionKindConfig:
        # The gRPC lane cannot distinguish "" from unset (proto3 string default),
        # so it maps "" -> None. Normalising here keeps the in-process lane from
        # issuing ``docker exec --user ''`` for the same config.
        if self.user is not None and not self.user.strip():
            self.user = None
        elif self.user is not None:
            self.user = self.user.strip()
        return self

    @model_validator(mode="after")
    def _artifacts_and_collect_need_a_service(self) -> TestExecutionKindConfig:
        # ``stage_paths`` is NOT in this check on purpose: it is the one hook
        # whose destination may legitimately be the agent's own container.
        if (self.artifacts or self.collect) and self.service is None:
            raise ValueError(
                "artifacts and collect hooks belong to the separate-verifier shape, so "
                "a 'service' (the verifier) must be set when either is non-empty."
            )
        return self

    @model_validator(mode="after")
    def _staged_path_cannot_be_its_own_source(self) -> TestExecutionKindConfig:
        # Copying a service's path onto itself empties it (materialise_path is
        # empty-then-copy against a source it has just deleted), so the suite
        # would be gone at the moment it is needed and the trial would grade on
        # nothing. Refuse the config instead of discovering it as a zero.
        for staged in self.stage_paths:
            if staged.src_service == self.service:
                raise ValueError(
                    f"stage_paths entry for {staged.source!r} names src_service "
                    f"{staged.src_service!r}, which is also the verifier service: a "
                    "path cannot be staged from the container it is staged into."
                )
        return self


def _looks_like_a_missing_file(raw: str) -> bool:
    """Whether *raw* is ``cat`` reporting the reward file does not exist.

    Under ``require_reward`` the reward is read with a bare ``cat``, so an
    absent file arrives as that command's own error text rather than as empty
    bytes. Matching it is what keeps the wrong-user hint reachable: without
    this the commonest failure of all falls through to "not a finite number".
    """
    lowered = raw.lower()
    return "no such file" in lowered or "cannot open" in lowered


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
        for staged in cfg.stage_paths:
            # Carry the suite into the container it runs in, now that the agent
            # is done. Everything below — the collect hooks, the artifacts, the
            # run itself — presupposes the suite is there, so this goes first.
            substrate.materialise_path(
                src_service=staged.src_service,
                dst_service=cfg.service,
                path=staged.source,
                exclude=staged.exclude,
            )
            # materialise_path treats an absent SOURCE as "the verifier runs
            # without it": correct for an artifact the agent never produced,
            # catastrophic for the test suite. Without this probe a sidecar that
            # never came up grades every trial 0.0 — a number that reads as a
            # hundred model failures and is really one provisioning bug. Check
            # the destination, which also catches a copy that silently landed
            # nothing.
            exit_code, output = substrate.exec_in_service(
                service=cfg.service,
                command=f"test -e {shlex.quote(staged.source)}",
                timeout_s=cfg.stage_probe_timeout_s,
            )
            if exit_code != 0:
                raise GraderKindRefusedError(
                    f"the test suite was not staged: {staged.source!r} is absent from "
                    f"{cfg.service or 'the agent container'} after copying it from "
                    f"{staged.src_service!r}. Nothing was measured, so this trial is "
                    f"ungradeable rather than a zero. Probe said: "
                    f"{output[: cfg.output_truncation_chars]!r}"
                )
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
            user=cfg.user,
            require_reward=cfg.require_reward,
        )
        if result.tool_absent:
            raise GraderKindRefusedError(result.tool_absent_reason)
        if result.script_exec_error:
            raise GraderKindRefusedError(f"test.sh execution failed: {result.script_exec_error}")
        raw = result.reward_bytes.decode(errors="ignore").strip()
        try:
            reward = float(raw.split("\n")[-1])
            if not math.isfinite(reward):
                # nan and inf are not scores. Clamping them silently awards a
                # PERFECT 1.0 -- every comparison with nan is False, so
                # ``max(0.0, min(1.0, nan))`` returns 1.0 -- which would turn a
                # corrupt reward file into a full pass.
                raise ValueError(f"reward is not a finite number: {reward!r}")
            reward = max(0.0, min(1.0, reward))
        except (ValueError, IndexError):
            if cfg.require_reward:
                # The suite ran; its verdict did not land. Scoring that 0.0
                # reports a number for a measurement that never happened, and
                # reads as a model failure. Refuse it instead, as an exec
                # failure already is.
                # Name the failure that actually happened. These are four
                # different problems with four different owners, and the first
                # two have nothing to do with users: pointing every one of them
                # at the 'user' setting sends whoever reads it after a
                # permissions bug that is not there. Measured 2026-10-30, when
                # exactly that message was emitted for a container the daemon
                # had already stopped.
                lowered = raw.lower()
                suite_out = (result.stdout or "").lower()
                if (
                    "is not running" in lowered
                    or "no such container" in lowered
                    or "oci runtime exec failed" in lowered
                ):
                    reason = (
                        "the trial's container was gone when grading ran, so "
                        "nothing was measured. This is lost infrastructure, not "
                        f"a failed attempt. Daemon said: {raw!r}"
                    )
                elif result.exit_code in (137, 143) or (
                    "[exit code: 137]" in suite_out or "[exit code: 143]" in suite_out
                ):
                    # The SIGNAL lands on the SUITE's exit, not on the reward
                    # read: by the time we cat the reward the verifier is
                    # already dead and the cat exits 1 on its own. Testing the
                    # cat's output for 137 never fires, which is how the four
                    # sweep()-class tasks would have been mislabelled.
                    reason = (
                        f"the verifier was killed by signal (exit {result.exit_code}) "
                        "before it could write a verdict."
                    )
                elif _looks_like_a_missing_file(raw):
                    # `require_reward` drops the `2>/dev/null || echo 0.0`
                    # fallback, so an ABSENT reward comes back as the cat's own
                    # error, not as empty bytes. That is the single most common
                    # shape -- the one #1953 exists for -- so it must be matched
                    # before the generic "not a number" branch, or the wrong-user
                    # hint becomes unreachable in practice.
                    reason = (
                        f"the verifier wrote no reward at {cfg.reward_path!r}. "
                        "Common cause: it could not write there because it ran "
                        "as the wrong user — see this kind's 'user' setting."
                    )
                elif raw:
                    reason = (
                        f"the reward at {cfg.reward_path!r} is not a finite "
                        f"number. Read: {raw!r}"
                    )
                else:
                    reason = (
                        f"the verifier wrote no reward at {cfg.reward_path!r} "
                        "(the file was empty). Common cause: it could not write "
                        "there because it ran as the wrong user — see this "
                        "kind's 'user' setting."
                    )
                raise GraderKindRefusedError(
                    f"test-execution produced no usable verdict: {reason}"
                ) from None
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
