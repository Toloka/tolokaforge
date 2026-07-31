"""Single source of truth for which grading substrate consumes which author key.

Two substrates grade a trial: the in-process core :class:`~tolokaforge.core.grading.combine.GradingEngine`
and the runner's gRPC ``GradeTrial`` path. Every key a task author may write in
``grading.yaml`` is enumerated in :data:`GRADING_KEYS` together with the
substrate(s) that evaluate it, the evaluator that does so, the tier at which that
claim is proven, and — when only one substrate evaluates it — why.

``tests/canonical/test_grading_substrate_parity.py`` makes the enumeration
load-bearing: a key added to either substrate's config model without an entry
here fails that suite, and every entry claiming both substrates at
:attr:`Enforcement.DIFFERENTIAL_CANONICAL` must demonstrably move both
substrates' component scores.
"""

from dataclasses import dataclass
from enum import Enum


class KeyKind(str, Enum):
    """What role a key plays in producing a grade."""

    SCORED_CHECK = "SCORED_CHECK"
    """Produces a component score from a trajectory or a final state."""

    CONFIG_INPUT = "CONFIG_INPUT"
    """Shapes how another check behaves; carries no score of its own."""

    AGGREGATION = "AGGREGATION"
    """Combines component scores into the final score."""


class SubstrateCoverage(str, Enum):
    """Which substrates evaluate the key.

    Members starting with ``BOTH`` are matched by prefix, so the differential
    predicate can be expressed as ``coverage.startswith("BOTH")``.
    """

    BOTH_SCORE_PARITY = "BOTH_SCORE_PARITY"
    """Both substrates consume it and produce the same component score."""

    BOTH_SIGNAL_PARITY = "BOTH_SIGNAL_PARITY"
    """Both substrates consume it and both discriminate; their aggregation differs."""

    CORE_ONLY = "CORE_ONLY"
    RUNNER_ONLY = "RUNNER_ONLY"


class Enforcement(str, Enum):
    """How strongly the coverage claim is proven by the test suite."""

    DIFFERENTIAL_CANONICAL = "DIFFERENTIAL_CANONICAL"
    """A satisfying/violating differential runs in-process, no services."""

    DIFFERENTIAL_INTEGRATION = "DIFFERENTIAL_INTEGRATION"
    """The differential needs real services; ``enforcing_test`` names it."""

    FIELD_RESOLUTION_ONLY = "FIELD_RESOLUTION_ONLY"
    """Only "the field exists and resolves" is proven; requires a tracking issue
    unless the key is structurally not differentially testable."""


@dataclass(frozen=True)
class GradingKey:
    """One author-facing ``grading.yaml`` key and its substrate coverage.

    ``core_field`` / ``runner_field`` are dotted *model attribute* paths
    (``"StateChecksConfig.jsonpaths"``), ``None`` when that substrate does not
    declare the key at all. When the author key lives inside an untyped dict
    field, ``*_field`` names the dict field and ``*_dict_key`` the key inside it
    — the dict half is declared data, not introspection-verified.

    ``family_root`` marks an entry that stands for a whole subtree of leaf
    entries: one substrate may flatten the subtree into per-leaf fields, so the
    root itself need not declare a field on both sides. :func:`family_author_keys`
    reads it, and the runtime ledger accounts for a family as a unit.
    """

    author_key: str
    kind: KeyKind
    coverage: SubstrateCoverage
    enforcement: Enforcement
    core_field: str | None
    runner_field: str | None
    core_dict_key: str | None = None
    runner_dict_key: str | None = None
    core_evaluator: str | None = None
    runner_evaluator: str | None = None
    enforcing_test: str | None = None
    reason: str = ""
    tracking_issue: int | None = None
    family_root: bool = False

    def __post_init__(self) -> None:
        if not self.coverage.startswith("BOTH") and not self.reason.strip():
            raise ValueError(
                f"{self.author_key}: coverage {self.coverage.value} needs a non-empty "
                "reason saying why only one substrate evaluates it"
            )
        if self.enforcement is Enforcement.DIFFERENTIAL_INTEGRATION and not self.enforcing_test:
            raise ValueError(
                f"{self.author_key}: enforcement DIFFERENTIAL_INTEGRATION needs an "
                "enforcing_test naming the integration test that proves the differential"
            )
        if (
            self.coverage.startswith("BOTH")
            and not self.family_root
            and (self.core_field is None or self.runner_field is None)
        ):
            raise ValueError(
                f"{self.author_key}: coverage {self.coverage.value} claims both substrates, "
                "so core_field and runner_field must both name a declared field. Only a "
                "family_root entry, whose leaves carry the fields, is exempt"
            )


