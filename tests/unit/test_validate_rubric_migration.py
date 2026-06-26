"""`tolokaforge validate` rejects the pre-Stage-2 free-text rubric shape.

Rubric grading moved from a free-text ``rubric: str`` (+ ignored
``output_schema``) blob to a structured ``Rubric``. The migration is an
intentional, non-back-compatible break (docs/RUBRIC_GRADING_DESIGN.md). ``validate``
must catch the old shape at validate time with a message that names the field
and shows the new shape — not defer the failure to run time.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from tolokaforge.adapters._task_loader import validate_grading_yaml
from tolokaforge.cli.main import cli

pytestmark = pytest.mark.unit


_TASK_YAML = textwrap.dedent(
    """
    task_id: rubric_migration_probe
    name: "Rubric migration probe"
    category: test
    description: "Probe task for rubric migration validation."
    initial_state:
      json_db: null
    tools:
      agent:
        enabled: []
      user:
        enabled: []
    user_simulator:
      mode: "scripted"
      scripted_flow:
        - role: "user"
          content: "hi"
    grading: "grading.yaml"
    """
).strip()


def _write_task(task_dir: Path, grading_yaml: str) -> Path:
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.yaml").write_text(_TASK_YAML)
    (task_dir / "grading.yaml").write_text(textwrap.dedent(grading_yaml).strip())
    return task_dir / "task.yaml"


def test_validate_rejects_legacy_rubric_str(tmp_path: Path):
    task_file = _write_task(
        tmp_path / "legacy_rubric",
        """
        combine:
          method: weighted
          weights:
            llm_judge: 1.0
        llm_judge:
          rubric: "Grade the reply for correctness and tone."
          output_schema:
            type: object
        """,
    )

    result = CliRunner(mix_stderr=False).invoke(cli, ["validate", "--tasks", str(task_file)])

    # validate reports per-task pass/fail to stdout and exits 0; the task must be
    # flagged invalid with the migration message naming rubric + the new shape.
    out = result.output
    assert "1 valid, 0 invalid" not in out
    assert "0 valid, 1 invalid" in out
    assert "rubric is now a structured Rubric" in out
    assert "criteria:" in out


def test_validate_rejects_legacy_model_ref(tmp_path: Path):
    """The judge model relocated to the run config (models.judge); a stray
    ``llm_judge.model_ref`` in grading.yaml must fail validate with a migration
    message that names where the model now lives."""
    task_file = _write_task(
        tmp_path / "legacy_model_ref",
        """
        combine:
          method: weighted
          weights:
            llm_judge: 1.0
        llm_judge:
          model_ref: "openai/gpt-4o-mini"
          rubric:
            criteria:
              - id: refund_amount
                description: "Reply quotes the correct refund amount"
                kind: binary
                weight: 1.0
        """,
    )

    result = CliRunner(mix_stderr=False).invoke(cli, ["validate", "--tasks", str(task_file)])

    out = result.output
    assert "0 valid, 1 invalid" in out
    assert "model_ref moved to the run config" in out


def test_validate_accepts_structured_rubric(tmp_path: Path):
    task_file = _write_task(
        tmp_path / "structured_rubric",
        """
        combine:
          method: weighted
          weights:
            llm_judge: 1.0
        llm_judge:
          rubric:
            reference: "Correct refund is $328.50."
            criteria:
              - id: refund_amount
                description: "Reply quotes the correct refund amount"
                expected: "$328.50"
                kind: binary
                required: true
                weight: 1.0
        """,
    )

    result = CliRunner(mix_stderr=False).invoke(cli, ["validate", "--tasks", str(task_file)])
    assert "1 valid, 0 invalid" in result.output


def test_validate_grading_yaml_rejects_removed_output_schema(tmp_path: Path):
    """The output_schema field is gone; its presence is a loud migration error."""
    grading = tmp_path / "grading.yaml"
    grading.write_text(
        textwrap.dedent(
            """
            llm_judge:
              rubric:
                criteria:
                  - id: a
                    description: d
              output_schema:
                type: object
            """
        ).strip()
    )

    with pytest.raises(ValueError, match="output_schema has been removed"):
        validate_grading_yaml(grading)
