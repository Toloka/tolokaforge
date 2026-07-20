"""Pins the loader's unknown-key error message shape across the four
Project-layer YAML roots so a regression that drops the file name, the
offending key path, or the difflib closest-match suggestion fails loud.

The message renders the source as its **basename** — the snapshot must be
machine-independent, so a fixed filename is passed for ``source`` rather
than a ``tmp_path`` absolute path.
"""

from pathlib import Path

import pytest

from tolokaforge.core.models import GradingConfig, ProjectConfig, RunConfig, TaskConfig
from tolokaforge.core.project_loader import construct_config

pytestmark = pytest.mark.canonical


def _error(model, data: dict, source: str, section: str = "") -> str:
    with pytest.raises(RuntimeError) as excinfo:
        construct_config(model, data, source=Path(source), section=section)
    return str(excinfo.value)


def test_unknown_key_error_message_shape(canon_snapshot) -> None:
    messages = {
        "run_config_top_level": _error(
            RunConfig,
            {"models": {}, "orchestrator": {}, "evaluation": {"output_dir": "x"}, "computee": {}},
            "run_configs/dev.yaml",
        ),
        "run_config_nested": _error(
            RunConfig,
            {"models": {}, "orchestrator": {"mox_turns": 5}, "evaluation": {"output_dir": "x"}},
            "run_configs/dev.yaml",
        ),
        "project": _error(
            ProjectConfig,
            {"name": "demo", "discription": "typo"},
            "project.yaml",
        ),
        "task": _error(
            TaskConfig,
            {"task_id": "t", "description": "d", "max_turnss": 5},
            "task.yaml",
        ),
        "grading": _error(
            GradingConfig,
            {"combine": {}, "transcript_rules": {"must_contain_phrase": ["x"]}},
            "grading.yaml",
            section="grading",
        ),
    }
    canon_snapshot("strict_schema_error_surface").assert_match(messages, "messages.json")