_CORE_TRANSCRIPT_EVALUATOR = "tolokaforge.core.grading.transcript.TranscriptChecker.grade"
_RUNNER_TRANSCRIPT_EVALUATOR = "tolokaforge.runner.grading.evaluate_transcript_rules"
_CORE_HASH_EVALUATOR = "tolokaforge.core.grading.state_checks.StateChecker.check_hash"

RUNNER_HASH_EVALUATOR = "tolokaforge.runner.service.RunnerServiceImpl._execute_hash_grading"
"""The one runner evaluator that reads the ``state_checks.hash`` family.

The runtime ledger hands a single outcome to the whole family, so a member naming a
different evaluator needs its own recording site.
"""

_ID_FIELDS_LOAD_CHECK = "tolokaforge.runner.id_resolution.check_id_fields_reference_known_tables"

_TRANSCRIPT_AGGREGATION_REASON = (
    "core averages four fixed buckets while the runner scores one sub-check per "
    "declared entry, so the two component scores differ in magnitude"
)

_TRANSCRIPT_PHRASE_REASON = (
    "two independent divergences, and the second one flips the verdict rather than "
    "scaling it. Aggregation: core averages four fixed buckets while the runner scores "
    "one sub-check per declared entry, so the magnitudes differ. Evidence set: core "
    "searches user turns, assistant turns and tool results, while the runner searches "
    "assistant turns alone — so a phrase that appears only in a tool result is FOUND "
    "core-side and MISSING runner-side for the same trial. Measured on a records-present "
    "timeline: must_contain(['refunds allowed']) against a tool result carrying it returns "
    "1.0 on core and 0.0 on the runner. Both predate #676; the runner's narrower set is "
    "pinned by test_must_contain_only_searches_assistant_turns, so it is deliberate rather "
    "than an oversight, and #685 must reconcile the evidence sets and not only the averaging"
)

