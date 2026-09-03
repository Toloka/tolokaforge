"""``prompts.yaml`` records the effective judge system prompt verbatim, and
replay reads that recorded prompt in preference to the current engine constant.

Every trial bundle's ``prompts.yaml`` must carry a top-level ``judge_prompt``
key equal to ``_compose_judge_system_prompt(customization.system_prompt)`` for
the trial's effective ``LLMJudgeConfig`` — the exact prose the judge would have
graded under, byte-for-byte. A human analyst who opens the bundle sees which
contract graded it without trusting the current engine's constant; a bundle-
native replay reads the same string to reconstruct the judge's system prompt.

Scenarios locked here:

Bundle-write side:
    (1) ``customization=None`` (or no customization block at all) → ``judge_prompt``
        is byte-for-byte the engine default ``_JUDGE_SYSTEM_PROMPT``.
    (2) ``customization.system_prompt = "STRICT-VIBE"`` → ``judge_prompt`` begins
        with ``"STRICT-VIBE"`` (the author's body verbatim) and ends with
        ``_JUDGE_MARKER_CONTRACT`` (the harness always appends the marker so
        ``submit_report`` validation is unbreakable).
    (3) Auto-fail trial with ``llm_judge`` block → ``judge_prompt`` still recorded
        (derived from the effective ``grading_config``, not from a judge invocation).

Bundle-native replay side:
    (4) Recorded ``judge_prompt`` is reused byte-for-byte: no doubled marker,
        provenance stamped ``bundle``.
    (5) Bundle with no ``judge_prompt`` key but a recorded ``customization.
        system_prompt``: replay falls through to the legacy resolver and composes
        at run time; provenance stamps ``custom_prompt_source == "recorded"``.
    (6) Bundle with no ``judge_prompt`` key and no customisation anywhere:
        replay grades under the engine default and stamps every prompt source
        ``None`` — the one-way ``BUNDLE ⟹ (no legacy source)`` implication on
        ``ReplayProvenance`` admits this shape (a biconditional would red it).
    (7) Mutual-exclusion invariants: ``LLMJudge(explicit_system_prompt=...,
        custom_system_prompt=...)`` refuses construction, and
        ``ReplayProvenance(judge_prompt_source=BUNDLE, custom_prompt_source=
        RECORDED, custom_system_prompt=True)`` refuses validation — bundle-source
        with legacy-source together is a caller bug.

The write path is driven end-to-end: ``InProcessConductor._write_artifacts``
runs against a real ``FileArtifactWriter``, a real ``GradingConfig`` carrying an
``LLMJudgeConfig``, and the real ``_compose_judge_system_prompt`` composition —
no mocks of the code under test. Replay tests drive the real ``read_replay_inputs``
+ ``replay_trial`` path with a scripted judge model that captures the composed
system prompt handed to it, so the "no doubled marker" invariant is asserted
against a real ``LLMJudge.run`` composition rather than an intermediate value.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from pydantic import ValidationError

from tests.canonical._factories import make_task_config, make_trial_spec
from tests.unit.grading.test_judge import ScriptedClient
from tests.utils.conductor_phases import (
    make_conductor,
    make_run_config,
    make_setup,
    runner_stub,
)
from tolokaforge.core.grading.judge import LLMJudge
from tolokaforge.core.grading.replay import (
    FidelityMode,
    KnowledgeSearchMode,
    ProvenanceSource,
    ReplayProvenance,
    read_replay_inputs,
    replay_trial,
)
from tolokaforge.core.judge_prompt import (
    _JUDGE_MARKER_CONTRACT,
    _JUDGE_SYSTEM_PROMPT,
    _compose_judge_system_prompt,
)
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    GradingCombineConfig,
    GradingConfig,
    JudgeInputs,
    JudgeStatus,
    Message,
    MessageRole,
    Metrics,
    ModelConfig,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.output.artifacts import FileArtifactWriter
from tolokaforge.runner.models import Criterion, JudgeCustomization, LLMJudgeConfig, Rubric

pytestmark = pytest.mark.canonical


_RUBRIC = Rubric(
    criteria=[
        Criterion(
            id="agent_answered_the_question",
            description="Agent produced a final answer that addresses the user's question.",
            kind="binary",
            required=True,
        )
    ]
)


def _grading_config(customization: JudgeCustomization | None) -> GradingConfig:
    """Build a ``GradingConfig`` whose ``llm_judge`` block carries the given
    customization (``None`` means no customization block at all — the engine
    default judge prompt is in effect)."""
    return GradingConfig(
        combine=GradingCombineConfig(),
        llm_judge=LLMJudgeConfig(rubric=_RUBRIC, customization=customization),
    )


def _write_prompts_yaml(
    tmp_path: Path,
    customization: JudgeCustomization | None,
    *,
    status: TrialStatus = TrialStatus.COMPLETED,
) -> dict:
    """Drive ``_write_artifacts`` end-to-end for one synthetic trial whose task
    carries an ``LLMJudgeConfig``; return the loaded ``prompts.yaml``.

    ``status`` selects the trajectory's terminal state — ``COMPLETED`` for the
    graded happy path, ``ERROR`` for the auto-fail short-circuit that never
    invoked a judge. ``_write_artifacts`` derives the judge prompt from
    ``grading_config`` regardless of status; the same bundle shape reaches
    disk on both paths."""
    conductor = make_conductor(make_run_config(tmp_path / "results"), tmp_path, MagicMock())
    conductor.adapter.get_grading_config.return_value = _grading_config(customization)

    task = make_task_config("task_with_llm_judge")
    setup = make_setup(tmp_path, task.task_id, 0)
    now = datetime.now(UTC)
    trajectory = Trajectory(
        task_id=task.task_id,
        trial_index=0,
        start_ts=now,
        end_ts=now,
        status=status,
        messages=[],
        metrics=Metrics(),
    )

    conductor._write_artifacts(
        make_trial_spec(trial_id=f"{task.task_id}:0", task_id=task.task_id),
        task,
        setup,
        trajectory,
        runner_stub(),
    )

    return yaml.safe_load((setup.trial_dir / "prompts.yaml").read_text(encoding="utf-8"))


def test_prompts_yaml_records_the_default_judge_prompt_when_no_customization_is_configured(
    tmp_path: Path,
) -> None:
    """A task whose ``llm_judge`` block carries no ``customization`` records the
    engine default composition — byte-for-byte ``_JUDGE_SYSTEM_PROMPT``.

    Direct key access is intentional: a bundle written before this contract
    existed carries no ``judge_prompt`` key, and the ``KeyError`` names the
    missing contract instead of surfacing as a bare ``None`` any downstream
    consumer would have to guard against."""
    data = _write_prompts_yaml(tmp_path, customization=None)

    assert data["judge_prompt"] == _JUDGE_SYSTEM_PROMPT
    assert data["judge_prompt"] == _compose_judge_system_prompt(None)


def test_prompts_yaml_records_a_customized_judge_prompt_verbatim_with_the_marker_appended(
    tmp_path: Path,
) -> None:
    """A task's ``customization.system_prompt`` reaches ``prompts.yaml`` verbatim
    at the front, followed by the harness-owned marker contract at the tail —
    the exact composition the judge would have graded under, so a reader
    reconstructing the contract need not re-run ``_compose_judge_system_prompt``.

    Byte-for-byte equality against the composer's own output locks the whole
    string (body + separator + marker); the prefix / suffix assertions name what
    the shape is for a reader tracking down a regression."""
    body = "STRICT-VIBE"
    data = _write_prompts_yaml(tmp_path, customization=JudgeCustomization(system_prompt=body))

    assert data["judge_prompt"].startswith(body)
    assert data["judge_prompt"].endswith(_JUDGE_MARKER_CONTRACT)
    assert data["judge_prompt"] == _compose_judge_system_prompt(body)


def test_prompts_yaml_records_the_judge_prompt_on_an_auto_fail_trial_that_never_invoked_the_judge(
    tmp_path: Path,
) -> None:
    """An auto-fail trial (``TrialStatus.ERROR`` — the runner short-circuited
    before grading, so the judge was never called) still records the composed
    judge contract when the task carries an ``llm_judge`` block.

    ``_write_artifacts`` derives the effective prompt from ``grading_config``,
    not from the judge itself, so the bundle shape stays consistent across
    every trial of a run: a rejudge once the underlying auto-fail cause is
    fixed has the recorded contract to reconstruct against."""
    data = _write_prompts_yaml(tmp_path, customization=None, status=TrialStatus.ERROR)

    assert data["judge_prompt"] == _JUDGE_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Replay-side: the recorded prompt reaches the judge verbatim.
# ---------------------------------------------------------------------------


_REPLAY_AGENT_PROMPT = "You are the agent. Follow the refund policy exactly."
_REPLAY_JUDGE_MODEL = {"provider": "openrouter", "name": "openai/gpt-4.1-mini", "temperature": 0.0}


def _replay_trajectory() -> Trajectory:
    """A minimal completed conversation the judge can grade over."""
    now = datetime.now(UTC)
    return Trajectory(
        task_id="refund_task",
        trial_index=0,
        start_ts=now,
        end_ts=now,
        status=TrialStatus.COMPLETED,
        messages=[
            Message(role=MessageRole.USER, content="I want a refund for order O-1."),
            Message(role=MessageRole.ASSISTANT, content="Your refund of $328.50 is issued."),
        ],
    )


def _replay_rubric() -> dict:
    return {
        "reference": "The refund must be issued for $328.50.",
        "criteria": [
            {"id": "refund_amount", "description": "Refund quotes $328.50", "kind": "binary"},
        ],
    }


def _submit_report_client() -> ScriptedClient:
    """A judge model that immediately submits a MET verdict — the smallest
    scripted trajectory that reaches ``LLMJudge.run``'s composition site."""
    return ScriptedClient(
        [
            [
                (
                    "submit_report",
                    {
                        "reasons": "Refund quoted correctly.",
                        "refund_amount": True,
                        "refund_amount_justification": "Reply quotes $328.50. VERDICT: MET",
                    },
                )
            ]
        ]
    )


