"""Redaction happens where writing happens, and the bundle says it happened.

The layering this file exists to hold: the policy runs inside the **writer**, so
the grader never sees a rewritten argument. Every claim is driven through the
production artifact-write phase (``InProcessConductor._write_artifacts``) or the
production provision-failure path, never through a hand-rolled stand-in.

What each claim carries:

1. **Live grading is unaffected** — the load-bearing one. The trial's record is
   built by the real ``TrialToolCallRecorder``, the bundle is written under both
   policies, and the pack's ``args.api_token`` constraint is scored against the
   in-memory trajectory *after* each write. Same verdict both times, because the
   writer redacts a dump and never the model the grader reads.
2. **The default writes what the agent sent** — no stamp, no withheld sidecar,
   the credential present in both argument-carrying artifacts.
3. **Under a redacting policy** both artifacts carry the placeholder, ordinary
   arguments survive, and the stamp names what was rewritten.
4. **The judge sidecar is withheld and the stamp says so**, and the credential
   appears in **no file in the bundle**.
5. **Two trials through one writer** get their own stamps.
6. **The provision-failure bundle** stamps only what it wrote, and Stage 2's
   gate still refuses it.

**Scope of the whole-bundle secret scan (claim 4).** It proves absence for the
deterministic, argument-derived sites only. Judge prose — ``Grade.reasons`` and
``CriterionResult.justification`` — can quote the transcript the judge was shown,
and no key-name rule reaches prose; the judge is stubbed here, so this file's
green says nothing about it. That gap is declared design, covered by #1157.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from tests.canonical._factories import make_task_config, make_trial_spec
from tests.utils.conductor_phases import (
    make_conductor,
    make_run_config,
    make_setup,
    runner_stub,
)
from tolokaforge.core.conductor import InMemoryConductor
from tolokaforge.core.grading.trace_checks import evaluate_trace_checks
from tolokaforge.core.grading.trace_timeline import build_trial_timeline
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    JudgeStatus,
    Message,
    MessageRole,
    Metrics,
    ToolCall,
    ToolExecutionStatus,
    ToolExecutorIdentity,
    TraceChecksConfig,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.output.artifacts import (
    FileArtifactWriter,
    RedactedBundleError,
    bundle_redaction,
    read_recorded_tool_log,
)
from tolokaforge.core.redaction import (
    REDACTED_PLACEHOLDER,
    NoRedaction,
    RedactionPolicyName,
    SensitiveKeyRedaction,
)
from tolokaforge.core.runner import TrialToolCallRecorder
from tolokaforge.core.runtime import InMemoryRuntimeBackend, ProvisionError
from tolokaforge.core.trial_executor import ProvisioningTrialExecutor

pytestmark = [pytest.mark.canonical, pytest.mark.grading]

_SECRET = "sk-live-abc123"
_URL = "https://api.example/v1/orders"
_TASK_ID = "refund_task"

#: A constraint that reads the very argument the policy rewrites. Scored against
#: the in-memory trajectory it passes; scored against a redacted bundle it fails
#: as *decided*, which is the confident wrong answer this layering prevents.
_TRACE_CHECKS = TraceChecksConfig.model_validate(
    {
        "constraints": [
            {
                "id": "the_live_token_was_passed",
                "description": "the agent passed the live token to get_order",
                "require": {
                    "present": {
                        "match": {
                            "kind": "tool_call",
                            "tool": {"equals": "get_order"},
                            "args": {"api_token": {"equals": _SECRET}},
                        }
                    }
                },
            }
        ]
    }
)


def _recorded_trajectory() -> Trajectory:
    """A trial whose record comes off the production recorder.

    Built through :class:`TrialToolCallRecorder` rather than by hand so that
    moving the policy into ``record`` — the wrong layer this issue exists to
    prevent — reds the grading claim below.
    """
    recorder = TrialToolCallRecorder()
    arguments = {"id": "O-1", "url": _URL, "api_token": _SECRET, "body": {"api_key": _SECRET}}
    recorder.record(
        call_id="c1",
        tool_name="get_order",
        arguments=arguments,
        executor=ToolExecutorIdentity.AGENT,
        status=ToolExecutionStatus.SUCCESS,
        output='{"total": 328.5}',
        latency_seconds=0.01,
    )
    now = datetime.now(UTC)
    return Trajectory(
        task_id=_TASK_ID,
        trial_index=0,
        start_ts=now,
        end_ts=now,
        status=TrialStatus.COMPLETED,
        metrics=Metrics(),
        tool_log=list(recorder.recorded),
        messages=[
            Message(role=MessageRole.USER, content="Refund order O-1."),
            Message(
                role=MessageRole.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id="c1", name="get_order", arguments=dict(arguments))],
            ),
            Message(role=MessageRole.TOOL, content='{"total": 328.5}', tool_call_id="c1"),
            Message(role=MessageRole.ASSISTANT, content="Refunded."),
        ],
        grade=Grade(
            binary_pass=True,
            score=1.0,
            components=GradeComponents(llm_judge=1.0),
            judge_status=JudgeStatus.COMPLETED,
            judge_transcript=[
                {"role": "user", "content": f"  -> tool_call get_order(api_token={_SECRET})"}
            ],
        ),
    )


def _write_bundle(trial_root: Path, policy: Any, trajectory: Trajectory) -> Path:
    """Drive the production artifact-write phase under *policy*; return the bundle."""
    conductor = make_conductor(
        make_run_config(trial_root / "results"),
        trial_root,
        MagicMock(),
        artifact_writer=FileArtifactWriter(redaction=policy),
    )
    setup = make_setup(trial_root, _TASK_ID, 0)
    conductor._write_artifacts(
        make_trial_spec(trial_id=f"{_TASK_ID}:0", task_id=_TASK_ID),
        make_task_config(task_id=_TASK_ID),
        setup,
        trajectory,
        runner_stub(),
    )
    return setup.trial_dir


def _scored_in_memory(trajectory: Trajectory) -> bool:
    """The constraint's verdict over the trajectory the run still holds."""
    timeline = build_trial_timeline(
        trajectory.messages, trajectory.tool_log, trajectory.termination_reason
    )
    result = evaluate_trace_checks(timeline, _TRACE_CHECKS)
    (constraint,) = result.constraints
    return constraint.passed


