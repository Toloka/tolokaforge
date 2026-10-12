"""Offline replay rebuilds the judge from the ``task.yaml`` block a real run recorded.

The conductor writes a ``resolved:`` preset fingerprint into every
``model_config.<role>`` block. Replay must drop exactly that key and rebuild the
judge's :class:`ModelConfig` from the rest, so this drives
:func:`read_replay_inputs` over a committed migration-corpus bundle rather than
a hand-written block.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from tolokaforge.core.grading.replay import ProvenanceSource, read_replay_inputs
from tolokaforge.core.models import RESOLVED_RECORD_KEY
from tolokaforge.core.output.artifacts import FileArtifactWriter

pytestmark = pytest.mark.canonical

_BUNDLE = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "migration_corpora"
    / "lot_ops_names_lot"
    / "lot_ops_corpus_haiku_20260812_132740_trial0"
)


def _recorded_judge_block() -> dict:
    return yaml.safe_load((_BUNDLE / "task.yaml").read_text())["model_config"]["judge"]


def test_the_corpus_judge_block_carries_the_resolved_record() -> None:
    assert isinstance(_recorded_judge_block()[RESOLVED_RECORD_KEY], dict)


def test_replay_rebuilds_the_recorded_judge_from_a_corpus_bundle(tmp_path: Path) -> None:
    trial_dir = tmp_path / _BUNDLE.name
    shutil.copytree(_BUNDLE, trial_dir)
    # Corpus bundles are trimmed of prompts.yaml; replay refuses a bundle without one.
    FileArtifactWriter().write_prompts(trial_dir, None, None)
    recorded = {k: v for k, v in _recorded_judge_block().items() if k != RESOLVED_RECORD_KEY}

    inputs = read_replay_inputs(trial_dir)

    assert inputs.provenance.judge_model_source is ProvenanceSource.RECORDED
    replayed = inputs.judge_model_config.model_dump(mode="json")
    assert {key: replayed[key] for key in recorded} == recorded
