"""Load-time rejection of removed / malformed ``grading.yaml`` blocks, and of the
``migration.yaml`` sidecar beside them.

Every contract here surfaces at load time (``validate`` /
``validate_grading_yaml``), not deferred to run time, and each rejection is
asserted on its **remediation text** — for a removal, the message *is* the
migration:

* ``llm_judge``: the removed free-text ``rubric: str`` (+ ignored
  ``output_schema``) shape and the relocated ``model_ref`` are intentional,
  non-back-compatible breaks (docs/RUBRIC_GRADING_DESIGN.md), rejected with a
  message naming the field and showing the new shape.
* ``state_checks``: ``env_assertions`` and ``db_hash_check`` are removed —
  neither ever produced grading signal on either substrate — and are rejected
  naming the check that replaces each, while an inert declaration of either is
  dropped so a recorded bundle still loads; every other key the block does not
  declare is rejected naming the closest declared field and the accepted set.
* The ``llm_judge.customization`` block (and its project-defaults twin
  ``grading_defaults.llm_judge.customization``) rejects malformed values and
  unknown keys, the message naming the offending field.
* ``combine``: the aggregation ``method`` is a closed set, and the two retired
  names that were declared but never dispatched are rejected naming the rule each
  one meant; a block that is not a mapping is rejected naming the file, the key and
  the shape received; a key the block does not declare is rejected naming the closest
  declared field and the accepted set.
* ``transcript_rules``: a turn window whose ``min_assistant_turns`` floor sits above
  its ``max_turns`` ceiling admits no assistant-turn count, and is rejected on both
  substrates' models naming both keys and both values; a key the block does not
  declare is rejected on both models too.
* ``trace_checks``: the whole matcher vocabulary is validated here, so a constraint
  that could only ever select nothing is rejected before a trial is paid for; a
  block that is not a mapping is rejected naming the file, the key and the shape.
* The ``migration.yaml`` sidecar: ``validate`` reads it, so a declaration its pack
  contradicts is heard here rather than by whoever reads the migration report. It is
  the one gate that does, because the file cannot affect a grade and a run must not
  abort on authoring metadata.

Every gate here is about the block's own shape, so the calls pass an unresolvable
tool inventory: none of these rejections has anything to do with the task's tools,
and the value that reports "nothing about the tools is checkable" is what makes that
explicit. The calls outside a ``pytest.raises`` are the standing lock that an
unresolvable inventory fails nothing — folding ``unchecked`` into the fatal channels
reddens every one of them. What a *resolvable* inventory rejects is locked in
``tests/unit/grading/test_grading_authoring_gate.py``.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner
from pydantic import ValidationError

from tests.utils.trace_checks_configs import every_kind_block
from tolokaforge.adapters._task_loader import validate_grading_yaml
from tolokaforge.core.grading.combine_method import (
    COMBINE_METHODS,
    RETIRED_COMBINE_METHOD_ALIASES,
)
from tolokaforge.core.grading.config_validation import ToolInventory
from tolokaforge.core.models import GradingCombineConfig, GradingDefaults, StateChecksConfig
from tolokaforge.core.models import TranscriptRulesConfig as CoreTranscriptRules
from tolokaforge.core.project_loader import resolve_effective_grading_combine
from tolokaforge.dx.cli.main import cli
from tolokaforge.runner.models import RunnerTranscriptRulesConfig as RunnerTranscriptRules

pytestmark = pytest.mark.unit

_UNRESOLVED = ToolInventory.unresolvable()


_TASK_YAML = textwrap.dedent("""
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
    """).strip()


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


def test_validate_rejects_env_assertions(tmp_path: Path):
    """``state_checks.env_assertions`` is removed; validate must name the replacements."""
    task_file = _write_task(
        tmp_path / "removed_env_assertions",
        """
        combine:
          method: weighted
          weights:
            state_checks: 1.0
        state_checks:
          env_assertions:
            - env_type: user
              func_name: assert_order_cancelled
              arguments:
                order_id: O1
        """,
    )

    result = CliRunner(mix_stderr=False).invoke(cli, ["validate", "--tasks", str(task_file)])

    out = result.stderr
    assert "0 valid, 1 invalid" in out
    assert "state_checks.env_assertions has been removed" in out
    # The message is the migration: every replacement is named, and named as an
    # alternative rather than as a sibling — the three sources do not compose, so a
    # snippet printing them under one block would hand the author a config no substrate
    # loads.
    for replacement in ("state_checks.jsonpaths", "state_checks.hash", "state_checks.db_probes"):
        assert replacement in out, out
    assert "pick one" in out, out


def test_validate_rejects_db_hash_check(tmp_path: Path):
    """``state_checks.db_hash_check`` is removed; validate must name hash grading."""
    task_file = _write_task(
        tmp_path / "removed_db_hash_check",
        """
        combine:
          method: weighted
          weights:
            state_checks: 1.0
        state_checks:
          db_hash_check: true
        """,
    )

    result = CliRunner(mix_stderr=False).invoke(cli, ["validate", "--tasks", str(task_file)])

    out = result.stderr
    assert "0 valid, 1 invalid" in out
    assert "state_checks.db_hash_check has been removed" in out
    assert "enabled: true" in out


def test_removed_state_check_keys_are_rejected_at_grading_load(tmp_path: Path):
    """The rejection lives on the model, so the run path fails too — not just validate.

    The message naming the replacement is why these two keys are answered here rather
    than by the model's ``extra="forbid"``, which knows of no replacement to name.
    """
    with pytest.raises(ValueError, match="env_assertions has been removed"):
        StateChecksConfig(env_assertions=[{"env_type": "user", "func_name": "assert_x"}])
    with pytest.raises(ValueError, match="db_hash_check has been removed"):
        StateChecksConfig(db_hash_check=True)


def test_inert_removed_keys_still_load(tmp_path: Path):
    """An empty declaration requests nothing, so it is dropped rather than rejected.

    Recorded trial bundles serialize the full grading config, including these keys at
    their old defaults; re-reading such a bundle must not raise. Dropped rather than
    returned untouched, because the model's ``extra="forbid"`` reads whatever the
    before-validator hands back — and asserted at the gate too, which refuses a key the
    block does not declare before the model is ever constructed.
    """
    config = StateChecksConfig(env_assertions=[], db_hash_check=False, jsonpaths=[])

    assert not hasattr(config, "env_assertions")
    assert not hasattr(config, "db_hash_check")

    validate_grading_yaml(
        _write_grading(
            tmp_path, {"env_assertions": [], "db_hash_check": False, "jsonpaths": _ASSERTIONS}
        ),
        inventory=_UNRESOLVED,
    )


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
            system_prompt: "Grade strictly against the policy handbook."
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
    grading.write_text(textwrap.dedent("""
            llm_judge:
              rubric:
                criteria:
                  - id: a
                    description: d
              output_schema:
                type: object
            """).strip())

    with pytest.raises(ValueError, match="output_schema has been removed"):
        validate_grading_yaml(grading, inventory=_UNRESOLVED)


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
        ("system_prompt: 123", "system_prompt"),
        ('system_prompt: ""', "empty"),
        ("include_agent_system_prompt: sometimes", "include_agent_system_prompt"),
    ],
    ids=[
        "malformed_value",
        "unknown_key",
        "malformed_system_prompt_type",
        "empty_system_prompt",
        "malformed_include_agent_system_prompt_type",
    ],
)
def test_validate_grading_yaml_rejects_malformed_customization(
    tmp_path: Path, customization_block: str, match: str
):
    grading = tmp_path / "grading.yaml"
    grading.write_text(textwrap.dedent(_grading_with_customization(customization_block)).strip())

    with pytest.raises(Exception, match=match):
        validate_grading_yaml(grading, inventory=_UNRESOLVED)


def test_validate_grading_yaml_skips_customization_without_rubric(tmp_path: Path):
    """Without a rubric, ``validate_grading_yaml`` never constructs ``LLMJudgeConfig``,
    so a malformed customization is NOT caught here — the reason the rejection
    fixtures above must carry a valid rubric to avoid a false green."""
    grading = tmp_path / "grading.yaml"
    grading.write_text(textwrap.dedent("""
            llm_judge:
              customization:
                disable_knowledge_search: sometimes
            """).strip())

    # No rubric and no model_ref, so LLMJudgeConfig is never constructed and the
    # malformed customization is never seen.
    validate_grading_yaml(grading, inventory=_UNRESOLVED)


@pytest.mark.parametrize(
    "customization, match",
    [
        ({"disable_knowledge_search": "sometimes"}, "disable_knowledge_search"),
        ({"typo_key": True}, "typo_key"),
        ({"system_prompt": 123}, "system_prompt"),
        ({"system_prompt": ""}, "empty"),
        ({"include_agent_system_prompt": "sometimes"}, "include_agent_system_prompt"),
    ],
    ids=[
        "malformed_value",
        "unknown_key",
        "malformed_system_prompt_type",
        "empty_system_prompt",
        "malformed_include_agent_system_prompt_type",
    ],
)
def test_project_grading_defaults_reject_malformed_customization(customization: dict, match: str):
    """The project-defaults layer (``grading_defaults.llm_judge.customization``) is
    locked at project-config parse time. ``GradingDefaults`` has no ``extra="forbid"``,
    so the rejection rests entirely on ``LLMJudgeDefaults`` / ``JudgeCustomization``
    each carrying it."""
    with pytest.raises(Exception, match=match):
        GradingDefaults(llm_judge={"customization": customization})


# ---------------------------------------------------------------------------
# state_checks.hash.weight — rejected at validate time, not at grade time
# ---------------------------------------------------------------------------


_ASSERTIONS = [{"path": "$.db.widgets[0].status", "equals": "closed"}]
_GOLDEN_ACTIONS = [{"name": "close_widget"}]


def _write_grading(tmp_path: Path, state_checks: dict) -> Path:
    """Serialise the block rather than indenting a string: a mis-indented ``hash``
    lands as a sibling of ``state_checks`` and every rejection here false-greens."""
    grading = tmp_path / "grading.yaml"
    grading.write_text(
        yaml.safe_dump(
            {
                "combine": {
                    "method": "weighted",
                    "weights": {"state_checks": 1.0},
                    "pass_threshold": 1.0,
                },
                "state_checks": state_checks,
            }
        )
    )
    return grading


@pytest.mark.parametrize(
    "hash_block",
    [
        {"enabled": True, "expected_state_hash": "aaaa"},
        {"enabled": True, "golden_actions": _GOLDEN_ACTIONS},
    ],
    ids=["expected_state_hash", "golden_actions"],
)
def test_validate_rejects_a_two_source_pack_with_no_weight(tmp_path: Path, hash_block: dict):
    """Validate is where an author hears this: the engine's own model is not
    constructed until artifacts are written, so a run-time-only gate would report
    an undecidable score after the trial had already been paid for."""
    grading = _write_grading(tmp_path, {"jsonpaths": _ASSERTIONS, "hash": hash_block})
    with pytest.raises(ValueError, match="state_checks.hash.weight is required"):
        validate_grading_yaml(grading, inventory=_UNRESOLVED)


@pytest.mark.parametrize(
    "state_checks",
    [
        {
            "jsonpaths": _ASSERTIONS,
            "hash": {"enabled": True, "expected_state_hash": "aaaa", "weight": 0.6},
        },
        {"jsonpaths": _ASSERTIONS, "hash": {"enabled": False}},
        {"jsonpaths": _ASSERTIONS, "hash": {"enabled": False, "weight": 0.6}},
        {"jsonpaths": [], "hash": {"enabled": True, "golden_actions": _GOLDEN_ACTIONS}},
        {
            "jsonpaths": [],
            "hash": {"enabled": True, "expected_state_hash": "aaaa", "weight": 1.0},
        },
    ],
    ids=[
        "weight_declared",
        "hash_off",
        "hash_off_with_an_inert_weight",
        "no_assertions_to_weigh",
        "recorded_tau_bundle_shape",
    ],
)
def test_validate_accepts_every_shape_the_weight_cannot_decide(tmp_path: Path, state_checks: dict):
    """The gate must stay narrow. Each shape here would demand a number the
    composer never reads, and the last one is the recorded tau bundle: rejecting
    it would make three committed trial bundles unloadable."""
    validate_grading_yaml(_write_grading(tmp_path, state_checks), inventory=_UNRESOLVED)


@pytest.mark.parametrize(
    "hash_block",
    [
        {"enabled": False, "expected_state_hash": "aaaa"},
        {"enabled": False, "expected_state_hash": "aaaa", "weight": 0.6},
        {"expected_state_hash": "aaaa"},
    ],
    ids=["flag_off", "flag_off_with_an_inert_weight", "flag_absent"],
)
def test_validate_rejects_a_hash_the_flag_stops_anything_from_reading(
    tmp_path: Path, hash_block: dict
):
    """A declared expectation nothing compares against is graded as if unwritten.

    Both substrates test ``enabled`` before reading the hash, so the pack scores its
    state on the JSONPath assertions alone and the author is never told. The weight
    rule is untouched: the same block still constructs, which is what makes this the
    file gate's rejection rather than a widening of the model's.
    """
    StateChecksConfig(jsonpaths=_ASSERTIONS, hash=hash_block)

    grading = _write_grading(tmp_path, {"jsonpaths": _ASSERTIONS, "hash": hash_block})
    with pytest.raises(ValueError, match="the comparison never runs"):
        validate_grading_yaml(grading, inventory=_UNRESOLVED)


def test_validate_rejects_a_flag_with_nothing_to_compare_against(tmp_path: Path):
    """The converse, and the one shape whose two substrates decide it differently.

    Core produces no hash verdict for a source it cannot find while the runner
    compares the trial against its initial state, so the pack's ``state_checks``
    component depends on where it was graded. The file gate is what removes the
    shape: the model still constructs it, because the weight predicate's real-source
    condition is intact and a weight there would divide a verdict that does not
    exist — dropping that condition instead would refuse this pack with the *weight*
    message, for a number nothing reads.
    """
    hash_block = {"enabled": True}
    StateChecksConfig(jsonpaths=_ASSERTIONS, hash=hash_block)

    grading = _write_grading(tmp_path, {"jsonpaths": _ASSERTIONS, "hash": hash_block})
    with pytest.raises(ValueError, match="declares no expected_state_hash or golden_actions"):
        validate_grading_yaml(grading, inventory=_UNRESOLVED)


@pytest.mark.parametrize("weight", [2.0, -0.1])
def test_validate_rejects_a_weight_outside_the_unit_interval(tmp_path: Path, weight: float):
    grading = _write_grading(
        tmp_path,
        {
            "jsonpaths": _ASSERTIONS,
            "hash": {"enabled": True, "expected_state_hash": "aaaa", "weight": weight},
        },
    )
    with pytest.raises(ValueError, match=r"state_checks.hash.weight must be a real number"):
        validate_grading_yaml(grading, inventory=_UNRESOLVED)


# ---------------------------------------------------------------------------
# state_checks' own field names — every declared block, not only a hash-declaring one
#
# The gate constructs the model on any declared block, so every rule it carries is an
# authoring answer rather than one a pack first hears at grade time with the trial
# already paid for.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state_checks", "match"),
    [
        (
            {"jsonpaths": _ASSERTIONS, "jsonpaths_typo": _ASSERTIONS},
            "unknown key 'jsonpaths_typo'",
        ),
        ({"jsonpaths": _ASSERTIONS, "id_fields": {"widgets": ""}}, "must be a non-empty key field"),
        ({"jsonpaths": _ASSERTIONS, True: 1}, "grading keys must be strings"),
    ],
    ids=["a_misspelled_key", "a_rule_the_model_always_carried", "a_key_yaml_read_as_a_boolean"],
)
def test_validate_gates_a_state_checks_block_that_declares_no_hash(
    tmp_path: Path, state_checks: dict, match: str
):
    """Every arm here loaded clean while only a hash-declaring block reached the model.

    The second is the one an out-of-tree pack is likelier to meet: not a typo but a rule
    ``StateChecksConfig`` always carried, which such a pack was always violating and now
    hears about at ``validate`` instead of at grade time. The third is not a misspelling
    at all — YAML reads a bare ``on:`` / ``no:`` as a boolean and a bare ``3:`` as an int
    — and the did-you-mean suggester raises ``TypeError: 'bool' object is not iterable``
    on one, so the refusal partitions such a key out and says to quote it instead.
    """
    with pytest.raises(ValueError, match=match):
        validate_grading_yaml(_write_grading(tmp_path, state_checks), inventory=_UNRESOLVED)


def test_validate_names_a_misspelled_state_checks_key_and_the_accepted_set(tmp_path: Path):
    """A dropped key was silent whenever a real source survived beside it.

    A typo *alone* in this block already drew the asserts-nothing refusal, which names
    neither the key nor the fix; a typo beside a declared ``jsonpaths`` drew nothing at
    all, and the assertions the author wrote under it were never compared. Measured on
    a block with no surviving source beside a weighted sibling: ``1.0`` and passing,
    where the assertion the author wrote scored ``0.5`` and failing.
    """
    grading = _write_grading(
        tmp_path, {"jsonpaths": _ASSERTIONS, "jsonpath": [{"path": "$.db.widgets[0].owner"}]}
    )

    with pytest.raises(ValueError) as excinfo:
        validate_grading_yaml(grading, inventory=_UNRESOLVED)

    message = str(excinfo.value)
    assert str(grading) in message, message
    assert "unknown key 'jsonpath'" in message, message
    assert "did you mean 'jsonpaths'?" in message, message
    assert "the task's own state_checks block" in message, message
    for field in StateChecksConfig.model_fields:
        assert field in message, message


def test_a_populated_retired_key_draws_its_migration_and_not_the_unknown_key_refusal(
    tmp_path: Path,
):
    """The two answers are disjoint, and only one of them names a replacement.

    A retired key is not a key the author misspelled — it is one that was removed, and
    the message that names what to write instead is the whole point of keeping it. A
    refusal calling it unknown would answer one mistake with two contradicting
    sentences, one of them useless.
    """
    grading = _write_grading(
        tmp_path, {"env_assertions": [{"env_type": "user", "func_name": "assert_x"}]}
    )

    with pytest.raises(ValueError) as excinfo:
        validate_grading_yaml(grading, inventory=_UNRESOLVED)

    message = str(excinfo.value)
    assert "state_checks.env_assertions has been removed" in message, message
    assert "unknown key" not in message, message


# ---------------------------------------------------------------------------
# combine.method — the aggregation an author names, rejected at validate time
#
# This gate closes the bad-*value* half of the combine typo space; the misspelled
# *key* half (``combine: {methd: any}``) is the section after it.
# ---------------------------------------------------------------------------


_WEIGHTS = {"state_checks": 1.0}


def _write_combine(tmp_path: Path, combine: object) -> Path:
    """Serialise the block: an indented string fixture lands ``method`` outside
    ``combine`` and every rejection below false-greens."""
    grading = tmp_path / "grading.yaml"
    grading.write_text(yaml.safe_dump({"combine": combine}))
    return grading


@pytest.mark.parametrize(("alias", "replacement"), tuple(RETIRED_COMBINE_METHOD_ALIASES.items()))
def test_validate_rejects_a_retired_combine_method_naming_its_replacement(
    tmp_path: Path, alias: str, replacement: str
):
    """Asserted on the two things a bare ``Literal`` cannot say.

    Pydantic's own ``literal_error`` already quotes the offending value and the whole
    permitted set, so an assertion on those would pass with this gate's alias-aware
    validator deleted.
    """
    grading = _write_combine(tmp_path, {"method": alias, "weights": _WEIGHTS})

    with pytest.raises(ValueError) as excinfo:
        validate_grading_yaml(grading, inventory=_UNRESOLVED)

    message = str(excinfo.value)
    assert f"Use {replacement!r}" in message, message
    assert "never worked" in message, message


@pytest.mark.parametrize("method", ["bogus_method_xyz", "min", "Weighted", ""])
def test_validate_rejects_an_unsupported_combine_method_listing_the_supported_set(
    tmp_path: Path, method: str
):
    grading = _write_combine(tmp_path, {"method": method, "weights": _WEIGHTS})

    with pytest.raises(ValueError) as excinfo:
        validate_grading_yaml(grading, inventory=_UNRESOLVED)

    message = str(excinfo.value)
    for supported in COMBINE_METHODS:
        assert repr(supported) in message, message


@pytest.mark.parametrize("method", COMBINE_METHODS)
def test_validate_accepts_every_declared_combine_method(tmp_path: Path, method: str):
    validate_grading_yaml(
        _write_combine(tmp_path, {"method": method, "weights": _WEIGHTS}), inventory=_UNRESOLVED
    )


def test_validate_accepts_a_combine_block_that_names_no_method(tmp_path: Path):
    """A partial block declares only what it overrides, and the field keeps its default.

    ``examples/native/example-microservices-pack/tasks/long_debugging_session`` ships
    exactly this shape over a project-level default, so a gate reading the key instead
    of constructing the block would reject a pack that grades today.
    """
    validate_grading_yaml(_write_combine(tmp_path, {"pass_threshold": 0.7}), inventory=_UNRESOLVED)


@pytest.mark.parametrize(
    "combine",
    [
        {"method": "weighted", "pass_threshold": "high"},
        {"method": "weighted", "weights": {"state_checks": "most"}},
    ],
    ids=["pass_threshold", "weights"],
)
def test_validate_rejects_a_malformed_combine_block_beside_a_valid_method(
    tmp_path: Path, combine: dict
):
    """The gate constructs the whole block, so its siblings are validated too.

    A gate narrowed to the one field would leave these two loading, which is what
    makes this the lock for constructing ``GradingCombineConfig`` rather than
    reaching past it to ``method``.
    """
    with pytest.raises(ValueError):
        validate_grading_yaml(_write_combine(tmp_path, combine), inventory=_UNRESOLVED)


@pytest.mark.parametrize(
    "combine",
    [[{"method": "all_pass"}], "weighted", 3],
    ids=["list", "string", "number"],
)
def test_validate_rejects_a_combine_block_that_is_not_a_mapping(tmp_path: Path, combine: object):
    """The shape half of the same typo space, which the value gate reads past.

    A block skipped here reports the pack valid and then fails the run inside
    ``deep_merge`` with ``'list' object has no attribute 'items'``, naming neither the
    file nor the key — and a retired method inside such a block never gets read at all.
    """
    grading = _write_combine(tmp_path, combine)

    with pytest.raises(RuntimeError) as excinfo:
        validate_grading_yaml(grading, inventory=_UNRESOLVED)

    message = str(excinfo.value)
    assert str(grading) in message, message
    assert "'combine'" in message, message
    assert type(combine).__name__ in message, message


def test_validate_accepts_a_combine_key_with_nothing_under_it(tmp_path: Path):
    """``combine:`` alone is the absent block, not a malformed one: every field falls
    through to its default, which is what the loader's own merge does with it."""
    validate_grading_yaml(_write_combine(tmp_path, None), inventory=_UNRESOLVED)


