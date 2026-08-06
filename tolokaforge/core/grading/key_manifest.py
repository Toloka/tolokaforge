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

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from tolokaforge.core.models import (
    KeyAccounting,
    KeyAccountingRecord,
    TraceConstraintKind,
)


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
    """The differential needs real services; ``enforcing_test`` names it as a nodeid."""

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

    When the author key lives *inside the elements* of a ``list[BaseModel]``
    field, ``*_field`` names the list and ``*_element_path`` is a dotted path
    walked from the element model (``"require.before"``). Several entries then
    name one list field, so the field-coverage lock reads the element path rather
    than the field as the claim. The walk needs the substrates' grading-config
    models, which this module does not import, so it lives in
    ``tests/canonical/test_grading_substrate_parity.py``; what is checked here is
    only what needs no config model at all.

    ``family_root`` marks an entry that stands for a whole subtree of leaf
    entries: one substrate may flatten the subtree into per-leaf fields, so the
    root itself need not declare a field on both sides. :func:`family_author_keys`
    reads it, and the runtime ledger accounts for a family as a unit.

    ``enforcing_test`` is a pytest nodeid — ``<module path>::<test function>`` —
    so the claim names the function that runs the differential rather than a file
    that merely contains one.
    """

    author_key: str
    kind: KeyKind
    coverage: SubstrateCoverage
    enforcement: Enforcement
    core_field: str | None
    runner_field: str | None
    core_dict_key: str | None = None
    runner_dict_key: str | None = None
    core_element_path: str | None = None
    runner_element_path: str | None = None
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
        if self.enforcing_test and "::" not in self.enforcing_test:
            raise ValueError(
                f"{self.author_key}: enforcing_test {self.enforcing_test!r} is a file, not a "
                "pytest nodeid. Name the test function that runs the differential: "
                "<module path>::<test function>"
            )
        for substrate, field, dict_key, element_path in (
            ("core", self.core_field, self.core_dict_key, self.core_element_path),
            ("runner", self.runner_field, self.runner_dict_key, self.runner_element_path),
        ):
            if element_path is None:
                continue
            if dict_key is not None:
                raise ValueError(
                    f"{self.author_key}: {substrate}_element_path {element_path!r} and "
                    f"{substrate}_dict_key {dict_key!r} address the same field two ways. A "
                    "dict key is declared data inside an untyped field; an element path is "
                    "walked against the element model. Keep one"
                )
            if field is None:
                raise ValueError(
                    f"{self.author_key}: {substrate}_element_path {element_path!r} is walked "
                    f"from the element model of {substrate}_field, which is None. Name the "
                    "list field the path starts at"
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


_TRANSCRIPT_EVALUATOR = "tolokaforge.core.grading.transcript.evaluate_transcript_rules"
_CORE_HASH_EVALUATOR = "tolokaforge.core.grading.state_checks.StateChecker.check_hash"

RUNNER_HASH_EVALUATOR = "tolokaforge.runner.service.RunnerServiceImpl._execute_hash_grading"
"""The one runner evaluator that reads the ``state_checks.hash`` family.

The runtime ledger hands a single outcome to the whole family, so a member naming a
different evaluator needs its own recording site.
"""

_ID_FIELDS_LOAD_CHECK = "tolokaforge.runner.id_resolution.check_id_fields_reference_known_tables"

_HASH_COMPOSITION_WIRE_TEST = (
    "tests/integration/test_docker_grading_hash_composition.py"
    "::test_the_runner_blends_its_golden_replay_verdict_by_the_authors_weight"
)
"""Drives the runner's own golden-replay hash verdict into the shared composer.