def _tool_log(bundle: Path) -> list[dict[str, Any]]:
    return yaml.safe_load((bundle / "tool_log.yaml").read_text(encoding="utf-8"))


def _trajectory_tool_calls(bundle: Path) -> list[dict[str, Any]]:
    written = yaml.safe_load((bundle / "trajectory.yaml").read_text(encoding="utf-8"))
    return [call for message in written["messages"] for call in message.get("tool_calls") or []]


class TestLiveGradingIsUnaffected:
    """The claim the whole layering exists for."""

    @pytest.mark.parametrize(
        ("policy", "label"),
        [(NoRedaction(), "default"), (SensitiveKeyRedaction(), "redacting")],
    )
    def test_the_constraint_passes_after_the_bundle_is_written(
        self, tmp_path: Path, policy: Any, label: str
    ) -> None:
        """Scored *after* the write, so a writer that reached the grader's input —
        by mutating the trajectory, or by redacting at record time — reds here."""
        trajectory = _recorded_trajectory()

        _write_bundle(tmp_path / label, policy, trajectory)

        assert _scored_in_memory(trajectory) is True

    def test_both_policies_score_the_same_while_the_bundles_differ(self, tmp_path: Path) -> None:
        plain_trajectory = _recorded_trajectory()
        redacted_trajectory = _recorded_trajectory()

        plain = _write_bundle(tmp_path / "plain", NoRedaction(), plain_trajectory)
        redacted = _write_bundle(
            tmp_path / "redacted", SensitiveKeyRedaction(), redacted_trajectory
        )

        assert _scored_in_memory(plain_trajectory) == _scored_in_memory(redacted_trajectory)
        assert _tool_log(plain) != _tool_log(redacted)

    def test_the_writer_leaves_the_run_s_own_trajectory_alone(self, tmp_path: Path) -> None:
        trajectory = _recorded_trajectory()

        _write_bundle(tmp_path / "redacted", SensitiveKeyRedaction(), trajectory)

        assert trajectory.tool_log[0].arguments["api_token"] == _SECRET
        assert trajectory.messages[1].tool_calls[0].arguments["api_token"] == _SECRET


class TestTheDefaultWritesWhatTheAgentSent:
    def test_both_argument_carrying_artifacts_hold_the_credential(self, tmp_path: Path) -> None:
        bundle = _write_bundle(tmp_path, NoRedaction(), _recorded_trajectory())

        assert _tool_log(bundle)[0]["arguments"]["api_token"] == _SECRET
        assert _trajectory_tool_calls(bundle)[0]["arguments"]["api_token"] == _SECRET

    def test_the_bundle_carries_no_stamp_and_withholds_nothing(self, tmp_path: Path) -> None:
        bundle = _write_bundle(tmp_path, NoRedaction(), _recorded_trajectory())

        assert bundle_redaction(bundle) is None
        assert "redaction" not in yaml.safe_load((bundle / "metrics.yaml").read_text())
        assert (bundle / "judge_trajectory.yaml").exists()


class TestARedactingPolicyRewritesAndStamps:
    def test_both_artifacts_carry_the_placeholder_and_keep_ordinary_arguments(
        self, tmp_path: Path
    ) -> None:
        bundle = _write_bundle(tmp_path, SensitiveKeyRedaction(), _recorded_trajectory())

        for arguments in (
            _tool_log(bundle)[0]["arguments"],
            _trajectory_tool_calls(bundle)[0]["arguments"],
        ):
            assert arguments["api_token"] == REDACTED_PLACEHOLDER
            assert arguments["body"]["api_key"] == REDACTED_PLACEHOLDER
            assert arguments["url"] == _URL
            assert arguments["id"] == "O-1"

    def test_the_stamp_names_the_policy_and_both_rewritten_artifacts(self, tmp_path: Path) -> None:
        bundle = _write_bundle(tmp_path, SensitiveKeyRedaction(), _recorded_trajectory())

        stamp = bundle_redaction(bundle)

        assert stamp is not None
        assert stamp.policy is RedactionPolicyName.SENSITIVE_KEYS
        assert stamp.artifacts == ["tool_log.yaml", "trajectory.yaml"]