GRADING_KEYS: tuple[GradingKey, ...] = (
    GradingKey(
        author_key="combine.method",
        kind=KeyKind.AGGREGATION,
        coverage=SubstrateCoverage.RUNNER_ONLY,
        enforcement=Enforcement.FIELD_RESOLUTION_ONLY,
        core_field="GradingCombineConfig.method",
        runner_field="GradingConfig.combine_method",
        runner_evaluator="tolokaforge.runner.grading.combine_grade_components",
        reason=(
            "the core engine always computes a weighted average and never reads "
            "combine.method, so `method: all_pass` scores 0.5 core-side and 0.0 "
            "runner-side for the same components"
        ),
        tracking_issue=692,
    ),
    GradingKey(
        author_key="combine.weights",
        kind=KeyKind.AGGREGATION,
        coverage=SubstrateCoverage.BOTH_SCORE_PARITY,
        enforcement=Enforcement.FIELD_RESOLUTION_ONLY,
        core_field="GradingCombineConfig.weights",
        runner_field="GradingConfig.weights",
        core_evaluator="tolokaforge.core.grading.combine.GradingEngine.grade_trajectory",
        runner_evaluator="tolokaforge.runner.grading.combine_grade_components",
    ),
    GradingKey(
        author_key="combine.pass_threshold",
        kind=KeyKind.AGGREGATION,
        coverage=SubstrateCoverage.BOTH_SCORE_PARITY,
        enforcement=Enforcement.FIELD_RESOLUTION_ONLY,
        core_field="GradingCombineConfig.pass_threshold",
        runner_field="GradingConfig.pass_threshold",
        core_evaluator="tolokaforge.core.grading.combine.GradingEngine.grade_trajectory",
        runner_evaluator="tolokaforge.runner.grading.combine_grade_components",
    ),
    GradingKey(
        author_key="state_checks.hash",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.BOTH_SCORE_PARITY,
        enforcement=Enforcement.FIELD_RESOLUTION_ONLY,
        core_field="StateChecksConfig.hash",
        runner_field=None,
        core_evaluator=_CORE_HASH_EVALUATOR,
        runner_evaluator=RUNNER_HASH_EVALUATOR,
        tracking_issue=687,
        family_root=True,
    ),
    GradingKey(
        author_key="state_checks.hash.enabled",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.BOTH_SCORE_PARITY,
        enforcement=Enforcement.FIELD_RESOLUTION_ONLY,
        core_field="StateChecksConfig.hash",
        runner_field="StateChecksConfig.hash_enabled",
        core_dict_key="enabled",
        core_evaluator=_CORE_HASH_EVALUATOR,
        runner_evaluator=RUNNER_HASH_EVALUATOR,
        tracking_issue=687,
    ),
    GradingKey(
        author_key="state_checks.hash.golden_actions",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.BOTH_SCORE_PARITY,
        enforcement=Enforcement.FIELD_RESOLUTION_ONLY,
        core_field="StateChecksConfig.hash",
        runner_field="StateChecksConfig.golden_actions",
        core_dict_key="golden_actions",
        core_evaluator=(
            "tolokaforge.core.grading.state_checks.StateChecker.check_hash_against_golden_replay"
        ),
        runner_evaluator=RUNNER_HASH_EVALUATOR,
        tracking_issue=687,
    ),
    GradingKey(
        author_key="state_checks.hash.expected_state_hash",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.CORE_ONLY,
        enforcement=Enforcement.FIELD_RESOLUTION_ONLY,
        core_field="StateChecksConfig.hash",
        runner_field="StateChecksConfig.expected_hash",
        core_dict_key="expected_state_hash",
        core_evaluator=_CORE_HASH_EVALUATOR,
        reason=(
            "the adapter translates it onto the runner's expected_hash field and no "
            "runner code path reads it: hash grading always recomputes a golden hash "
            "from golden_actions, and the proto's precomputed_expected_hash is never "
            "populated by the host"
        ),
        tracking_issue=693,
    ),
    GradingKey(
        author_key="state_checks.hash.weight",
        kind=KeyKind.CONFIG_INPUT,
        coverage=SubstrateCoverage.BOTH_SCORE_PARITY,
        enforcement=Enforcement.DIFFERENTIAL_CANONICAL,
        core_field="StateChecksConfig.hash",
        runner_field="StateChecksConfig.hash_weight",
        core_dict_key="weight",
        core_evaluator="tolokaforge.core.grading.state_composition.compose_state_checks_score",
        runner_evaluator="tolokaforge.runner.grading.resolve_state_checks_component",
    ),
    GradingKey(
        author_key="state_checks.jsonpaths",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.BOTH_SCORE_PARITY,
        enforcement=Enforcement.DIFFERENTIAL_CANONICAL,
        core_field="StateChecksConfig.jsonpaths",
        runner_field="StateChecksConfig.jsonpath_checks",
        core_evaluator="tolokaforge.core.grading.state_checks.StateChecker.check_jsonpaths",
        runner_evaluator="tolokaforge.runner.grading.evaluate_jsonpath_checks",
    ),
    GradingKey(
        author_key="state_checks.numeric_string_fields",
        kind=KeyKind.CONFIG_INPUT,
        coverage=SubstrateCoverage.BOTH_SCORE_PARITY,
        enforcement=Enforcement.FIELD_RESOLUTION_ONLY,
        core_field="StateChecksConfig.numeric_string_fields",
        runner_field="StateChecksConfig.numeric_string_fields",
        core_evaluator="tolokaforge.core.hash.compute_stable_hash",
        runner_evaluator=RUNNER_HASH_EVALUATOR,
        tracking_issue=687,
    ),
    GradingKey(
        author_key="state_checks.id_fields",
        kind=KeyKind.CONFIG_INPUT,
        coverage=SubstrateCoverage.BOTH_SCORE_PARITY,
        enforcement=Enforcement.FIELD_RESOLUTION_ONLY,
        core_field="StateChecksConfig.id_fields",
        runner_field="StateChecksConfig.id_fields",
        core_evaluator=_ID_FIELDS_LOAD_CHECK,
        runner_evaluator="tolokaforge.runner.db_proxy.DBServiceProxy._resolve_id_field",
    ),
    GradingKey(
        author_key="state_checks.relaxed_validation",
        kind=KeyKind.CONFIG_INPUT,
        coverage=SubstrateCoverage.BOTH_SCORE_PARITY,
        enforcement=Enforcement.FIELD_RESOLUTION_ONLY,
        core_field="StateChecksConfig.relaxed_validation",
        runner_field="StateChecksConfig.relaxed_validation",
        core_evaluator=_ID_FIELDS_LOAD_CHECK,
        runner_evaluator=_ID_FIELDS_LOAD_CHECK,
    ),
    GradingKey(
        author_key="state_checks.db_probes",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.RUNNER_ONLY,
        enforcement=Enforcement.DIFFERENTIAL_INTEGRATION,
        core_field="StateChecksConfig.db_probes",
        runner_field="StateChecksConfig.db_probes",
        runner_evaluator="tolokaforge.runner.grading.evaluate_db_probes",
        enforcing_test="tests/integration/test_helpdesk_workflow_end_to_end.py",
        reason=(
            "the probe DSN resolves only inside the task's docker network, which the "
            "runner container joins and the host-side core engine does not; the core "
            "config keeps the field for round-trip fidelity and must not evaluate it"
        ),
    ),
    GradingKey(
        author_key="transcript_rules.must_contain",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.BOTH_SIGNAL_PARITY,
        enforcement=Enforcement.DIFFERENTIAL_CANONICAL,
        core_field="TranscriptRulesConfig.must_contain",
        runner_field="TranscriptRulesConfig.must_contain",
        core_evaluator=_CORE_TRANSCRIPT_EVALUATOR,
        runner_evaluator=_RUNNER_TRANSCRIPT_EVALUATOR,
        reason=_TRANSCRIPT_PHRASE_REASON,
        tracking_issue=685,
    ),
    GradingKey(
        author_key="transcript_rules.disallow_regex",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.BOTH_SIGNAL_PARITY,
        enforcement=Enforcement.DIFFERENTIAL_CANONICAL,
        core_field="TranscriptRulesConfig.disallow_regex",
        runner_field="TranscriptRulesConfig.disallow_regex",
        core_evaluator=_CORE_TRANSCRIPT_EVALUATOR,
        runner_evaluator=_RUNNER_TRANSCRIPT_EVALUATOR,
        reason=_TRANSCRIPT_PHRASE_REASON,
        tracking_issue=685,
    ),
    GradingKey(
        author_key="transcript_rules.max_turns",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.BOTH_SIGNAL_PARITY,
        enforcement=Enforcement.DIFFERENTIAL_CANONICAL,
        core_field="TranscriptRulesConfig.max_turns",
        runner_field="TranscriptRulesConfig.max_turns",
        core_evaluator=_CORE_TRANSCRIPT_EVALUATOR,
        runner_evaluator=_RUNNER_TRANSCRIPT_EVALUATOR,
        reason=_TRANSCRIPT_AGGREGATION_REASON,
        tracking_issue=685,
    ),
    GradingKey(
        author_key="transcript_rules.required_actions",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.BOTH_SIGNAL_PARITY,
        enforcement=Enforcement.DIFFERENTIAL_CANONICAL,
        core_field="TranscriptRulesConfig.required_actions",
        runner_field="TranscriptRulesConfig.required_actions",
        core_evaluator="tolokaforge.core.evaluators.action_evaluator.ActionEvaluator.evaluate_actions",
        runner_evaluator=_RUNNER_TRANSCRIPT_EVALUATOR,
        reason=_TRANSCRIPT_AGGREGATION_REASON,
        tracking_issue=685,
    ),
    GradingKey(
        author_key="transcript_rules.communicate_info",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.BOTH_SIGNAL_PARITY,
        enforcement=Enforcement.DIFFERENTIAL_CANONICAL,
        core_field="TranscriptRulesConfig.communicate_info",
        runner_field="TranscriptRulesConfig.communicate_info",
        core_evaluator=(
            "tolokaforge.core.evaluators.communicate_evaluator."
            "CommunicateEvaluator.evaluate_communication"
        ),
        runner_evaluator=_RUNNER_TRANSCRIPT_EVALUATOR,
        reason=_TRANSCRIPT_AGGREGATION_REASON,
        tracking_issue=685,
    ),
    GradingKey(
        author_key="transcript_rules.tool_expectations",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.BOTH_SIGNAL_PARITY,
        enforcement=Enforcement.DIFFERENTIAL_CANONICAL,
        core_field="TranscriptRulesConfig.tool_expectations",
        runner_field="TranscriptRulesConfig.tool_expectations",
        core_evaluator=(
            "tolokaforge.core.grading.transcript.TranscriptChecker.check_tool_expectations"
        ),
        runner_evaluator=_RUNNER_TRANSCRIPT_EVALUATOR,
        reason=(
            "core folds both tool lists into one of four averaged buckets and ignores call "
            "status; the runner scores one sub-check per declared tool and requires a "
            "required tool's call to have succeeded"
        ),
        tracking_issue=685,
    ),
    GradingKey(
        author_key="llm_judge",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.RUNNER_ONLY,
        enforcement=Enforcement.DIFFERENTIAL_INTEGRATION,
        core_field="GradingConfig.llm_judge",
        runner_field="GradingConfig.llm_judge",
        runner_evaluator="tolokaforge.runner.service.RunnerServiceImpl._grade_llm_judge",
        enforcing_test="tests/integration/test_rubric_judge_live.py",
        reason=(
            "the rubric judge runs runner-side on the shared ToolCallingLoop; the core "
            "engine deliberately leaves the llm_judge component unset. The whole "
            "subtree is one entry — its leaves are not separately claimed"
        ),
    ),
    GradingKey(
        author_key="custom_checks",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.BOTH_SCORE_PARITY,
        enforcement=Enforcement.DIFFERENTIAL_CANONICAL,
        core_field="GradingConfig.custom_checks",
        runner_field="GradingConfig.custom_checks",
        core_evaluator="tolokaforge.core.grading.combine.GradingEngine._run_custom_checks",
        runner_evaluator="tolokaforge.runner.service.RunnerServiceImpl._grade_custom_checks",
    ),
    GradingKey(
        author_key="grading_method",
        kind=KeyKind.AGGREGATION,
        coverage=SubstrateCoverage.RUNNER_ONLY,
        enforcement=Enforcement.FIELD_RESOLUTION_ONLY,
        core_field=None,
        runner_field="GradingConfig.grading_method",
        runner_evaluator="tolokaforge.runner.service.RunnerServiceImpl._grade_via_test_execution",
        reason=(
            "a runner-side dispatch selector with no grading.yaml counterpart, set by "
            "adapters (the terminal-bench adapter emits grading_method=test_execution). "
            "The test_execution dispatch returns before the component phase, so it "
            "bypasses key-level evaluation entirely — the declared reason the runtime "
            "accounted-keys ledger does not apply to that dispatch mode"
        ),
    ),
)

