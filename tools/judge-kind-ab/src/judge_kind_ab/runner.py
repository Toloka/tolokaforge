"""Live A/B runner — drives kappa-parity + cost measurement across JudgeKinds.

For every unordered pair of the given kinds, measures cross-kind agreement via
:func:`~tolokaforge.core.grading.judge_kinds.parity.measure_cross_kind_agreement`;
for every kind, measures self-consistency via
:func:`~tolokaforge.core.grading.judge_kinds.parity.measure_self_consistency`. Both
return a :class:`CalibrationReport`, which carries no usage — so usage (calls,
tokens, cost) is captured separately by wrapping each real
:class:`~tolokaforge.core.grading.judge_kinds._protocol.JudgeKind` in
:class:`_UsageTrackingKind`, which records the :class:`JudgeUsage` off every
``evaluate()`` return against the corpus entry it was called for.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

from tolokaforge.core.grading.judge_kinds._protocol import JudgeKind
from tolokaforge.core.grading.judge_kinds.parity import (
    ParityCorpusEntry,
    ParityGateDecision,
    ParityGateThresholds,
    decide_parity_gate,
    measure_cross_kind_agreement,
    measure_self_consistency,
)
from tolokaforge.core.grading.judge_model_provider import JudgeModelProvider
from tolokaforge.core.grading.judge_result import JudgeResult, JudgeUsage
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.plugin_registry import load_judge_kind

#: ``provider_factory_for(kind_name)`` returns a per-replay provider factory, the
#: same ``Callable[[int], JudgeModelProvider]`` shape ``measure_self_consistency``
#: takes; cross-kind agreement calls it once (replay index 0, single provider).
ProviderFactoryFor = Callable[[str], Callable[[int], JudgeModelProvider]]


@dataclass(frozen=True)
class UsageRecord:
    """One ``evaluate()`` call's usage, attributed to the corpus entry it scored."""

    entry_id: str
    family: str
    usage: JudgeUsage


@dataclass
class _UsageTrackingKind:
    """Wraps a real :class:`JudgeKind`, recording usage per ``evaluate()`` call.

    ``measure_cross_kind_agreement``/``measure_self_consistency`` call
    ``evaluate()`` exactly once per corpus entry, in corpus order, per kind
    instance — this walks ``entry_tags`` in lockstep to attribute each call.
    """

    inner: JudgeKind
    entry_tags: Sequence[tuple[str, str]]
    records: list[UsageRecord] = field(default_factory=list)
    _next: int = field(default=0, init=False, repr=False)

    def evaluate(self, **kwargs: Any) -> JudgeResult:
        result = self.inner.evaluate(**kwargs)
        entry_id, family = self.entry_tags[self._next]
        self._next += 1
        self.records.append(UsageRecord(entry_id=entry_id, family=family, usage=result.usage))
        return result


@dataclass(frozen=True)
class CrossKindResult:
    """One unordered pair's cross-kind agreement measurement."""

    reference: str
    candidate: str
    decision: ParityGateDecision


@dataclass(frozen=True)
class SelfConsistencyResult:
    """One kind's self-consistency measurement across replays."""

    kind: str
    decision: ParityGateDecision


@dataclass(frozen=True)
class LiveABResult:
    """The full live A/B sweep: every cross-kind pair, every kind's self-consistency,
    and usage attributed per kind (across every measurement that kind took part in)."""

    cross_kind: tuple[CrossKindResult, ...]
    self_consistency: tuple[SelfConsistencyResult, ...]
    usage_by_kind: dict[str, tuple[UsageRecord, ...]]


def run_live_ab(
    corpus: Sequence[tuple[str, ParityCorpusEntry]],
    *,
    kind_names: Sequence[str],
    replays: int,
    judge_model_config: ModelConfig,
    provider_factory_for: ProviderFactoryFor,
    thresholds: ParityGateThresholds | None = None,
) -> LiveABResult:
    """Measure cross-kind kappa agreement, self-consistency, and cost across ``kind_names``.

    ``corpus`` is ``load_corpus_from_run``'s ``(family, ParityCorpusEntry)`` shape.
    ``provider_factory_for`` is the seam live mode (``LiteLLMJudgeModelProvider``)
    and a dry-run test (scripted cassette providers) implement differently.
    """
    thresholds = thresholds or ParityGateThresholds()
    names = sorted(set(kind_names))
    entries = [entry for _family, entry in corpus]
    entry_tags = [(entry.entry_id, family) for family, entry in corpus]

    trackers: dict[str, list[_UsageTrackingKind]] = {name: [] for name in names}

    def _tracked(name: str) -> _UsageTrackingKind:
        tracker = _UsageTrackingKind(inner=load_judge_kind(name)(), entry_tags=entry_tags)
        trackers[name].append(tracker)
        return tracker

    cross_kind_results: list[CrossKindResult] = []
    for reference_name, candidate_name in combinations(names, 2):
        report = measure_cross_kind_agreement(
            reference_kind=_tracked(reference_name),
            candidate_kind=_tracked(candidate_name),
            corpus=entries,
            judge_model_config=judge_model_config,
            reference_provider=provider_factory_for(reference_name)(0),
            candidate_provider=provider_factory_for(candidate_name)(0),
        )
        decision = decide_parity_gate(report, thresholds=thresholds, measurement="cross_kind")
        cross_kind_results.append(CrossKindResult(reference_name, candidate_name, decision))

    self_consistency_results: list[SelfConsistencyResult] = []
    for name in names:
        report = measure_self_consistency(
            kind_factory=lambda _replay_index, name=name: _tracked(name),
            corpus=entries,
            replays=replays,
            judge_model_config=judge_model_config,
            provider_factory=provider_factory_for(name),
        )
        decision = decide_parity_gate(report, thresholds=thresholds, measurement="self_consistency")
        self_consistency_results.append(SelfConsistencyResult(name, decision))

    usage_by_kind = {
        name: tuple(record for tracker in trackers[name] for record in tracker.records)
        for name in names
    }

    return LiveABResult(
        cross_kind=tuple(cross_kind_results),
        self_consistency=tuple(self_consistency_results),
        usage_by_kind=usage_by_kind,
    )