def _write_replay_bundle(
    trial_dir: Path,
    *,
    judge_prompt: str | None,
    customization: dict[str, object] | None,
    include_judge_prompt_key: bool = True,
) -> None:
    """Assemble a trial bundle the replay can read: trajectory + prompts + task +
    grade. ``include_judge_prompt_key=False`` simulates a pre-`judge_prompt` bundle by
    hand-writing a two-key ``prompts.yaml``. ``customization`` is a dict merged
    into ``grading_config.llm_judge`` — ``None`` omits the block entirely."""
    trial_dir.mkdir(parents=True, exist_ok=True)
    trajectory = _replay_trajectory()

    writer = FileArtifactWriter()
    writer.write_trajectory(trial_dir, trajectory)

    if include_judge_prompt_key:
        writer.write_prompts(
            trial_dir,
            agent_prompt=_REPLAY_AGENT_PROMPT,
            user_prompt="user-sim prompt",
            judge_prompt=judge_prompt,
        )
    else:
        # Pre-`judge_prompt` bundle shape — hand-written to omit the ``judge_prompt``
        # key entirely, so ``_resolve_bundle_judge_prompt`` sees an absent key
        # and falls through to the legacy resolver.
        (trial_dir / "prompts.yaml").write_text(
            yaml.safe_dump(
                {
                    "system_prompt": _REPLAY_AGENT_PROMPT,
                    "user_system_prompt": "user-sim prompt",
                },
                sort_keys=True,
                allow_unicode=True,
                default_flow_style=False,
            ),
            encoding="utf-8",
        )

    llm_judge: dict[str, object] = {"rubric": _replay_rubric()}
    if customization is not None:
        llm_judge["customization"] = customization
    writer.write_task(
        trial_dir,
        {
            "task_id": "refund_task",
            "trial_index": 0,
            "grading_config": {"llm_judge": llm_judge},
            "model_config": {"judge": _REPLAY_JUDGE_MODEL},
        },
    )
    writer.write_grade(
        trial_dir,
        Grade(
            binary_pass=True,
            score=1.0,
            components=GradeComponents(llm_judge=1.0),
            judge_status=JudgeStatus.COMPLETED,
            judge_inputs=JudgeInputs(read_tools_offered=[]),
        ),
    )


