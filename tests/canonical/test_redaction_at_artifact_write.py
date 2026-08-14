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
   the credential present in every artifact that carries one.
3. **Under a redacting policy** the argument-carrying artifacts carry the
   placeholder, ordinary arguments survive, and the stamp names what was rewritten.
4. **Both judge sidecars are withheld and the stamp says so**, and the credential
   appears in **no file in the bundle**.
5. **Two trials through one writer** get their own stamps.
6. **The provision-failure bundle** stamps only what it wrote, and the offline
   reader still refuses it.
7. **The stamp is a property of the writer, not of ``write_metrics``** — the
   judge-replay path writes a grade and never a metrics file, and its bundle is
   stamped all the same.

**Scope of the whole-bundle secret scan (claim 4).** It proves absence for the
deterministic, argument-derived sites only. Judge prose — ``Grade.reasons`` and
``CriterionResult.justification`` — can quote the transcript the judge was shown,
and no key-name rule reaches prose; the judge is stubbed here, so this file's
green says nothing about it. ``logs.yaml`` is likewise outside the write policy:
its context keys are sanitised at source by the same vocabulary, which is why the
planted log line here carries the credential under a credential-*named* key —
a value quoted in a log message would survive, and that is the same declared gap.
Both are #1157's.
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
from tests.utils.provision_failure import FailProvisionBackend, provisioning_executor
from tolokaforge.core.grading.judge import JudgeResult, JudgeUsage
from tolokaforge.core.grading.judge import JudgeStatus as JudgeRunStatus
from tolokaforge.core.grading.replay import (
    FidelityMode,
    KnowledgeSearchMode,
    ProvenanceSource,
    ReplayProvenance,
    _write_replay_artifacts,
)
from tolokaforge.core.grading.trace_checks import evaluate_trace_checks
from tolokaforge.core.grading.trace_timeline import build_trial_timeline
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    JudgeInputs,
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
    moving the policy into ``record`` — the wrong layer — reds the grading claim
    below.

    Every argument-derived site a bundle has carries the credential: the recorded
    call, the message trace, the final env snapshot, the judge transcript and the
    judge's structured inputs. The whole-bundle scan is only a lock over the files
    that actually hold one.
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
        final_env_state={"orders": ["O-1"], "session": {"api_token": _SECRET}},
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
            judge_inputs=JudgeInputs(
                state_diff_text=f"session.api_token: null -> {_SECRET}",
                read_tools_offered=["get_db_state"],
            ),
        ),
    )