_BY_AUTHOR_KEY: dict[str, GradingKey] = {}
for _entry in GRADING_KEYS:
    if _entry.author_key in _BY_AUTHOR_KEY:
        raise ValueError(f"duplicate manifest entry for author key {_entry.author_key!r}")
    _BY_AUTHOR_KEY[_entry.author_key] = _entry


def author_keys() -> frozenset[str]:
    """Every author key the manifest enumerates."""
    return frozenset(_BY_AUTHOR_KEY)


def entry(author_key: str) -> GradingKey:
    """The manifest entry for ``author_key``, raising ``KeyError`` when absent."""
    return _BY_AUTHOR_KEY[author_key]


def family_author_keys(root_author_key: str) -> tuple[str, ...]:
    """A declared family root together with the leaf entries carrying its fields.

    Raises ``ValueError`` when the entry is not declared ``family_root``, so a
    caller cannot invent a family the manifest does not declare.
    """
    root = entry(root_author_key)
    if not root.family_root:
        raise ValueError(
            f"{root_author_key}: not declared family_root, so the manifest gives it no family"
        )
    prefix = f"{root_author_key}."
    return (
        root.author_key,
        *(item.author_key for item in GRADING_KEYS if item.author_key.startswith(prefix)),
    )


def scored_keys_claiming_runner() -> tuple[GradingKey, ...]:
    """Scored-check entries the runner is claimed to evaluate.

    The base of the runtime accounted-keys ledger
    (``tolokaforge/runner/grading_ledger.py``): a populated key here that reaches
    ``GradeTrial`` with neither an evaluator result nor a recorded skip is a silent
    no-op and fails the RPC.
    """
    return tuple(
        item
        for item in GRADING_KEYS
        if item.kind is KeyKind.SCORED_CHECK
        and item.coverage
        in (
            SubstrateCoverage.BOTH_SCORE_PARITY,
            SubstrateCoverage.BOTH_SIGNAL_PARITY,
            SubstrateCoverage.RUNNER_ONLY,
        )
    )
