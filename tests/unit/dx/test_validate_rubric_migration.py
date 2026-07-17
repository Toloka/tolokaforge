"""Load-time schema rejection of the ``grading.llm_judge`` block.

Two contracts are locked here, both surfacing at load time (``validate`` /
``validate_grading_yaml``), not deferred to run time:

* The removed free-text ``rubric: str`` (+ ignored ``output_schema``) shape is
  an intentional, non-back-compatible break (docs/RUBRIC_GRADING_DESIGN.md) —
  rejected with a message that names the field and shows the new shape.
* The ``llm_judge.customization`` block (and its project-defaults twin
  ``grading_defaults.llm_judge.customization``) rejects malformed values and
  unknown keys, the message naming the offending field.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from tolokaforge.adapters._task_loader import validate_grading_yaml
from tolokaforge.core.models import GradingDefaults
from tolokaforge.dx.cli.main import cli

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

    # validate reports per-task pass/fail via the shared stderr console and
    # exits 0; the task must be flagged invalid with the migration message
    # naming rubric + the new shape.
    out = result.stderr
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

    out = result.stderr
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
    assert "1 valid, 0 invalid" in result.stderr


def test_validate_accepts_customization_block(tmp_path: Path):
    """A well-formed ``llm_judge.customization`` block passes ``tolokaforge validate``."""
    task_file = _write_task(
        tmp_path / "customized_rubric",
        """
        combine:
          method: weighted
          weights:
            llm_judge: 1.0
        llm_judge:
          customization:
            disable_knowledge_search: true
          rubric:
            criteria:
              - id: refund_amount
                description: "Reply quotes the correct refund amount"
                kind: binary
                weight: 1.0
        """,
    )

    result = CliRunner(mix_stderr=False).invoke(cli, ["validate", "--tasks", str(task_file)])
    assert "1 valid, 0 invalid" in result.stderr


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


# ---------------------------------------------------------------------------
# llm_judge.customization schema rejection — task + project layers
# ---------------------------------------------------------------------------


def _grading_with_customization(customization_block: str) -> str:
    """A valid rubric grading.yaml carrying a customization sub-block. The rubric
    is REQUIRED: ``validate_grading_yaml`` constructs ``LLMJudgeConfig`` only when a
    rubric (or ``model_ref``) is present, so without it the malformed block is never
    validated and the check false-greens."""
    return f"""
    llm_judge:
      rubric:
        criteria:
          - id: a
            description: d
            kind: binary
            weight: 1.0
      customization:
    {textwrap.indent(textwrap.dedent(customization_block).strip(), "        ")}
    """


@pytest.mark.parametrize(
    "customization_block, match",
    [
        ("disable_knowledge_search: sometimes", "disable_knowledge_search"),
        ("typo_key: true", "typo_key"),
    ],
    ids=["malformed_value", "unknown_key"],
)
def test_validate_grading_yaml_rejects_malformed_customization(
    tmp_path: Path, customization_block: str, match: str
):
    grading = tmp_path / "grading.yaml"
    grading.write_text(textwrap.dedent(_grading_with_customization(customization_block)).strip())

    with pytest.raises(Exception, match=match):
        validate_grading_yaml(grading)


def test_validate_grading_yaml_skips_customization_without_rubric(tmp_path: Path):
    """Without a rubric, ``validate_grading_yaml`` never constructs ``LLMJudgeConfig``,
    so a malformed customization is NOT caught here — the reason the rejection
    fixtures above must carry a valid rubric to avoid a false green."""
    grading = tmp_path / "grading.yaml"
    grading.write_text(
        textwrap.dedent(
            """
            llm_judge:
              customization:
                disable_knowledge_search: sometimes
            """
        ).strip()
    )

    validate_grading_yaml(grading)  # no rubric/model_ref → no validation, no raise


@pytest.mark.parametrize(
    "customization, match",
    [
        ({"disable_knowledge_search": "sometimes"}, "disable_knowledge_search"),
        ({"typo_key": True}, "typo_key"),
    ],
    ids=["malformed_value", "unknown_key"],
)
def test_project_grading_defaults_reject_malformed_customization(customization: dict, match: str):
    """The project-defaults layer (``grading_defaults.llm_judge.customization``) is
    locked at project-config parse time. ``GradingDefaults`` has no ``extra="forbid"``,
    so the rejection rests entirely on ``LLMJudgeDefaults`` / ``JudgeCustomization``
    each carrying it."""
    with pytest.raises(Exception, match=match):
        GradingDefaults(llm_judge={"customization": customization})
