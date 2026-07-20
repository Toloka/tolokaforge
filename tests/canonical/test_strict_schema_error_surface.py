"""Pins the loader's unknown-key warning message shape across the four
Project-layer YAML roots so a regression that drops the file name, the
offending key, or the difflib closest-match suggestion fails loud.

The message renders the source as its **basename** — the snapshot must be
machine-independent, so a fixed filename is passed for ``source`` rather
than a ``tmp_path`` absolute path. Only top-level keys are checked; a
nested unknown key is dropped without a warning (see the unit suite).
"""

from pathlib import Path

import pytest

from tolokaforge.core.models import GradingConfig, ProjectConfig, RunConfig, TaskConfig
from tolokaforge.core.project_loader import construct_config

pytestmark = pytest.mark.canonical


def _warning(model, data: dict, source: str, section: str = "") -> str:
    with pytest.warns(DeprecationWarning) as record:
        construct_config(model, data, source=Path(source), section=section)
    messages = [str(w.message) for w in record if str(w.message).startswith("unknown key")]
    assert messages, "expected an unknown-key DeprecationWarning"
    return messages[0]


def test_unknown_key_warning_message_shape(canon_snapshot) -> None:
    messages = {
        "run_config": _warning(
            RunConfig,
            {"models": {}, "orchestrator": {}, "evaluation": {"output_dir": "x"}, "computee": {}},
            "run_configs/dev.yaml",
        ),
        "project": _warning(
            ProjectConfig,
            {"name": "demo", "discription": "typo"},
            "project.yaml",
        ),
        "task": _warning(
            TaskConfig,
            {"task_id": "t", "description": "d", "max_turnss": 5},
            "task.yaml",
        ),
        "grading": _warning(
            GradingConfig,
            {"combine": {}, "transcript_ruless": {"must_contain": ["x"]}},
            "grading.yaml",
            section="grading",
        ),
    }
    canon_snapshot("strict_schema_error_surface").assert_match(messages, "messages.json")