def _write_bundle(trial_root: Path, policy: Any, trajectory: Trajectory) -> Path:
    """Drive the production artifact-write phase under *policy*; return the bundle.

    The trial's logger is handed the credential under a credential-named context
    key, which the log path sanitises at source. ``logs.yaml`` is written from
    what that already redacted, never through the write policy.
    """
    conductor = make_conductor(
        make_run_config(trial_root / "results"),
        trial_root,
        MagicMock(),
        artifact_writer=FileArtifactWriter(redaction=policy),
    )
    runner = runner_stub()
    runner.logger.info("tool.request", tool="get_order", api_token=_SECRET)
    setup = make_setup(trial_root, _TASK_ID, 0)
    conductor._write_artifacts(
        make_trial_spec(trial_id=f"{_TASK_ID}:0", task_id=_TASK_ID),
        make_task_config(task_id=_TASK_ID),
        setup,
        trajectory,
        runner,
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


def _env_state(bundle: Path) -> dict[str, Any]:
    return yaml.safe_load((bundle / "env.yaml").read_text(encoding="utf-8"))


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
    def test_every_argument_carrying_artifact_holds_the_credential(self, tmp_path: Path) -> None:
        bundle = _write_bundle(tmp_path, NoRedaction(), _recorded_trajectory())

        assert _tool_log(bundle)[0]["arguments"]["api_token"] == _SECRET
        assert _trajectory_tool_calls(bundle)[0]["arguments"]["api_token"] == _SECRET
        assert _env_state(bundle)["session"]["api_token"] == _SECRET

    def test_the_bundle_carries_no_stamp_and_withholds_nothing(self, tmp_path: Path) -> None:
        bundle = _write_bundle(tmp_path, NoRedaction(), _recorded_trajectory())

        assert bundle_redaction(bundle) is None
        assert "redaction" not in yaml.safe_load((bundle / "metrics.yaml").read_text())
        assert (bundle / "judge_trajectory.yaml").exists()
        assert (bundle / "judge_inputs.yaml").exists()


class TestARedactingPolicyRewritesAndStamps:
    def test_every_rewritten_artifact_carries_the_placeholder_and_keeps_ordinary_values(
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
        assert _env_state(bundle) == {
            "orders": ["O-1"],
            "session": {"api_token": REDACTED_PLACEHOLDER},
        }

    def test_the_stamp_names_the_policy_and_every_rewritten_artifact(self, tmp_path: Path) -> None:
        bundle = _write_bundle(tmp_path, SensitiveKeyRedaction(), _recorded_trajectory())

        stamp = bundle_redaction(bundle)

        assert stamp is not None
        assert stamp.policy is RedactionPolicyName.SENSITIVE_KEYS
        assert stamp.artifacts == ["env.yaml", "tool_log.yaml", "trajectory.yaml"]


class TestTheJudgeSidecarsAreWithheldAndDeclared:
    def test_both_sidecars_are_absent_and_the_stamp_names_them(self, tmp_path: Path) -> None:
        bundle = _write_bundle(tmp_path, SensitiveKeyRedaction(), _recorded_trajectory())

        stamp = bundle_redaction(bundle)

        assert not (bundle / "judge_trajectory.yaml").exists()
        assert not (bundle / "judge_inputs.yaml").exists()
        assert stamp is not None
        assert stamp.omitted == ["judge_inputs.yaml", "judge_trajectory.yaml"]

    def test_no_file_in_the_bundle_carries_the_credential(self, tmp_path: Path) -> None:
        """Scans every written file, so an argument-carrying artifact added later
        fails here rather than shipping beside a stamp that says otherwise. Every
        file the trial produces is fed the credential first (see
        :func:`_recorded_trajectory` and :func:`_write_bundle`), so an empty result
        is absence rather than a bundle that never held one."""
        bundle = _write_bundle(tmp_path, SensitiveKeyRedaction(), _recorded_trajectory())

        carrying = [
            path.name
            for path in sorted(bundle.iterdir())
            if path.is_file() and _SECRET in path.read_text(encoding="utf-8", errors="replace")
        ]

        assert carrying == []

    def test_the_default_bundle_is_the_control_the_scan_needs(self, tmp_path: Path) -> None:
        """Which files the scan above proves anything about. Without this a fixture
        that planted the credential nowhere would pass it."""
        bundle = _write_bundle(tmp_path, NoRedaction(), _recorded_trajectory())

        carrying = sorted(
            path.name
            for path in bundle.iterdir()
            if path.is_file() and _SECRET in path.read_text(encoding="utf-8", errors="replace")
        )

        assert carrying == [
            "env.yaml",
            "judge_inputs.yaml",
            "judge_trajectory.yaml",
            "tool_log.yaml",
            "trajectory.yaml",
        ]

    def test_the_log_path_redacts_its_own_context_before_the_bundle_is_written(
        self, tmp_path: Path
    ) -> None:
        """``logs.yaml`` is out of the write policy's reach by design — the same
        vocabulary is applied where the context key is logged. A credential quoted
        in a log *message* would survive both, which is #1157's."""
        bundle = _write_bundle(tmp_path, NoRedaction(), _recorded_trajectory())

        logs = yaml.safe_load((bundle / "logs.yaml").read_text(encoding="utf-8"))

        (planted,) = [entry for entry in logs["logs"] if entry["message"] == "tool.request"]
        assert planted["context"]["api_token"] == REDACTED_PLACEHOLDER

    def test_a_trial_the_judge_left_no_sidecar_for_withholds_nothing(self, tmp_path: Path) -> None:
        """The empty ``omitted`` is what keeps "no judge ran" apart from "withheld"."""
        trajectory = _recorded_trajectory()
        unjudged = trajectory.model_copy(
            update={
                "grade": trajectory.grade.model_copy(
                    update={"judge_transcript": None, "judge_inputs": None}
                )
            }
        )

        bundle = _write_bundle(tmp_path, SensitiveKeyRedaction(), unjudged)

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
        assert first_stamp.artifacts == ["env.yaml", "tool_log.yaml", "trajectory.yaml"]
        assert second_stamp.artifacts == ["trajectory.yaml"]


def _provision_failure_bundle(output_dir: Path) -> Path:
    """The bundle the production provision-failure path writes under redaction."""
    executor = provisioning_executor(
        FailProvisionBackend("no capacity"),
        output_dir,
        FileArtifactWriter(redaction=SensitiveKeyRedaction()),
        logger=StructuredLogger("provision-failure-redaction"),
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
        """The offline refusal is locked over hand-written stamps in
        ``test_redacted_bundle_refusal.py``; this is the bundle it meets in
        production. This one carries no ``tool_log.yaml``, so a gate consulted
        after the record would read it back as a trial that called nothing."""
        bundle = _provision_failure_bundle(tmp_path)

        with pytest.raises(RedactedBundleError, match="sensitive_keys"):
            read_recorded_tool_log(bundle)


def _replay_result() -> JudgeResult:
    """A replay's judge outcome, carrying both sidecars a redacting policy withholds."""
    return JudgeResult(
        status=JudgeRunStatus.COMPLETED,
        usage=JudgeUsage(calls=1),
        reasons="the refund was issued",
        score=1.0,
        binary_pass=True,
        state_diff=f"session.api_token: null -> {_SECRET}",
        transcript=({"role": "user", "content": f"get_order(api_token={_SECRET})"},),
    )


_REPLAY_PROVENANCE = ReplayProvenance(
    judge_model="openrouter/openai/gpt-4.1-mini",
    judge_model_source=ProvenanceSource.RECORDED,
    rubric_source=ProvenanceSource.RECORDED,
    knowledge_search_mode=KnowledgeSearchMode.RECORDED,
    knowledge_search_disabled=False,
    custom_system_prompt=False,
    custom_prompt_source=None,
    include_agent_system_prompt=True,
    agent_prompt_source=None,
    fidelity_mode=FidelityMode.FULL,
)


class TestTheStampBelongsToTheWriterNotToWriteMetrics:
    """A caller that never writes ``metrics.yaml`` still leaves a stamped bundle.

    Judge replay is that caller: :func:`_write_replay_artifacts` writes a grade and
    a provenance file into a fresh directory. Under a redacting policy it withholds
    both judge sidecars, and a bundle that withheld them while declaring nothing
    would read as a faithful replay of a judge that produced no transcript.
    """

    def test_the_replay_bundle_is_stamped_and_names_what_it_withheld(self, tmp_path: Path) -> None:
        dest = tmp_path / "replays" / "canon" / _TASK_ID / "0"

        _write_replay_artifacts(
            dest,
            _replay_result(),
            _REPLAY_PROVENANCE,
            FileArtifactWriter(redaction=SensitiveKeyRedaction()),
        )

        stamp = bundle_redaction(dest)
        assert stamp is not None
        assert stamp.policy is RedactionPolicyName.SENSITIVE_KEYS
        assert stamp.artifacts == []
        assert stamp.omitted == ["judge_inputs.yaml", "judge_trajectory.yaml"]

    def test_the_offline_reader_refuses_that_replay_bundle(self, tmp_path: Path) -> None:
        """Naming no rewritten artifact must not read as nothing to refuse."""
        dest = tmp_path / "replays" / "canon" / _TASK_ID / "0"

        _write_replay_artifacts(
            dest,
            _replay_result(),
            _REPLAY_PROVENANCE,
            FileArtifactWriter(redaction=SensitiveKeyRedaction()),
        )

        with pytest.raises(RedactedBundleError, match="withheld judge_inputs.yaml"):
            read_recorded_tool_log(dest)

    def test_a_bundle_whose_metrics_write_never_happened_is_stamped_anyway(
        self, tmp_path: Path
    ) -> None:
        """``ProvisioningTrialExecutor`` logs and swallows a failing
        ``write_metrics``, so the rewritten ``trajectory.yaml`` would otherwise be
        the whole bundle and it would read as faithful."""
        bundle = tmp_path / "trials" / _TASK_ID / "0"
        writer = FileArtifactWriter(redaction=SensitiveKeyRedaction())

        writer.write_trajectory(bundle, _recorded_trajectory())

        stamp = bundle_redaction(bundle)
        assert stamp is not None
        assert stamp.artifacts == ["trajectory.yaml"]

    def test_the_default_replay_bundle_carries_both_sidecars_and_no_stamp(
        self, tmp_path: Path
    ) -> None:
        dest = tmp_path / "replays" / "canon" / _TASK_ID / "0"

        _write_replay_artifacts(dest, _replay_result(), _REPLAY_PROVENANCE, FileArtifactWriter())

        assert bundle_redaction(dest) is None
        assert (dest / "judge_trajectory.yaml").exists()
        assert (dest / "judge_inputs.yaml").exists()