def test_validate_cli_reports_a_retired_combine_method_as_invalid(tmp_path: Path):
    """The author-facing gate: ``tolokaforge validate`` is where a typo is heard."""
    task_file = _write_task(
        tmp_path / "retired_combine_method",
        yaml.safe_dump({"combine": {"method": "all_pass", "weights": _WEIGHTS}}),
    )

    result = CliRunner(mix_stderr=False).invoke(cli, ["validate", "--tasks", str(task_file)])

    out = result.stderr
    assert "0 valid, 1 invalid" in out
    assert "Use 'all'" in out
    assert "never worked" in out


# ---------------------------------------------------------------------------
# combine's own field names — a key the block does not declare, refused at load
#
# Every field in the block has a default a dropped key silently substituted, so the
# refusal is on the model (total over every construction path) and its author-facing
# message is at the gate, the only tier that knows the file.
# ---------------------------------------------------------------------------


_COMBINE_FIELDS = ("method", "weights", "pass_threshold")


def test_the_combine_block_refuses_a_key_it_does_not_declare():
    """On the model, so ``project.yaml`` load and direct Python are refused too.

    Which key the refusal names, and nothing about the grade: what a dropped key did to
    one is asserted at the merge that folded it, in the test below.
    """
    with pytest.raises(ValidationError) as excinfo:
        GradingCombineConfig(pass_treshold=0.95)

    assert [error["loc"] for error in excinfo.value.errors()] == [("pass_treshold",)]