A matching and a diverging final state, at two weights strictly inside ``(0, 1)``,
over real gRPC and a real db-service. The hash family's differential cannot run
in-process: the evaluator replays golden actions against db-service over HTTP.
"""

_HASH_SOURCE_SHAPE_REASON = (
    "both substrates fold the hash verdict by one shared rule, and of the two authorable "
    "hash-source shapes only one is proven to hand them the same verdict. Proven: "
    "golden_actions, by the enforcing_test — both replay the actions and compare against the "
    "resulting state. Not proven: expected_state_hash alone, because no runner path reads the "
    "translated expected_hash (#693), so the runner compares the trial against the *initial* "
    "state where core compares it against the author's literal. An enabled hash with no "
    "declared source is refused at the authoring gate, so the third shape is unauthorable — it "
    "survives only in a bundle recorded before that rule, where core produces no verdict at "
    "all while the runner's refusal semantics produce a binary one, and retrace replays it "
    "unchanged. Golden actions with no world to replay them in is a fourth shape, unauthorable "
    "the same way: core raises and leaves the trial unscored where the runner replays against "
    "the live trial — the tools RegisterTrial registered, over db-service's state — and grades "
    "it, so that divergence too survives only in a config no gate saw. Moving #693's shape "
    "moves refusal-task verdicts, which needs its own corpus measurement"
)

_COMBINE_METHOD_PARITY_REASON = (
    "both substrates read combine.method and both discriminate on it, but their final "
    "scores agree only when the two build the same component set. What prevents that is "
    "architectural and permanent: core produces no llm_judge component "
    "(GradingEngine.grade_trajectory leaves it unset) and cannot produce a db_probes one, "
    "both RUNNER_ONLY by design, so on a judge- or probe-graded pack core's component map is "
    "empty where the runner's is scored. Since all and any aggregate that map alone, the "
    "disagreement is a verdict flip, not a magnitude. The canonical differential proves the "
    "dispatch over deterministic components, which is the whole of what is provable here"
)

_COMBINE_WEIGHTS_MEMBERSHIP_REASON = (
    "both substrates scale the components they fold by the author's weights and both refuse "
    "the same way when a scored component carries none, so the membership question is "
    "differentially provable. What remains is architectural and permanent: core produces no "
    "llm_judge component (GradingEngine.grade_trajectory leaves it unset) and cannot produce "
    "a db_probes one, both RUNNER_ONLY by design, so on a judge- or probe-graded pack the two "
    "component sets differ whatever the map says — and all/any aggregate that set alone, so "
    "the disagreement is a verdict flip rather than a magnitude"
)

_TRACE_CHECKS_EVALUATOR = "tolokaforge.core.grading.trace_checks.evaluate_trace_checks"
"""The one evaluator both substrates score ``trace_checks`` with.

Named on both sides of every entry in the family, which is the manifest stating
that one function serves both substrates. Two names would claim two
implementations kept in step, and the block crosses the adapter as the same class
— there is no per-key translation for either to drift through.
"""

_TRACE_CHECKS_EVIDENCE_LIMITS = (
    "three declared limits on the evidence a matcher may read, so none of them "
    "surfaces as undeclared drift. #717: a failed call's recorded result text is not "
    "identical on the two substrates, so a result predicate is admitted only beside a "
    "status predicate reading exactly {equals: success}, and rejected at load "
    "otherwise. #688: no timeline event carries executor: user, so an executor "
    "predicate selecting it matches nothing on either substrate. #727: a "
    "TRIAL_NOT_FOUND harness fault is recorded as a tool error, so a status predicate "
    "reading error can select a call whose failure was not the agent's"
)

_TRACE_CHECKS_ALTERNATIVES_NARROWING = (
    "the alternative routes a pack declares, each scored as a whole against the "
    "shared constraints plus its own. An entry carries one runner_field, and the "
    "per-constraint entries address TraceChecksConfig.constraints alone, so a "
    "constraint kind or per-constraint field written only inside a path is covered "
    "by this key rather than by its own "
    "— #772 narrows the ledger's populated-implies-accounted guarantee there. The "
    "parity claim is untouched: one function reads a TraceConstraint identically "
    "whichever list it came from. "
)

_TRACE_CHECKS_FAMILY_ROOT_REASON = (
    "a family root declaring no field on either substrate: it names the block its "
    "leaves live under, and every score under it is a leaf's. CONFIG_INPUT is what "
    "that is — the root shapes nothing and scores nothing of its own — and the "
    "differential its enforcement claims is the family's, run by the leaves' own "
    "fixture packs. " + _TRACE_CHECKS_EVIDENCE_LIMITS
)


_TRACE_CONSTRAINT_MANIFEST_KINDS: tuple[str, ...] = (
    "present",
    "absent",
    "count",
    "before",
    "immediately_before",
    "absent_before",
    "absent_between",
    "all_of",
    "any_of",
    "negate",
)
"""The kinds the manifest carries an entry for, written out here.

