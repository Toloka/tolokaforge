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
from tolokaforge.runner.models import (
    RUBRIC_CRITERIA_CHUNKED_THRESHOLD,
    Criterion,
    LLMJudgeConfig,
    Rubric,
)

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


def _rubric_of_size(count: int) -> Rubric:
    return Rubric(
        criteria=[
            Criterion(id=f"c_{i}", description=f"criterion {i}", kind="binary")
            for i in range(count)
        ]
    )


def test_omitted_judge_kind_stays_single_shot_below_threshold() -> None:
    """Small rubrics keep the pre-threshold default so the byte-shape lock
    on shipping task packs (all currently below the threshold) doesn't
    move under this policy."""
    small = RUBRIC_CRITERIA_CHUNKED_THRESHOLD - 1

    config = LLMJudgeConfig(rubric=_rubric_of_size(small))

    assert config.judge_kind == "single_shot_rubric"


def test_omitted_judge_kind_auto_upgrades_to_chunked_at_threshold() -> None:
    """A rubric whose criterion count meets the threshold gets
    ``chunked_rubric`` when the pack did not choose a kind — this is what
    prevents ``submit_report`` overflow on large rubrics (GH #1524)."""
    at_threshold = RUBRIC_CRITERIA_CHUNKED_THRESHOLD

    config = LLMJudgeConfig(rubric=_rubric_of_size(at_threshold))

    assert config.judge_kind == "chunked_rubric"


def test_explicit_single_shot_on_large_rubric_is_honoured() -> None:
    """A pack that names ``single_shot_rubric`` explicitly on a rubric the
    policy would otherwise auto-upgrade keeps its choice — the auto-select
    is a default resolver, never an override."""
    large = RUBRIC_CRITERIA_CHUNKED_THRESHOLD + 5

    config = LLMJudgeConfig(rubric=_rubric_of_size(large), judge_kind="single_shot_rubric")

    assert config.judge_kind == "single_shot_rubric"


def test_explicit_chunked_on_small_rubric_is_honoured() -> None:
    """A pack that opts into ``chunked_rubric`` on a small rubric keeps it —
    below-threshold rubrics don't need chunking but authors can still
    request it for consistency or measurement."""
    small = 3

    config = LLMJudgeConfig(rubric=_rubric_of_size(small), judge_kind="chunked_rubric")

    assert config.judge_kind == "chunked_rubric"


def test_auto_upgrade_applies_when_validating_a_raw_dict_without_the_field() -> None:
    """The resolver fires on dict input too, so YAML-loaded packs that
    simply omit ``judge_kind`` reach the same effective config as
    programmatic construction."""
    payload = {"rubric": _rubric_of_size(RUBRIC_CRITERIA_CHUNKED_THRESHOLD).model_dump(mode="json")}

    parsed = LLMJudgeConfig.model_validate(payload)

    assert parsed.judge_kind == "chunked_rubric"


def test_serialized_config_round_trips_without_regressing_to_the_default() -> None:
    """A config that already carries an explicit ``judge_kind`` (from an
    earlier serialization) reparses to the same kind — the policy never
    silently overrides a stamped value."""
    original = LLMJudgeConfig(
        rubric=_rubric_of_size(RUBRIC_CRITERIA_CHUNKED_THRESHOLD),
        judge_kind="single_shot_rubric",
    )

    reparsed = LLMJudgeConfig.model_validate(original.model_dump(mode="json"))

    assert reparsed.judge_kind == "single_shot_rubric"