def test_a_misspelled_threshold_no_longer_resolves_to_the_default():
    """The value consequence, at the merge a run is graded by rather than at the model.

    The silent substitution is the regression: this resolution answered ``0.8`` for a
    pack asking for ``0.95``, and every consumer downstream read the wrong threshold as
    the author's own.
    """
    assert resolve_effective_grading_combine(None, {"pass_threshold": 0.95}).pass_threshold == 0.95

    with pytest.raises(ValidationError):
        resolve_effective_grading_combine(None, {"pass_treshold": 0.95})


def test_validate_names_the_key_its_closest_field_and_the_accepted_set(tmp_path: Path):
    """What the model's own ``extra_forbidden`` cannot say: the file, and the fix."""
    grading = _write_combine(tmp_path, {"pass_treshold": 0.95, "weights": _WEIGHTS})

    with pytest.raises(ValueError) as excinfo:
        validate_grading_yaml(grading, inventory=_UNRESOLVED)

    message = str(excinfo.value)
    assert str(grading) in message, message
    assert "'pass_treshold'" in message, message
    assert "did you mean 'pass_threshold'?" in message, message
    for field in _COMBINE_FIELDS:
        assert field in message, message
    assert "the task's own combine block" in message, message


def test_validate_cli_reports_a_misspelled_combine_key_as_invalid(tmp_path: Path):
    """The author-facing surface, where the whole message has to arrive."""
    task_file = _write_task(
        tmp_path / "misspelled_combine_key",
        yaml.safe_dump({"combine": {"pass_treshold": 0.95, "weights": _WEIGHTS}}),
    )

    result = CliRunner(mix_stderr=False).invoke(cli, ["validate", "--tasks", str(task_file)])

    out = " ".join(result.stderr.split())
    assert "0 valid, 1 invalid" in out
    assert "unknown key 'pass_treshold'" in out
    assert "did you mean 'pass_threshold'?" in out
    assert "combine accepts: method, weights, pass_threshold." in out