A second source for the vocabulary-totality lock, which compares it against
``TRACE_CONSTRAINT_KINDS`` and against the fields ``TraceConstraintExpr``
declares. Comprehending it from either would leave that lock asserting one source
against itself.
"""


def _trace_constraint_kind_key(kind: str) -> GradingKey:
    """One scored entry per constraint kind, addressed inside the constraint list.

    Every kind is independently implementable, so partial per-constraint coverage —
    a kind evaluated on one substrate and skipped on the other — is the realistic
    drift shape. Leaf granularity is what makes that shape fail the parity suite:
    each kind owns a fixture pack that must discriminate on both substrates.
    """
    return GradingKey(
        author_key=f"trace_checks.constraints.{kind}",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.BOTH_SCORE_PARITY,
        enforcement=Enforcement.DIFFERENTIAL_CANONICAL,
        core_field="TraceChecksConfig.constraints",
        runner_field="TraceChecksConfig.constraints",
        core_element_path=f"require.{kind}",
        runner_element_path=f"require.{kind}",
        core_evaluator=_TRACE_CHECKS_EVALUATOR,
        runner_evaluator=_TRACE_CHECKS_EVALUATOR,
        reason=_TRACE_CHECKS_EVIDENCE_LIMITS,
    )


def _trace_constraint_field_key(field: str, reason: str) -> GradingKey:
    """One entry per per-constraint field that shapes how the kinds are scored."""
    return GradingKey(
        author_key=f"trace_checks.constraints.{field}",
        kind=KeyKind.CONFIG_INPUT,
        coverage=SubstrateCoverage.BOTH_SCORE_PARITY,
        enforcement=Enforcement.DIFFERENTIAL_CANONICAL,
        core_field="TraceChecksConfig.constraints",
        runner_field="TraceChecksConfig.constraints",
        core_element_path=field,
        runner_element_path=field,
        core_evaluator=_TRACE_CHECKS_EVALUATOR,
        runner_evaluator=_TRACE_CHECKS_EVALUATOR,
        reason=reason,
    )


GRADING_KEYS: tuple[GradingKey, ...] = (
    GradingKey(
        author_key="combine.method",
        kind=KeyKind.AGGREGATION,
        coverage=SubstrateCoverage.BOTH_SIGNAL_PARITY,
        enforcement=Enforcement.DIFFERENTIAL_CANONICAL,
        core_field="GradingCombineConfig.method",
        runner_field="RunnerGradingConfig.combine_method",
        core_evaluator="tolokaforge.core.grading.combine.GradingEngine.grade_trajectory",
        runner_evaluator="tolokaforge.runner.grading.combine_grade_components",
        reason=_COMBINE_METHOD_PARITY_REASON,
    ),
    GradingKey(
        author_key="combine.weights",
        kind=KeyKind.AGGREGATION,
        coverage=SubstrateCoverage.BOTH_SIGNAL_PARITY,
        enforcement=Enforcement.DIFFERENTIAL_CANONICAL,
        core_field="GradingCombineConfig.weights",
        runner_field="RunnerGradingConfig.weights",
        core_evaluator="tolokaforge.core.grading.combine.GradingEngine.grade_trajectory",
        runner_evaluator="tolokaforge.runner.grading.combine_grade_components",
        reason=_COMBINE_WEIGHTS_MEMBERSHIP_REASON,
    ),
    GradingKey(
        author_key="combine.pass_threshold",
        kind=KeyKind.AGGREGATION,
        coverage=SubstrateCoverage.BOTH_SCORE_PARITY,
        enforcement=Enforcement.FIELD_RESOLUTION_ONLY,
        core_field="GradingCombineConfig.pass_threshold",
        runner_field="RunnerGradingConfig.pass_threshold",
        core_evaluator="tolokaforge.core.grading.combine.GradingEngine.grade_trajectory",
        runner_evaluator="tolokaforge.runner.grading.combine_grade_components",
    ),
    GradingKey(
        author_key="state_checks.hash",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.BOTH_SIGNAL_PARITY,
        enforcement=Enforcement.DIFFERENTIAL_INTEGRATION,
        core_field="StateChecksConfig.hash",
        runner_field=None,
        core_evaluator=_CORE_HASH_EVALUATOR,
        runner_evaluator=RUNNER_HASH_EVALUATOR,
        enforcing_test=_HASH_COMPOSITION_WIRE_TEST,
        reason=_HASH_SOURCE_SHAPE_REASON,
        family_root=True,
    ),
    GradingKey(
        author_key="state_checks.hash.enabled",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.BOTH_SIGNAL_PARITY,
        enforcement=Enforcement.DIFFERENTIAL_INTEGRATION,
        core_field="StateChecksConfig.hash",
        runner_field="RunnerStateChecksConfig.hash_enabled",
        core_dict_key="enabled",
        core_evaluator=_CORE_HASH_EVALUATOR,
        runner_evaluator=RUNNER_HASH_EVALUATOR,
        enforcing_test=_HASH_COMPOSITION_WIRE_TEST,
        reason=_HASH_SOURCE_SHAPE_REASON,
    ),
    GradingKey(
        author_key="state_checks.hash.golden_actions",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.BOTH_SCORE_PARITY,
        enforcement=Enforcement.DIFFERENTIAL_INTEGRATION,
        core_field="StateChecksConfig.hash",
        runner_field="RunnerStateChecksConfig.golden_actions",
        core_dict_key="golden_actions",
        core_evaluator=(
            "tolokaforge.core.grading.state_checks.StateChecker.check_hash_against_golden_replay"
        ),
        runner_evaluator=RUNNER_HASH_EVALUATOR,
        enforcing_test=_HASH_COMPOSITION_WIRE_TEST,
    ),
    GradingKey(
        author_key="state_checks.hash.expected_state_hash",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.CORE_ONLY,
        enforcement=Enforcement.FIELD_RESOLUTION_ONLY,
        core_field="StateChecksConfig.hash",
        runner_field="RunnerStateChecksConfig.expected_hash",
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
        runner_field="RunnerStateChecksConfig.hash_weight",
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
        runner_field="RunnerStateChecksConfig.jsonpath_checks",
        core_evaluator="tolokaforge.core.grading.state_checks.StateChecker.check_jsonpaths",
        runner_evaluator="tolokaforge.runner.grading.evaluate_jsonpath_checks",
    ),
    GradingKey(
        author_key="state_checks.numeric_string_fields",
        kind=KeyKind.CONFIG_INPUT,
        coverage=SubstrateCoverage.BOTH_SCORE_PARITY,
        enforcement=Enforcement.FIELD_RESOLUTION_ONLY,
        core_field="StateChecksConfig.numeric_string_fields",
        runner_field="RunnerStateChecksConfig.numeric_string_fields",
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
        runner_field="RunnerStateChecksConfig.id_fields",
        core_evaluator=_ID_FIELDS_LOAD_CHECK,
        runner_evaluator="tolokaforge.runner.db_proxy.DBServiceProxy._resolve_id_field",
    ),
    GradingKey(
        author_key="state_checks.relaxed_validation",
        kind=KeyKind.CONFIG_INPUT,
        coverage=SubstrateCoverage.BOTH_SCORE_PARITY,
        enforcement=Enforcement.FIELD_RESOLUTION_ONLY,
        core_field="StateChecksConfig.relaxed_validation",
        runner_field="RunnerStateChecksConfig.relaxed_validation",
        core_evaluator=_ID_FIELDS_LOAD_CHECK,
        runner_evaluator=_ID_FIELDS_LOAD_CHECK,
    ),
    GradingKey(
        author_key="state_checks.db_probes",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.RUNNER_ONLY,
        enforcement=Enforcement.DIFFERENTIAL_INTEGRATION,
        core_field="StateChecksConfig.db_probes",
        runner_field="RunnerStateChecksConfig.db_probes",
        runner_evaluator="tolokaforge.runner.grading.evaluate_db_probes",
        enforcing_test=(
            "tests/integration/test_helpdesk_workflow_end_to_end.py"
            "::test_helpdesk_workflow_infrastructure_end_to_end"
        ),
        reason=(
            "the probe DSN resolves only inside the task's docker network, which the "
            "runner container joins and the host-side core engine does not; the core "
            "config keeps the field for round-trip fidelity and must not evaluate it"
        ),
    ),
    GradingKey(
        author_key="transcript_rules.must_contain",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.BOTH_SCORE_PARITY,
        enforcement=Enforcement.DIFFERENTIAL_CANONICAL,
        core_field="TranscriptRulesConfig.must_contain",
        runner_field="TranscriptRulesConfig.must_contain",
        core_evaluator=_TRANSCRIPT_EVALUATOR,
        runner_evaluator=_TRANSCRIPT_EVALUATOR,
    ),
    GradingKey(
        author_key="transcript_rules.disallow_regex",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.BOTH_SCORE_PARITY,
        enforcement=Enforcement.DIFFERENTIAL_CANONICAL,
        core_field="TranscriptRulesConfig.disallow_regex",
        runner_field="TranscriptRulesConfig.disallow_regex",
        core_evaluator=_TRANSCRIPT_EVALUATOR,
        runner_evaluator=_TRANSCRIPT_EVALUATOR,
    ),
    GradingKey(
        author_key="transcript_rules.max_turns",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.BOTH_SCORE_PARITY,
        enforcement=Enforcement.DIFFERENTIAL_CANONICAL,
        core_field="TranscriptRulesConfig.max_turns",
        runner_field="TranscriptRulesConfig.max_turns",
        core_evaluator=_TRANSCRIPT_EVALUATOR,
        runner_evaluator=_TRANSCRIPT_EVALUATOR,
    ),
    GradingKey(
        author_key="transcript_rules.min_assistant_turns",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.BOTH_SCORE_PARITY,
        enforcement=Enforcement.DIFFERENTIAL_CANONICAL,
        core_field="TranscriptRulesConfig.min_assistant_turns",
        runner_field="TranscriptRulesConfig.min_assistant_turns",
        core_evaluator=_TRANSCRIPT_EVALUATOR,
        runner_evaluator=_TRANSCRIPT_EVALUATOR,
    ),
    GradingKey(
        author_key="transcript_rules.required_actions",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.BOTH_SCORE_PARITY,
        enforcement=Enforcement.DIFFERENTIAL_CANONICAL,
        core_field="TranscriptRulesConfig.required_actions",
        runner_field="TranscriptRulesConfig.required_actions",
        core_evaluator=_TRANSCRIPT_EVALUATOR,
        runner_evaluator=_TRANSCRIPT_EVALUATOR,
    ),
    GradingKey(
        author_key="transcript_rules.communicate_info",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.BOTH_SCORE_PARITY,
        enforcement=Enforcement.DIFFERENTIAL_CANONICAL,
        core_field="TranscriptRulesConfig.communicate_info",
        runner_field="TranscriptRulesConfig.communicate_info",
        core_evaluator=_TRANSCRIPT_EVALUATOR,
        runner_evaluator=_TRANSCRIPT_EVALUATOR,
    ),
    GradingKey(
        author_key="transcript_rules.tool_expectations",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.BOTH_SCORE_PARITY,
        enforcement=Enforcement.DIFFERENTIAL_CANONICAL,
        core_field="TranscriptRulesConfig.tool_expectations",
        runner_field="TranscriptRulesConfig.tool_expectations",
        core_evaluator=_TRANSCRIPT_EVALUATOR,
        runner_evaluator=_TRANSCRIPT_EVALUATOR,
    ),
    GradingKey(
        author_key="trace_checks",
        kind=KeyKind.CONFIG_INPUT,
        coverage=SubstrateCoverage.BOTH_SCORE_PARITY,
        enforcement=Enforcement.DIFFERENTIAL_CANONICAL,
        core_field=None,
        runner_field=None,
        core_evaluator=_TRACE_CHECKS_EVALUATOR,
        runner_evaluator=_TRACE_CHECKS_EVALUATOR,
        reason=_TRACE_CHECKS_FAMILY_ROOT_REASON,
        family_root=True,
    ),
    GradingKey(
        author_key="trace_checks.constraints",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.BOTH_SCORE_PARITY,
        enforcement=Enforcement.DIFFERENTIAL_CANONICAL,
        core_field="TraceChecksConfig.constraints",
        runner_field="TraceChecksConfig.constraints",
        core_evaluator=_TRACE_CHECKS_EVALUATOR,
        runner_evaluator=_TRACE_CHECKS_EVALUATOR,
        reason=_TRACE_CHECKS_EVIDENCE_LIMITS,
    ),
    GradingKey(
        author_key="trace_checks.alternatives",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.BOTH_SCORE_PARITY,
        enforcement=Enforcement.DIFFERENTIAL_CANONICAL,
        core_field="TraceChecksConfig.alternatives",
        runner_field="TraceChecksConfig.alternatives",
        core_evaluator=_TRACE_CHECKS_EVALUATOR,
        runner_evaluator=_TRACE_CHECKS_EVALUATOR,
        reason=_TRACE_CHECKS_ALTERNATIVES_NARROWING + _TRACE_CHECKS_EVIDENCE_LIMITS,
    ),
    *(_trace_constraint_kind_key(kind) for kind in _TRACE_CONSTRAINT_MANIFEST_KINDS),
    _trace_constraint_field_key(
        "weight",
        "the share of the component score a constraint carries, which both substrates "
        "fold by the same weighted fraction. " + _TRACE_CHECKS_EVIDENCE_LIMITS,
    ),
    _trace_constraint_field_key(
        "on_missing",
        "what an anchor that matched nothing decides, which is a policy over the "
        "constraint's verdict rather than a check of its own. " + _TRACE_CHECKS_EVIDENCE_LIMITS,
    ),
    _trace_constraint_field_key(
        "severity",
        "whether a constraint carries a share of the component or is a gate that must "
        "hold, which decides whether it enters the weighted average at all and whether "
        "its violation takes the component to 0.0. " + _TRACE_CHECKS_EVIDENCE_LIMITS,
    ),
    _trace_constraint_field_key(
        "within",
        "the inclusive turn window every matcher in a constraint is restricted to, "
        "which narrows what the kinds select. " + _TRACE_CHECKS_EVIDENCE_LIMITS,
    ),
    _trace_constraint_field_key(
        "bind",
        "the values a constraint draws out of one event, which every matcher in its "
        "require tree then resolves a binding reference against — so the kinds are "
        "read once per candidate assignment rather than once, and the policy for a "
        "binder that yielded none decides the constraint on its own. "
        + _TRACE_CHECKS_EVIDENCE_LIMITS,
    ),
    GradingKey(
        author_key="llm_judge",
        kind=KeyKind.SCORED_CHECK,
        coverage=SubstrateCoverage.RUNNER_ONLY,
        enforcement=Enforcement.DIFFERENTIAL_INTEGRATION,
        core_field="GradingConfig.llm_judge",
        runner_field="RunnerGradingConfig.llm_judge",
        runner_evaluator="tolokaforge.runner.service.RunnerServiceImpl._grade_llm_judge",
        enforcing_test=(
            "tests/integration/test_rubric_judge_live.py"
            "::test_rubric_judge_live_gate_fails_when_refund_missing"
        ),
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
        runner_field="RunnerGradingConfig.custom_checks",
        core_evaluator="tolokaforge.core.grading.combine.GradingEngine._run_custom_checks",
        runner_evaluator="tolokaforge.runner.service.RunnerServiceImpl._grade_custom_checks",
    ),
    GradingKey(
        author_key="grading_method",
        kind=KeyKind.AGGREGATION,
        coverage=SubstrateCoverage.RUNNER_ONLY,
        enforcement=Enforcement.FIELD_RESOLUTION_ONLY,
        core_field=None,
        runner_field="RunnerGradingConfig.grading_method",
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


def checked_author_key(author_key: str) -> str:
    """``author_key`` itself, raising at import when the manifest no longer has it."""
    return entry(author_key).author_key


EVALUATED = KeyAccountingRecord(outcome=KeyAccounting.EVALUATED)

NO_TIMELINE_EVENTS_SKIP = KeyAccountingRecord(
    outcome=KeyAccounting.SKIPPED, detail="the trial's timeline carries no events"
)

# Every way a binder leaves its constraint's ``require`` tree unentered: no event
# selected, events carrying no value to bind, and an event whose value the trial does
# not record. One detail rather than one per reason, because the record answers "was
# this kind evaluated" and the constraint's own message answers why it was not.
UNBOUND_BINDING_SKIP = KeyAccountingRecord(
    outcome=KeyAccounting.SKIPPED, detail="the binding yielded no assignment"
)

MUST_CONTAIN_KEY = checked_author_key("transcript_rules.must_contain")
DISALLOW_REGEX_KEY = checked_author_key("transcript_rules.disallow_regex")
MAX_TURNS_KEY = checked_author_key("transcript_rules.max_turns")
MIN_ASSISTANT_TURNS_KEY = checked_author_key("transcript_rules.min_assistant_turns")
TOOL_EXPECTATIONS_KEY = checked_author_key("transcript_rules.tool_expectations")
REQUIRED_ACTIONS_KEY = checked_author_key("transcript_rules.required_actions")
COMMUNICATE_INFO_KEY = checked_author_key("transcript_rules.communicate_info")

TRACE_CONSTRAINTS_KEY = checked_author_key("trace_checks.constraints")

TRACE_ALTERNATIVES_KEY = checked_author_key("trace_checks.alternatives")

TRACE_CONSTRAINT_KEY_BY_KIND: Mapping[TraceConstraintKind, str] = {
    TraceConstraintKind.PRESENT: checked_author_key("trace_checks.constraints.present"),
    TraceConstraintKind.ABSENT: checked_author_key("trace_checks.constraints.absent"),
    TraceConstraintKind.COUNT: checked_author_key("trace_checks.constraints.count"),
    TraceConstraintKind.BEFORE: checked_author_key("trace_checks.constraints.before"),
    TraceConstraintKind.IMMEDIATELY_BEFORE: checked_author_key(
        "trace_checks.constraints.immediately_before"
    ),
    TraceConstraintKind.ABSENT_BEFORE: checked_author_key("trace_checks.constraints.absent_before"),
    TraceConstraintKind.ABSENT_BETWEEN: checked_author_key(
        "trace_checks.constraints.absent_between"
    ),
    TraceConstraintKind.ALL_OF: checked_author_key("trace_checks.constraints.all_of"),
    TraceConstraintKind.ANY_OF: checked_author_key("trace_checks.constraints.any_of"),
    TraceConstraintKind.NEGATE: checked_author_key("trace_checks.constraints.negate"),
}
"""The author key each constraint kind is accounted under, one line per kind.

Written out rather than comprehended over
:class:`~tolokaforge.runner.models.TraceConstraintKind`, so the lock comparing
the two compares two sources: a comprehension would assert the vocabulary against
itself and pass with a kind nothing accounts for. Each value is resolved through
:func:`checked_author_key`, so a kind the manifest stops carrying an entry for
raises at import instead of accounting against a key nothing enumerates.
"""


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
