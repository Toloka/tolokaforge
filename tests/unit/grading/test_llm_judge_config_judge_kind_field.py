"""``LLMJudgeConfig.judge_kind`` / ``kind_config`` — parse-time contract.

Locks three parse-time behaviours on the two new ``LLMJudgeConfig``
fields:

- The defaults preserve the prior shape byte-identically (any task-pack
  that never authored either field parses to the same effective config
  and dumps the same JSON body it always did — new keys emit with their
  defaults).
- An unknown ``judge_kind`` value is refused by Pydantic
  ``model_validate`` with a message that names both the offending value
  and the registered set — an actionable authoring signal that fails at
  parse time, not at run time.
- ``kind_config`` is accepted as an opaque ``dict[str, Any]`` — the
  framework performs no shape checks; the model round-trips whatever the
  author put in.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tolokaforge.core.plugin_registry import available_judge_kinds
from tolokaforge.runner.models import Criterion, LLMJudgeConfig, Rubric

pytestmark = pytest.mark.unit


def _rubric() -> Rubric:
    return Rubric(
        criteria=[
            Criterion(
                id="answer_correct",
                description="Agent answered the question",
                kind="binary",
            )
        ]
    )


def test_defaults_preserve_the_prior_shape() -> None:
    config = LLMJudgeConfig(rubric=_rubric())

    assert config.judge_kind == "single_shot_rubric"
    assert config.kind_config is None


def test_the_json_dump_carries_both_new_fields_at_their_defaults() -> None:
    """Byte-shape lock on ``model_dump(mode="json")`` — a future silent flip
    on either default surfaces here rather than in a downstream regression."""
    dumped = LLMJudgeConfig(rubric=_rubric()).model_dump(mode="json")

    assert dumped["judge_kind"] == "single_shot_rubric"
    assert dumped["kind_config"] is None


def test_an_old_task_pack_dict_without_either_new_field_parses_cleanly() -> None:
    """Old task packs / bundles that omit both fields keep round-tripping:
    the two new fields are additive with defaults, so omitting them yields
    the same effective config as authoring the defaults explicitly."""
    payload = {"rubric": _rubric().model_dump(mode="json")}

    parsed = LLMJudgeConfig.model_validate(payload)

    assert parsed.judge_kind == "single_shot_rubric"
    assert parsed.kind_config is None


def test_unknown_judge_kind_is_refused_at_parse_time_naming_the_registered_set() -> None:
    """The failure surfaces at ``model_validate`` — no code path below
    ``load_judge_kind`` is exercised. The error must name the offending
    typo AND every registered kind by name so the author sees what to
    write instead (asserted per-name so the check does not depend on the
    set's ordering)."""
    with pytest.raises(ValidationError) as excinfo:
        LLMJudgeConfig(rubric=_rubric(), judge_kind="not_a_real_kind")

    message = str(excinfo.value)
    assert "not_a_real_kind" in message
    for registered in available_judge_kinds():
        assert registered in message


def test_kind_config_is_accepted_as_an_opaque_dict() -> None:
    """The framework does no shape validation on ``kind_config`` — a nested
    heterogeneous payload round-trips verbatim through the model."""
    payload = {"key": "value", "nested": {"n": 1, "flags": [True, False]}}

    config = LLMJudgeConfig(rubric=_rubric(), kind_config=payload)

    assert config.kind_config == payload