# ---------------------------------------------------------------------------
# transcript_rules — the turn window, rejected at validate time on both substrates
#
# A floor above the ceiling admits no assistant-turn count, so the component is
# 0.0 however the agent behaves. Heard at validate time rather than after a trial
# has been run and paid for.
# ---------------------------------------------------------------------------


_TRANSCRIPT_MODELS = (CoreTranscriptRules, RunnerTranscriptRules)
_MODEL_IDS = ("core", "runner")


def _write_transcript_rules(tmp_path: Path, transcript_rules: object) -> Path:
    """Serialise the block beside a valid ``combine``, so a rejection below is the
    transcript gate's and not the combine gate's."""
    grading = tmp_path / "grading.yaml"
    grading.write_text(
        yaml.safe_dump(
            {
                "combine": {"method": "weighted", "weights": {"transcript_rules": 1.0}},
                "transcript_rules": transcript_rules,
            }
        )
    )
    return grading


@pytest.mark.parametrize("model", _TRANSCRIPT_MODELS, ids=_MODEL_IDS)
@pytest.mark.parametrize(
    ("floor", "ceiling"),
    [(5, 3), (4, 3), (2, 1)],
    ids=["clear", "off_by_one", "ceiling_of_one"],
)
def test_both_substrates_reject_an_unsatisfiable_turn_window(model: type, floor: int, ceiling: int):
    """One predicate, both models: the engine and the runner agree on what loads.

    Asserted on the paired key-and-value text rather than the bare digits — a
    message naming only the bound it tripped leaves the author guessing which of
    the two keys to move, and digits alone appear in any pydantic error.
    """
    with pytest.raises(ValueError) as excinfo:
        model(min_assistant_turns=floor, max_turns=ceiling)

    message = str(excinfo.value)
    assert f"min_assistant_turns ({floor})" in message, message
    assert f"max_turns ({ceiling})" in message, message


