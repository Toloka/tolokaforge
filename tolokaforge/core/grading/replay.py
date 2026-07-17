"""Offline judge replay — reconstruct ``LLMJudge.run()`` inputs from a recorded bundle.

Replay is a *caller* of the one production judge (:meth:`LLMJudge.run`), never a
second judge implementation. It reads a trial bundle written by the eval flow,
rebuilds the exact ``run()`` inputs, and re-executes the rubric-judge stage with
real judge spend but no access to the original run's live services. Live read
tools (DB, knowledge base, workspace) are replaced by offline shims that return an
explicit "unavailable in replay" marker so the judge grades knowing what it could
not inspect — never a silent empty result. See docs/JUDGE_REPLAY.md.

Trial classification is the batch-selection predicate: a trial is *judge-eligible*
iff its recorded ``grade.yaml`` carried a judge stage (``judge_status`` not
``unspecified``). Trials that never had a judge are *not-applicable* and are never
judged and never fail-loud — not even under a rubric override, which would spend
real tokens on a task that never had a judge stage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from tolokaforge.core.grading.judge import (
    DBReader,
    JudgeResult,
    LLMJudge,
    model_config_from_ref,
)
from tolokaforge.core.grading.kb_search import KnowledgeSearch, SearchHit
from tolokaforge.core.llm.client import LLMClient
from tolokaforge.core.models import JudgeInputs, JudgeStatus, ModelConfig, Trajectory
from tolokaforge.core.trial_grader import _build_judge_messages_json, split_leading_system_message
from tolokaforge.runner.models import LLMJudgeConfig, Rubric
from tolokaforge.tools.registry import Tool, ToolCategory, ToolPolicy, ToolResult

__all__ = [
    "REPLAY_UNAVAILABLE",
    "FidelityMode",
    "KnowledgeSearchMode",
    "MissingReplayInputError",
    "OfflineDBReader",
    "OfflineKnowledgeSearch",
    "ProvenanceSource",
    "ReplayInputs",
    "ReplayProvenance",
    "TrialEligibility",
    "classify_trial",
    "load_grading_rubric",
    "read_replay_inputs",
    "replay_trial",
]

#: Prefix of the marker every offline read tool returns in place of live data.
REPLAY_UNAVAILABLE = "unavailable in replay"


def _unavailable(backend: str) -> str:
    return f"{REPLAY_UNAVAILABLE}: {backend}"


class KnowledgeSearchMode(str, Enum):
    """Tri-state KB-gating control for replay, mirroring ``--knowledge-search``.

    ``RECORDED`` honours the bundle's recorded ``judge_kb_gating`` gating;
    ``ON`` forces the judge to be offered KB tools; ``OFF`` forces them withheld.
    """

    RECORDED = "recorded"
    ON = "on"
    OFF = "off"


class TrialEligibility(str, Enum):
    """Whether a recorded trial is in scope for re-judging."""

    ELIGIBLE = "eligible"
    NOT_APPLICABLE = "not_applicable"


class FidelityMode(str, Enum):
    """How faithfully the judge's opening view is reconstructed.

    ``FULL`` — the bundle recorded the structured ``state_diff`` (a Stage-1
    ``judge_inputs.yaml``), so the opening message is rebuilt exactly. ``FALLBACK``
    — an old bundle lacking structured inputs: the opening message omits the
    ``state_diff``, so it will not byte-reproduce a state-diff-influenced verdict.
    """

    FULL = "full"
    FALLBACK = "fallback"


class ProvenanceSource(str, Enum):
    """Whether a resolved replay input came from the bundle or a CLI override."""

    RECORDED = "recorded"
    OVERRIDE = "override"


class MissingReplayInputError(ValueError):
    """A judge-eligible trial cannot be reconstructed because a required input is
    missing and no override was supplied. The message names exactly what is
    missing. Never raised for a not-applicable trial (that is a declared skip)."""


class OfflineDBReader:
    """:class:`DBReader` shim for offline replay.

    Every read returns an explicit "unavailable in replay" marker instead of live
    DB state — never a silent empty ``{}`` — so the judge's transcript records that
    it could not inspect the database. Reconstructing real recorded state from
    ``env.yaml`` is deferred (issue #525); v1 declares the unavailability.
    """

    def get_state(self, tables: list[str] | None = None) -> dict[str, Any]:
        return {"replay_unavailable": _unavailable("db_state")}

    def query(self, jsonpath: str) -> dict[str, Any]:
        return {"replay_unavailable": _unavailable("db_query")}


class OfflineKnowledgeSearch:
    """:class:`KnowledgeSearch` shim for offline replay.

    Returns a single hit carrying the "unavailable in replay" marker rather than an
    empty list, so a KB-blind replay is visible in the judge's transcript instead of
    reading as "no documents found".
    """

    def search(self, query: str, top_k: int = 5, alpha: float = 0.5) -> list[SearchHit]:
        return [
            SearchHit(
                doc_id="replay-unavailable",
                source="replay",
                score=0.0,
                text=_unavailable("knowledge_search"),
            )
        ]


class _OfflineReadTool(Tool):
    """A read tool offered to match the recorded surface but backed offline.

    Any call returns the explicit "unavailable in replay" marker. Used for the
    ``read_file`` workspace reader and any non-``search_kb`` KB passthrough (e.g.
    ``search_policy``) the judge was recorded to have been offered.
    """

    def __init__(self, name: str, backend: str, *, is_knowledge_search: bool = False) -> None:
        super().__init__(
            name=name,
            description=f"[replay] {name}: {_unavailable(backend)}",
            policy=ToolPolicy(timeout_s=15.0, category=ToolCategory.READ, visibility=["agent"]),
        )
        self._backend = backend
        self.is_knowledge_search = is_knowledge_search

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": {}},
            },
        }

    def execute(self, **_: Any) -> ToolResult:
        return ToolResult(success=True, output=_unavailable(self._backend))


class ReplayProvenance(BaseModel):
    """Stamp of how a replay's inputs were resolved, for persistence in Stage 3.

    Records the judge model actually used and, per input, whether it came from the
    recorded bundle or a CLI override, plus the fidelity mode. Consumed by the
    replay artifact writer so a reviewer can tell a full-fidelity replay from a
    degraded one at a glance.
    """

    judge_model: str
    judge_model_source: ProvenanceSource
    rubric_source: ProvenanceSource
    knowledge_search_mode: KnowledgeSearchMode
    knowledge_search_disabled: bool
    fidelity_mode: FidelityMode

    model_config = {"extra": "forbid"}


@dataclass(frozen=True)
class ReplayInputs:
    """The exact ``LLMJudge.run()`` inputs reconstructed for one recorded trial,
    plus the resolved judge model / gating and the provenance stamp."""

    rubric: Rubric
    agent_system_prompt: str
    transcript: list[dict[str, Any]]
    state_diff: str | None
    judge_model_config: ModelConfig
    disable_knowledge_search: bool
    db_reader: DBReader | None = None
    kb_search: KnowledgeSearch | None = None
    extra_read_tools: list[Tool] = field(default_factory=list)
    workspace_dir: Path | None = None
    provenance: ReplayProvenance | None = None


def _load_yaml(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    loaded = yaml.safe_load(path.read_text())
    return loaded if isinstance(loaded, dict) else None


def load_grading_rubric(grading_path: Path) -> Rubric:
    """Parse a supplied ``grading.yaml`` override into its :class:`Rubric`.

    Accepts either a full grading document (``llm_judge.rubric: …``) or a bare
    ``rubric:`` mapping, so an operator can point ``--grading`` at a task's grading
    file or a hand-authored rubric snippet. Fails loud if neither carries a rubric.
    """
    data = _load_yaml(grading_path)
    if data is None:
        raise MissingReplayInputError(f"grading override {grading_path} is not a YAML mapping")
    llm_judge = data.get("llm_judge")
    if isinstance(llm_judge, dict) and "rubric" in llm_judge:
        return LLMJudgeConfig.model_validate(llm_judge).rubric
    if "rubric" in data:
        return Rubric.model_validate(data["rubric"])
    raise MissingReplayInputError(
        f"grading override {grading_path} has no llm_judge.rubric or rubric block"
    )


def classify_trial(trial_dir: Path) -> TrialEligibility:
    """Classify a recorded trial as judge-eligible or not-applicable.

    Eligible iff the recorded ``grade.yaml`` carried a judge stage
    (``judge_status`` not ``unspecified``). Independent of any rubric override — a
    trial that never had a judge is never conjured into one.
    """
    grade = _load_yaml(trial_dir / "grade.yaml")
    status_value = (grade or {}).get("judge_status") or JudgeStatus.UNSPECIFIED.value
    status = JudgeStatus(status_value)
    if status is JudgeStatus.UNSPECIFIED:
        return TrialEligibility.NOT_APPLICABLE
    return TrialEligibility.ELIGIBLE


def _resolve_rubric(
    task: dict[str, Any] | None, rubric_override: Rubric | None
) -> tuple[Rubric, ProvenanceSource]:
    if rubric_override is not None:
        return rubric_override, ProvenanceSource.OVERRIDE
    llm_judge = ((task or {}).get("grading_config") or {}).get("llm_judge")
    if isinstance(llm_judge, dict) and llm_judge.get("rubric") is not None:
        return LLMJudgeConfig.model_validate(llm_judge).rubric, ProvenanceSource.RECORDED
    raise MissingReplayInputError(
        "no rubric: the bundle's task.yaml has no grading_config.llm_judge.rubric "
        "and no --grading override was supplied"
    )


def _resolve_judge_model(
    task: dict[str, Any] | None, judge_model_override: str | None
) -> tuple[ModelConfig, ProvenanceSource]:
    if judge_model_override is not None:
        return model_config_from_ref(judge_model_override), ProvenanceSource.OVERRIDE
    recorded = ((task or {}).get("model_config") or {}).get("judge")
    if isinstance(recorded, dict):
        return ModelConfig.model_validate(recorded), ProvenanceSource.RECORDED
    raise MissingReplayInputError(
        "no judge model: the bundle's task.yaml has no model_config.judge and no "
        "--judge-model override was supplied"
    )


def _resolve_knowledge_search(mode: KnowledgeSearchMode, kb_gating: dict[str, Any] | None) -> bool:
    if mode is KnowledgeSearchMode.ON:
        return False
    if mode is KnowledgeSearchMode.OFF:
        return True
    return bool((kb_gating or {}).get("knowledge_search_disabled", False))


def _offline_read_surface(
    trial_dir: Path, judge_inputs: JudgeInputs | None, kb_gating: dict[str, Any] | None
) -> tuple[DBReader | None, KnowledgeSearch | None, list[Tool]]:
    """Reconstruct the offline read-tool surface to match the recorded one.

    New bundles carry the authoritative non-KB surface in ``judge_inputs.yaml``;
    the KB surface comes from ``grade.yaml`` ``judge_kb_gating``. Old bundles lack
    both — ``env.yaml`` presence is the only signal that the judge had DB reads.
    """
    db_reader: DBReader | None = None
    kb_search: KnowledgeSearch | None = None
    extra: list[Tool] = []

    if judge_inputs is not None:
        offered = set(judge_inputs.read_tools_offered)
        if offered & {"get_db_state", "query_db"}:
            db_reader = OfflineDBReader()
        if "read_file" in offered:
            extra.append(_OfflineReadTool("read_file", "workspace"))
    elif (trial_dir / "env.yaml").exists():
        db_reader = OfflineDBReader()

    for name in (kb_gating or {}).get("offered") or []:
        if name == "search_kb":
            kb_search = OfflineKnowledgeSearch()
        else:
            extra.append(_OfflineReadTool(name, name, is_knowledge_search=True))

    return db_reader, kb_search, extra


def read_replay_inputs(
    trial_dir: Path,
    *,
    rubric_override: Rubric | None = None,
    judge_model_override: str | None = None,
    knowledge_search: KnowledgeSearchMode = KnowledgeSearchMode.RECORDED,
) -> ReplayInputs:
    """Reconstruct the ``LLMJudge.run()`` inputs for one judge-eligible trial.

    Rebuilds the judge's transcript through the same
    :func:`_build_judge_messages_json` + :func:`split_leading_system_message` the
    runner uses, so the reconstructed transcript is byte-identical to the live
    grading path. Read tools are offline shims matching the recorded surface.

    Raises :class:`MissingReplayInputError`, naming what is missing, when the trial
    had a judge but the bundle lacks a required input and no override fills it (no
    rubric and no ``--grading``; no transcript; no judge model and no
    ``--judge-model``). Callers must classify the trial first — this is only valid
    for a judge-eligible trial.
    """
    task = _load_yaml(trial_dir / "task.yaml")
    grade = _load_yaml(trial_dir / "grade.yaml")
    prompts = _load_yaml(trial_dir / "prompts.yaml")
    trajectory_raw = _load_yaml(trial_dir / "trajectory.yaml")
    if trajectory_raw is None:
        raise MissingReplayInputError(f"no transcript: {trial_dir / 'trajectory.yaml'} is missing")

    rubric, rubric_source = _resolve_rubric(task, rubric_override)
    judge_model_config, judge_model_source = _resolve_judge_model(task, judge_model_override)

    trajectory = Trajectory.model_validate(trajectory_raw)
    recorded_agent_prompt = (prompts or {}).get("system_prompt") or ""
    wire = _build_judge_messages_json(trajectory, recorded_agent_prompt)
    if wire is None:
        raise MissingReplayInputError(
            f"no transcript: {trial_dir / 'trajectory.yaml'} has no messages and no agent prompt"
        )
    agent_system_prompt, transcript = split_leading_system_message(json.loads(wire))

    judge_inputs = _load_judge_inputs(trial_dir)
    kb_gating = (grade or {}).get("judge_kb_gating")
    disable_knowledge_search = _resolve_knowledge_search(knowledge_search, kb_gating)
    db_reader, kb_search, extra_read_tools = _offline_read_surface(
        trial_dir, judge_inputs, kb_gating
    )

    state_diff = judge_inputs.state_diff_text if judge_inputs is not None else None
    fidelity_mode = FidelityMode.FULL if judge_inputs is not None else FidelityMode.FALLBACK

    provenance = ReplayProvenance(
        judge_model=f"{judge_model_config.provider}/{judge_model_config.name}",
        judge_model_source=judge_model_source,
        rubric_source=rubric_source,
        knowledge_search_mode=knowledge_search,
        knowledge_search_disabled=disable_knowledge_search,
        fidelity_mode=fidelity_mode,
    )

    return ReplayInputs(
        rubric=rubric,
        agent_system_prompt=agent_system_prompt,
        transcript=transcript,
        state_diff=state_diff,
        judge_model_config=judge_model_config,
        disable_knowledge_search=disable_knowledge_search,
        db_reader=db_reader,
        kb_search=kb_search,
        extra_read_tools=extra_read_tools,
        workspace_dir=None,
        provenance=provenance,
    )


def _load_judge_inputs(trial_dir: Path) -> JudgeInputs | None:
    raw = _load_yaml(trial_dir / "judge_inputs.yaml")
    return JudgeInputs.model_validate(raw) if raw is not None else None


def replay_trial(inputs: ReplayInputs, *, judge_client: LLMClient | None = None) -> JudgeResult:
    """Re-execute the rubric-judge stage over reconstructed inputs.

    Constructs the one production :class:`LLMJudge` and calls its ``run()`` — no
    reimplementation of prompt construction, tool surface, validation, retry, or
    aggregation. ``judge_client`` injects a scripted client for tests (no network);
    production passes ``None`` and the judge builds its own client.
    """
    judge = LLMJudge(
        inputs.judge_model_config,
        disable_knowledge_search=inputs.disable_knowledge_search,
        llm_client=judge_client,
    )
    return judge.run(
        rubric=inputs.rubric,
        agent_system_prompt=inputs.agent_system_prompt,
        transcript=inputs.transcript,
        db_reader=inputs.db_reader,
        kb_search=inputs.kb_search,
        extra_read_tools=inputs.extra_read_tools or None,
        workspace_dir=inputs.workspace_dir,
        state_diff=inputs.state_diff,
    )
