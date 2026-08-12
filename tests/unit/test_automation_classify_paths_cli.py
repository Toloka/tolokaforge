"""Unit tests for ``automation classify-paths`` CLI shape.

Drives the typer app via ``CliRunner`` and locks:

- Bucket A / Bucket B JSON shape via stdin.
- Empty stdin → Bucket A with ``reason == "empty diff"``.
- ``--format plain`` output shape.
- Conflicting or missing input flags exit 2 (typer's BadParameter default).
"""

from __future__ import annotations

import json

import pytest
from automation.cli import app
from typer.testing import CliRunner

pytestmark = pytest.mark.unit

_BUCKET_A_STDIN = (
    "tolokaforge_models/src/tolokaforge_models/data/model_presets.yaml\n"
    "tolokaforge_models/src/tolokaforge_models/certificates/registry.py\n"
)
_BUCKET_B_EXTRA_ENGINE_PATH = "tolokaforge/core/llm/schema_sanitizer.py"


def test_bucket_a_stdin_classifies_as_a() -> None:
    result = CliRunner().invoke(
        app, ["classify-paths", "--paths-from-stdin"], input=_BUCKET_A_STDIN
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["bucket"] == "A"
    assert payload["engine_paths"] == []
    assert payload["reason"] == "all touched paths in Bucket-A allow-list"


def test_bucket_b_stdin_classifies_as_b_with_engine_paths() -> None:
    stdin = _BUCKET_A_STDIN + _BUCKET_B_EXTRA_ENGINE_PATH + "\n"
    result = CliRunner().invoke(app, ["classify-paths", "--paths-from-stdin"], input=stdin)
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["bucket"] == "B"
    assert payload["engine_paths"] == [_BUCKET_B_EXTRA_ENGINE_PATH]
    assert payload["reason"] == "1 engine-side path(s) outside Bucket-A allow-list"


def test_empty_stdin_classifies_as_a_empty_diff() -> None:
    result = CliRunner().invoke(app, ["classify-paths", "--paths-from-stdin"], input="")
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload == {"bucket": "A", "reason": "empty diff", "engine_paths": []}


def test_format_plain_emits_bash_greppable_lines() -> None:
    result = CliRunner().invoke(
        app,
        ["classify-paths", "--paths-from-stdin", "--format", "plain"],
        input=_BUCKET_A_STDIN,
    )
    assert result.exit_code == 0, result.stdout
    lines = result.stdout.strip().splitlines()
    assert lines[0] == "bucket=A"
    assert lines[1] == "reason=all touched paths in Bucket-A allow-list"
    assert lines[2] == "engine_paths="


def test_conflicting_input_flags_exit_two() -> None:
    result = CliRunner().invoke(
        app, ["classify-paths", "--paths-from-stdin", "--paths-from-diff", "HEAD"]
    )
    assert result.exit_code == 2


def test_no_input_flag_exits_two() -> None:
    result = CliRunner().invoke(app, ["classify-paths"])
    assert result.exit_code == 2


def test_unknown_format_exits_two() -> None:
    result = CliRunner().invoke(
        app, ["classify-paths", "--paths-from-stdin", "--format", "yaml"], input=""
    )
    assert result.exit_code == 2