@pytest.mark.parametrize("model", _TRANSCRIPT_MODELS, ids=_MODEL_IDS)
@pytest.mark.parametrize(
    "transcript_rules",
    [
        {"min_assistant_turns": 3, "max_turns": 3},
        {"min_assistant_turns": 1, "max_turns": 5},
        {"min_assistant_turns": 9},
        {"max_turns": 1},
        {"must_contain": ["shipped"]},
    ],
    ids=["floor_equals_ceiling", "all_keys_pack_shape", "floor_alone", "ceiling_alone", "neither"],
)
def test_both_substrates_accept_every_window_a_trial_can_land_in(
    model: type, transcript_rules: dict
):
    """The gate must stay narrow: only a pack declaring *both* can close the window.

    ``floor_equals_ceiling`` is satisfied by exactly that many turns, and
    ``all_keys_pack_shape`` is what ``tests/data/grading_parity/all_keys`` ships — the
    corpus shape a gate reading either key on its own would reject.
    """
    model(**transcript_rules)


@pytest.mark.parametrize("model", _TRANSCRIPT_MODELS, ids=_MODEL_IDS)
@pytest.mark.parametrize(
    "transcript_rules",
    [{"min_assistant_turns": 0}, {"max_turns": 0}],
    ids=["floor_below_the_domain", "ceiling_below_the_domain"],
)
def test_both_substrates_reject_a_turn_bound_below_the_domain(model: type, transcript_rules: dict):
    """Each bound is declarable from 1 up, on the wire model as well as the core one.

    A floor of 0 asserts nothing, and the runtime key ledger tests a declared key by
    truthiness, so it would be an unpoliced declaration. A ceiling below 1 is the
    same defect from the other side: it closes the window on its own, so every trial
    fails the transcript component whatever the agent did — the outcome the window
    gate rejects when a pack writes both keys.
    """
    with pytest.raises(ValueError, match="greater than or equal to 1"):
        model(**transcript_rules)