def _load_replay_provenance(dest: Path) -> dict:
    return yaml.safe_load((dest / "replay_provenance.yaml").read_text(encoding="utf-8"))


def _replay(tmp_path: Path, trial_dir: Path) -> tuple[ScriptedClient, Path]:
    """Drive ``read_replay_inputs`` + ``replay_trial`` end-to-end. Persists the
    replay artifacts under ``tmp_path/replay/`` so provenance and the judge's
    own transcript can be reloaded from YAML — the same shape a real batch
    replay produces. Returns the scripted client (its ``seen_system`` records
    the composed prompt handed to the judge) and the replay-artifact dir."""
    inputs = read_replay_inputs(trial_dir, knowledge_search=KnowledgeSearchMode.OFF)
    client = _submit_report_client()
    result = replay_trial(inputs, judge_client=client)

    dest = tmp_path / "replay"
    dest.mkdir(parents=True, exist_ok=True)
    from tolokaforge.core.grading.replay import build_replay_grade  # local — no re-export

    FileArtifactWriter().write_grade(dest, build_replay_grade(result))
    (dest / "replay_provenance.yaml").write_text(
        yaml.safe_dump(
            inputs.provenance.model_dump(mode="json"),
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return client, dest


def test_bundle_recorded_judge_prompt_is_reused_verbatim_with_no_doubled_marker(
    tmp_path: Path,
) -> None:
    """The composed prompt on disk reaches the judge verbatim: the recorded
    body + one marker appears once in the system prompt, no re-composition
    adds a second marker at ``LLMJudge.run`` time.

    Locks the two-resolver split. A regression that routed the bundle-recorded
    string through ``LLMJudge(custom_system_prompt=...)`` would trigger
    ``_compose_judge_system_prompt`` again — the composed prompt would end with
    the marker twice — which this test catches with a ``.count(marker) == 1``
    assertion after stripping the rubric brief."""
    pinned = f"PINNED BODY.\n\n{_JUDGE_MARKER_CONTRACT}"
    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_replay_bundle(trial_dir, judge_prompt=pinned, customization=None)

    client, dest = _replay(tmp_path, trial_dir)

    assert client.seen_system is not None
    assert client.seen_system.startswith(pinned), (
        "captured system prompt does not open with the recorded composed prompt — the "
        "resolver may have re-composed the body or dropped the marker"
    )
    assert client.seen_system.count(_JUDGE_MARKER_CONTRACT) == 1, (
        "marker contract appears more than once — the bundle-recorded prompt was "
        "routed through the legacy composer, which re-appended the marker"
    )

    provenance = _load_replay_provenance(dest)
    assert provenance["judge_prompt_source"] == "bundle"
    assert provenance["custom_prompt_source"] is None
    assert provenance["custom_system_prompt"] is False


def test_legacy_bundle_customized_body_falls_through_to_the_legacy_resolver(
    tmp_path: Path,
) -> None:
    """A pre-`judge_prompt` bundle (no ``judge_prompt`` key) with a recorded
    ``customization.system_prompt`` falls through to the legacy composer: the
    body is composed at run time (marker appended once), and provenance stamps
    the legacy source.

    Locks that the bundle path returning ``(None, None)`` really does fall
    through to ``_resolve_custom_prompt`` and its downstream composition —
    a regression that eagerly stamped ``judge_prompt_source`` would trigger
    the mutual-exclusion validator and red this trial."""
    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_replay_bundle(
        trial_dir,
        judge_prompt=None,
        include_judge_prompt_key=False,
        customization={"system_prompt": "LEGACY BODY."},
    )

    client, dest = _replay(tmp_path, trial_dir)

    expected = _compose_judge_system_prompt("LEGACY BODY.")
    assert client.seen_system is not None
    assert client.seen_system.startswith(expected)
    assert client.seen_system.count(_JUDGE_MARKER_CONTRACT) == 1

    provenance = _load_replay_provenance(dest)
    assert provenance["judge_prompt_source"] is None
    assert provenance["custom_prompt_source"] == "recorded"
    assert provenance["custom_system_prompt"] is True


def test_legacy_bundle_with_no_customization_replays_under_the_engine_default(
    tmp_path: Path,
) -> None:
    """A pre-`judge_prompt` bundle with NO recorded customisation anywhere replays
    under the engine default and stamps every prompt source ``None`` — the
    one-way ``BUNDLE ⟹ no-legacy-source`` implication on ``ReplayProvenance``
    admits this shape.

    The regression this test locks against is a mistaken biconditional
    formalisation of the implication: a strict ``iff`` (``bundle IFF no
    legacy source``) would reject the all-None shape a legitimate legacy
    default-prompt trial produces, and every such replay would red."""
    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_replay_bundle(
        trial_dir,
        judge_prompt=None,
        include_judge_prompt_key=False,
        customization=None,
    )

    client, dest = _replay(tmp_path, trial_dir)

    assert client.seen_system is not None
    assert client.seen_system.startswith(_JUDGE_SYSTEM_PROMPT)
    assert client.seen_system.count(_JUDGE_MARKER_CONTRACT) == 1

    provenance = _load_replay_provenance(dest)
    assert provenance["judge_prompt_source"] is None
    assert provenance["custom_prompt_source"] is None
    assert provenance["custom_system_prompt"] is False


# ---------------------------------------------------------------------------
# Negative-shape guards on the mutually exclusive prompt seams.
# ---------------------------------------------------------------------------


def test_llm_judge_construction_refuses_both_explicit_and_custom_system_prompts() -> None:
    """The two prompt seams carry distinct semantics (composed-verbatim vs.
    body-fragment-composed-at-run-time); passing both is a caller bug the
    constructor names at once, not a silent precedence rule downstream."""
    with pytest.raises(ValueError, match="explicit_system_prompt.*OR.*custom_system_prompt"):
        LLMJudge(
            ModelConfig(provider="openrouter", name="openai/gpt-4.1-mini", temperature=0.0),
            explicit_system_prompt="X",
            custom_system_prompt="Y",
        )


def test_replay_provenance_refuses_bundle_source_beside_a_legacy_source() -> None:
    """Stamping both a bundle source AND a legacy source together is a caller
    bug — the bundle-recorded composed prompt supersedes any legacy
    customization, so the two cannot coexist on a coherent stamp. The one-way
    validator rejects the impossible shape; the pre-existing
    ``_custom_prompt_fields_coherent`` biconditional it composes with still
    holds (both ``custom_system_prompt`` and ``custom_prompt_source`` name a
    legacy source, so they pass that check but fail this one)."""
    with pytest.raises(
        ValidationError,
        match="judge_prompt_source=BUNDLE forbids any legacy custom-prompt source",
    ):
        ReplayProvenance(
            judge_model="openrouter/openai/gpt-4.1-mini",
            judge_model_source=ProvenanceSource.RECORDED,
            rubric_source=ProvenanceSource.RECORDED,
            knowledge_search_mode=KnowledgeSearchMode.OFF,
            knowledge_search_disabled=True,
            custom_system_prompt=True,
            custom_prompt_source=ProvenanceSource.RECORDED,
            judge_prompt_source=ProvenanceSource.BUNDLE,
            include_agent_system_prompt=True,
            agent_prompt_source=None,
            fidelity_mode=FidelityMode.FULL,
        )
