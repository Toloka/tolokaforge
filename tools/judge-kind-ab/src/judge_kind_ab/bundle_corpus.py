"""Assemble :class:`ParityCorpusEntry` fixtures from real grade bundles.

Live A/B needs the exact judge-input evidence the runner would hand a
:class:`~tolokaforge.core.grading.judge_kinds._protocol.JudgeKind`, reconstructed
from a bundle produced by
:func:`~tolokaforge.core.grading.bundle.serialize_grade_bundle`. The
reconstruction mirrors
:meth:`~tolokaforge.core.grading.kinds.composite.CompositeGraderKind.evaluate`'s
offline recompute path: rehydrate the trajectory, re-encode it against the
task's system prompt via
:func:`~tolokaforge.core.grading.transcript_wire.encode_transcript_wire`, then
split the wire-encoded messages back into ``(agent_system_prompt, transcript)``
via :func:`~tolokaforge.core.grading.transcript_wire.split_leading_system_message`
— the same two calls the live dispatch composes, so a live-A/B run replays
byte-identical judge inputs.
"""

from __future__ import annotations

import json
from pathlib import Path

from tolokaforge.core.grading.bundle import load_grade_bundle
from tolokaforge.core.grading.judge_kinds.parity import ParityCorpusEntry
from tolokaforge.core.grading.state_diff import render_state_diff
from tolokaforge.core.grading.transcript_wire import (
    encode_transcript_wire,
    split_leading_system_message,
)
from tolokaforge.core.models.trajectory import Trajectory
from tolokaforge.runner.models import RunnerGradingConfig, TaskDescription


def corpus_entry_from_bundle(bundle_dir: Path) -> ParityCorpusEntry:
    """Reconstruct one :class:`ParityCorpusEntry` from a bundle directory.

    Fails loud (``ValueError`` naming the missing field) when
    ``grading_config.json`` carries no ``llm_judge.rubric`` block or the bundle
    carries no ``task_description.json`` part — both are silently-defaultable
    inputs a live A/B run must never paper over. The returned entry carries no
    ``judge_scripts``: live mode drives a real provider, not a cassette.
    """
    bundle = load_grade_bundle(bundle_dir)

    grading_config = RunnerGradingConfig.model_validate(
        json.loads(bundle.open_part("grading_config.json"))
    )
    if grading_config.llm_judge is None:
        raise ValueError(
            f"bundle {bundle_dir} grading_config.json is missing llm_judge.rubric: "
            "no llm_judge block is declared"
        )

    if not bundle.has_part("task_description.json"):
        raise ValueError(
            f"bundle {bundle_dir} is missing task_description.json: "
            "JudgeKind.evaluate requires agent_system_prompt, which this "
            "v1.1-optional part is the only source of"
        )
    task_description = TaskDescription.model_validate(
        json.loads(bundle.open_part("task_description.json"))
    )

    trajectory = Trajectory.model_validate(json.loads(bundle.open_part("trajectory.json")))
    wire_str = encode_transcript_wire(trajectory, task_description.system_prompt)
    llm_messages = json.loads(wire_str) if wire_str else []
    agent_system_prompt, transcript = split_leading_system_message(llm_messages)

    initial_state = json.loads(bundle.open_part("initial_state.json"))
    state_diff = None
    if initial_state:
        final_state = json.loads(bundle.open_part("final_state.json"))
        primary_keys = grading_config.state_checks.id_fields if grading_config.state_checks else {}
        state_diff = render_state_diff(initial_state, final_state, primary_keys=primary_keys)

    customization = grading_config.llm_judge.customization

    return ParityCorpusEntry(
        entry_id=bundle.manifest.trial_id,
        rubric=grading_config.llm_judge.rubric,
        agent_system_prompt=agent_system_prompt,
        transcript=transcript,
        state_diff=state_diff,
        disable_knowledge_search=bool(customization and customization.disable_knowledge_search),
        custom_system_prompt=customization.system_prompt if customization else None,
        include_agent_system_prompt=(
            customization.include_agent_system_prompt
            if customization and customization.include_agent_system_prompt is not None
            else True
        ),
        judge_scripts={},
    )


def load_corpus_from_run(bundles_dir: Path) -> list[tuple[str, ParityCorpusEntry]]:
    """Load every bundle under ``bundles_dir/<family>/<bundle>/``, tagged by family.

    ``bundles_dir``'s immediate subdirectories are task families (e.g. one per
    ``examples/native/...`` pack); each family directory's own immediate
    subdirectories are individual trial bundles (a directory containing
    ``manifest.json``). Both levels are sorted for reproducible report
    ordering, matching the multi-trial-per-family shape the live-A/B run
    consumes (the umbrella's acceptance bar is trials spread across families,
    not one bundle per family).
    """
    return [
        (family_dir.name, corpus_entry_from_bundle(bundle_dir))
        for family_dir in sorted(p for p in bundles_dir.iterdir() if p.is_dir())
        for bundle_dir in sorted(p for p in family_dir.iterdir() if p.is_dir())
    ]