def test_validate_rejects_a_pack_whose_turn_window_admits_nothing(tmp_path: Path):
    """Validate is where an author hears this.

    Without the gate the pack loads clean and every trial fails the transcript
    component at grade time — after the tokens are spent.
    """
    grading = _write_transcript_rules(tmp_path, {"min_assistant_turns": 5, "max_turns": 3})

    with pytest.raises(ValueError, match="unsatisfiable turn window"):
        validate_grading_yaml(grading, inventory=_UNRESOLVED)


def test_validate_accepts_the_corpus_turn_window(tmp_path: Path):
    validate_grading_yaml(
        _write_transcript_rules(tmp_path, {"min_assistant_turns": 1, "max_turns": 5}),
        inventory=_UNRESOLVED,
    )


@pytest.mark.parametrize(
    "transcript_rules",
    [[{"max_turns": 3}], "max_turns: 3", 3],
    ids=["list", "string", "number"],
)
def test_validate_rejects_a_transcript_rules_block_that_is_not_a_mapping(
    tmp_path: Path, transcript_rules: object
):
    """The shape half of the typo space the window gate reads past.

    ``isinstance(..., dict)`` alone reports such a pack valid, and every rule the
    block was meant to carry then grades as unset — a closed turn window included.
    The same shape ``combine`` carries in
    ``test_validate_rejects_a_combine_block_that_is_not_a_mapping``.
    """
    grading = _write_transcript_rules(tmp_path, transcript_rules)

    with pytest.raises(RuntimeError) as excinfo:
        validate_grading_yaml(grading, inventory=_UNRESOLVED)

    message = str(excinfo.value)
    assert str(grading) in message, message
    assert "'transcript_rules'" in message, message
    assert type(transcript_rules).__name__ in message, message