class TestTheJudgeSidecarIsWithheldAndDeclared:
    def test_the_sidecar_is_absent_and_the_stamp_names_it(self, tmp_path: Path) -> None:
        bundle = _write_bundle(tmp_path, SensitiveKeyRedaction(), _recorded_trajectory())

        stamp = bundle_redaction(bundle)

        assert not (bundle / "judge_trajectory.yaml").exists()
        assert stamp is not None
        assert stamp.omitted == ["judge_trajectory.yaml"]

    def test_no_file_in_the_bundle_carries_the_credential(self, tmp_path: Path) -> None:
        """Scans every written file, so a fourth argument-carrying artifact added
        later fails here rather than shipping beside a stamp that says otherwise."""
        bundle = _write_bundle(tmp_path, SensitiveKeyRedaction(), _recorded_trajectory())

        carrying = [
            path.name
            for path in sorted(bundle.iterdir())
            if path.is_file() and _SECRET in path.read_text(encoding="utf-8", errors="replace")
        ]

        assert carrying == []

    def test_a_trial_with_no_judge_transcript_withholds_nothing(self, tmp_path: Path) -> None:
        """The empty ``omitted`` is what keeps "no judge ran" apart from "withheld"."""
        trajectory = _recorded_trajectory()
        ungraded = trajectory.model_copy(
            update={"grade": trajectory.grade.model_copy(update={"judge_transcript": None})}
        )

        bundle = _write_bundle(tmp_path, SensitiveKeyRedaction(), ungraded)

        stamp = bundle_redaction(bundle)
        assert stamp is not None
        assert stamp.omitted == []


class TestOneWriterTwoTrials:
    def test_each_trial_is_stamped_with_only_its_own_artifacts(self, tmp_path: Path) -> None:
        """The accumulated set lives on the per-trial writer, not on the shared one."""
        writer = FileArtifactWriter(redaction=SensitiveKeyRedaction())
        first = tmp_path / "trials" / _TASK_ID / "0"
        second = tmp_path / "trials" / _TASK_ID / "1"

        writer.write_trial_bundle(
            first, _recorded_trajectory(), {"task_id": _TASK_ID}, {}, StructuredLogger("first")
        )
        # The second trial writes no tool-call record, so a set bleeding from the
        # first would name a file this bundle does not carry.
        writer.write_trajectory(second, _recorded_trajectory())
        writer.write_metrics(second, _recorded_trajectory())

        first_stamp = bundle_redaction(first)
        second_stamp = bundle_redaction(second)
        assert first_stamp is not None and second_stamp is not None
        assert first_stamp.artifacts == ["tool_log.yaml", "trajectory.yaml"]
        assert second_stamp.artifacts == ["trajectory.yaml"]


class _FailProvisionBackend(InMemoryRuntimeBackend):
    def provision(self, spec):  # type: ignore[override]
        raise ProvisionError(trial_id=spec.trial_id, stage="provision", reason="no capacity")


def _provision_failure_bundle(output_dir: Path) -> Path:
    """The bundle the production provision-failure path writes under redaction."""
    executor = ProvisioningTrialExecutor(
        runtime_backend=_FailProvisionBackend(),
        conductor=InMemoryConductor(),
        logger=StructuredLogger("provision-failure-redaction"),
        output_dir=output_dir,
        artifact_writer=FileArtifactWriter(redaction=SensitiveKeyRedaction()),
    )

    executor.execute(
        make_trial_spec(trial_id=f"{_TASK_ID}:0", task_id=_TASK_ID),
        make_task_config(task_id=_TASK_ID),
    )

    return output_dir / "trials" / _TASK_ID / "0"


class TestTheProvisionFailureBundleStampsWhatItWrote:
    def test_the_stamp_names_only_the_trajectory_and_withholds_nothing(
        self, tmp_path: Path
    ) -> None:
        """That path writes no ``tool_log.yaml`` and has no grade at all, so a
        statically scoped stamp would name a file the bundle lacks."""
        bundle = _provision_failure_bundle(tmp_path)

        stamp = bundle_redaction(bundle)
        assert not (bundle / "tool_log.yaml").exists()
        assert stamp is not None
        assert stamp.artifacts == ["trajectory.yaml"]
        assert stamp.omitted == []

    def test_the_gate_refuses_the_bundle_this_writer_produced(self, tmp_path: Path) -> None:
        """Stage 2's refusal is locked over a hand-stamped bundle; this is the one
        it will meet. The bundle carries no ``tool_log.yaml``, so a gate consulted
        after the record would read it back as a trial that called nothing."""
        bundle = _provision_failure_bundle(tmp_path)

        with pytest.raises(RedactedBundleError, match="sensitive_keys"):
            read_recorded_tool_log(bundle)