@pytest.mark.parametrize(
    ("transcript_rules", "match"),
    [
        ({"min_assistant_turns": 0}, "greater than or equal to 1"),
        ({"tool_expectations": {"required_toolz": ["db_update"]}}, "required_toolz"),
        (
            {"must_contain": ["shipped"], "must_contian": ["refunded"]},
            "unknown key 'must_contian'",
        ),
    ],
    ids=["floor_below_the_domain", "misspelled_tool_expectations_key", "misspelled_rule_key"],
)
def test_validate_rejects_a_malformed_transcript_block_beside_a_valid_window(
    tmp_path: Path, transcript_rules: dict, match: str
):
    """The gate constructs the whole block, so its siblings are validated too.

    A gate that read the two window fields off the raw dict and compared them
    directly — never constructing the model — would leave all three of these loading: a
    floor of 0 asserting nothing, a misspelled ``tool_expectations`` key grading as an
    empty list, and a misspelled rule key at the top of the block doing the same one
    level up.
    """
    with pytest.raises(ValueError, match=match):
        validate_grading_yaml(
            _write_transcript_rules(tmp_path, transcript_rules), inventory=_UNRESOLVED
        )


@pytest.mark.parametrize("model", _TRANSCRIPT_MODELS, ids=_MODEL_IDS)
def test_both_substrates_refuse_a_rule_key_the_block_does_not_declare(model: type):
    """One answer, both models: the engine and the runner agree on what loads.

    Every rule here defaults to asserting nothing, so the dropped key graded the trial
    by the rules that survived — measured, ``must_contian: ['refund issued']`` scored
    ``1.0`` and passing on a transcript the authored ``must_contain`` scored ``0.75``
    and failing. The runner model has refused this since it was written; the engine's
    is what a ``grading.yaml`` is read by.
    """
    with pytest.raises(ValidationError) as excinfo:
        model(must_contian=["refund issued"])

    assert [error["loc"] for error in excinfo.value.errors()] == [("must_contian",)]


def test_validate_names_a_misspelled_transcript_rule_key_and_the_accepted_set(tmp_path: Path):
    """What the model's own ``extra_forbidden`` cannot say: the file, and the fix.

    Written beside a rule that survives, which is the shape nothing caught: a typo alone
    in this block already drew the asserts-nothing refusal, and that message names
    neither the key nor what to write instead.
    """
    grading = _write_transcript_rules(
        tmp_path, {"must_contain": ["shipped"], "must_contian": ["refunded"]}
    )

    with pytest.raises(ValueError) as excinfo:
        validate_grading_yaml(grading, inventory=_UNRESOLVED)

    message = str(excinfo.value)
    assert str(grading) in message, message
    assert "did you mean 'must_contain'?" in message, message
    assert "the task's own transcript_rules block" in message, message
    for field in CoreTranscriptRules.model_fields:
        assert field in message, message


def test_validate_cli_reports_an_unsatisfiable_turn_window_as_invalid(tmp_path: Path):
    task_file = _write_task(
        tmp_path / "unsatisfiable_turn_window",
        yaml.safe_dump({"transcript_rules": {"min_assistant_turns": 5, "max_turns": 3}}),
    )

    result = CliRunner(mix_stderr=False).invoke(cli, ["validate", "--tasks", str(task_file)])

    out = result.stderr
    assert "0 valid, 1 invalid" in out
    assert "min_assistant_turns (5)" in out
    assert "max_turns (3)" in out


_A_GOLDEN_REPLAY = """
    combine:
      method: weighted
      weights:
        state_checks: 1.0
    state_checks:
      hash:
        enabled: true
        golden_actions:
          - name: http_request
    """

# A task whose golden path has a world to be replayed in except for the server module:
# ``initial_state.json`` is written beside it, and ``http_request`` is a builtin the task
# declares, so the only fact left for a rejection to be about is ``mcp_server``.
_A_TASK_WITHHOLDING_ITS_SERVER_MODULE = textwrap.dedent("""
    task_id: golden_replay_probe
    description: "Probe task for golden-replay world validation."
    initial_state:
      json_db: "initial_state.json"
    tools:
      agent:
        enabled: ["http_request"]
    grading: "grading.yaml"
    """).strip()


def test_validate_cli_reports_a_golden_replay_with_no_world_as_invalid(tmp_path: Path):
    """``validate`` reads the task, not only the block it grades by.

    The two facts a golden replay is executed against live in ``task.yaml``, so the
    command resolves them for the gate; resolve nothing and this pack validates clean and
    then raises inside the grading engine, once a trial is already paid for. Driven through
    the CLI rather than through ``validate_grading_yaml`` because the resolving is the
    command's own — the gate itself is exercised in
    ``tests/unit/grading/test_grading_authoring_gate.py``.
    """
    task_dir = tmp_path / "golden_replay_with_no_world"
    task_dir.mkdir()
    (task_dir / "initial_state.json").write_text("{}")
    (task_dir / "task.yaml").write_text(_A_TASK_WITHHOLDING_ITS_SERVER_MODULE)
    (task_dir / "grading.yaml").write_text(textwrap.dedent(_A_GOLDEN_REPLAY).strip())

    result = CliRunner(mix_stderr=False).invoke(
        cli, ["validate", "--tasks", str(task_dir / "task.yaml")]
    )

    assert result.exit_code != 0
    out = " ".join(result.stderr.split())
    assert "0 valid, 1 invalid" in out
    assert "this task declares no tools.agent.mcp_server" in out
    assert "state_checks.hash.golden_actions" in out


# ---------------------------------------------------------------------------
# trace_checks — the block is constructed, so the whole matcher vocabulary is
# validated at validate time
#
# The rejection wording is locked per rule in
# tests/unit/grading/test_trace_checks_config.py. What belongs here is the gate
# itself: that validate reaches the block at all, and that a block which is not a
# mapping is rejected naming the file, the key and the shape received.
# ---------------------------------------------------------------------------


def _write_trace_checks(tmp_path: Path, trace_checks: object) -> Path:
    """Serialise the block beside a valid ``combine``, so a rejection below is the
    trace gate's and not the combine gate's."""
    grading = tmp_path / "grading.yaml"
    grading.write_text(
        yaml.safe_dump(
            {
                "combine": {"method": "weighted", "weights": {"trace_checks": 1.0}},
                "trace_checks": trace_checks,
            }
        )
    )
    return grading


def test_validate_accepts_a_well_formed_trace_checks_block(tmp_path: Path):
    validate_grading_yaml(_write_trace_checks(tmp_path, every_kind_block()), inventory=_UNRESOLVED)


@pytest.mark.parametrize(
    ("trace_checks", "match"),
    [
        (
            {"constraints": [{"id": "a", "description": "d", "require": {}}]},
            "exactly one",
        ),
        (
            {
                "constraints": [
                    {
                        "id": "a",
                        "description": "d",
                        "require": {
                            "present": {"match": {"kind": "user_message", "tool": {"equals": "x"}}}
                        },
                    }
                ]
            },
            "carries no",
        ),
        ({"constraints": []}, "declares neither constraints nor alternatives"),
    ],
    ids=["no_constraint_kind", "field_the_kind_never_carries", "no_constraints"],
)
def test_validate_rejects_a_malformed_trace_checks_block(
    tmp_path: Path, trace_checks: dict, match: str
):
    """Validate is where an author hears this.

    Without the gate the pack loads clean and the matcher selects nothing at grade
    time, which the default ``on_missing`` reports as the agent failing — after the
    tokens are spent.
    """
    with pytest.raises(ValueError, match=match):
        validate_grading_yaml(_write_trace_checks(tmp_path, trace_checks), inventory=_UNRESOLVED)


@pytest.mark.parametrize(
    "trace_checks",
    [[{"id": "a"}], "constraints: []", 3],
    ids=["list", "string", "number"],
)
def test_validate_rejects_a_trace_checks_block_that_is_not_a_mapping(
    tmp_path: Path, trace_checks: object
):
    """A constraint written directly under ``trace_checks:`` makes the block a list.

    ``isinstance(..., dict)`` alone reports such a pack valid and every constraint
    the block was meant to carry grades as unset. The same shape ``combine`` and
    ``transcript_rules`` carry.

    The remediation names **both** keys the block may carry: an author whose
    alternatives-only pack lost its indentation is told to write the key that pack
    actually has, not the one it does not.
    """
    grading = _write_trace_checks(tmp_path, trace_checks)

    with pytest.raises(RuntimeError) as excinfo:
        validate_grading_yaml(grading, inventory=_UNRESOLVED)

    message = str(excinfo.value)
    assert str(grading) in message, message
    assert "'trace_checks'" in message, message
    assert type(trace_checks).__name__ in message, message
    assert "'constraints:' or 'alternatives:'" in message, message


# ---------------------------------------------------------------------------
# migration.yaml — the sidecar is reached by validate, and by nothing a run pays for
#
# Every rule the declaration carries is locked in
# tests/unit/grading/test_migration_declaration.py and
# tests/canonical/test_migration_declaration.py. What belongs here is the gate: that
# ``validate`` reads the file beside a task's grading.yaml at all, so a mis-authored
# declaration is heard before a trial is run rather than by whoever reads the
# migration report later.
# ---------------------------------------------------------------------------


_MIGRATED_GRADING = yaml.safe_dump(
    {
        "combine": {"method": "weighted", "weights": {"llm_judge": 1.0}},
        "llm_judge": {
            "rubric": {
                "criteria": [
                    {"id": "checked_first", "description": "It checked first.", "required": True}
                ]
            }
        },
    }
)


def test_validate_cli_reports_a_mis_authored_migration_sidecar_as_invalid(tmp_path: Path):
    """The entry names a criterion the rubric does not declare, which no other gate reads."""
    task_dir = tmp_path / "mis_authored_migration"
    task_file = _write_task(task_dir, _MIGRATED_GRADING)
    (task_dir / "migration.yaml").write_text(
        yaml.safe_dump(
            {
                "migrations": [
                    {
                        "criterion": "never_declared",
                        "mode": "candidate",
                        "by": ["nothing_declares_this_either"],
                        "was": {"description": "Something the rubric never said."},
                    }
                ]
            }
        )
    )

    result = CliRunner(mix_stderr=False).invoke(cli, ["validate", "--tasks", str(task_file)])

    out = result.stderr
    assert "0 valid, 1 invalid" in out
    assert "names no criterion in the pack's rubric" in out


def test_validate_cli_reports_a_task_carrying_no_sidecar_as_valid(tmp_path: Path):
    """The file is optional, so the gate's accepting direction is every pack that has
    none — which is every pack in the tree until one declares a migration."""
    task_file = _write_task(tmp_path / "no_migration_sidecar", _MIGRATED_GRADING)

    result = CliRunner(mix_stderr=False).invoke(cli, ["validate", "--tasks", str(task_file)])

    assert "1 valid, 0 invalid" in result.stderr
