"""Canonical lane for the :class:`JudgeKind` κ-parity gate.

Every :class:`JudgeKind` that ships or joins the registry proves itself
here:

- **Cross-kind agreement** — the candidate kind's per-criterion verdicts
  and ``single_shot_rubric``'s agree at per-criterion κ ≥ 0.8 across the
  20 committed corpus fixtures.
- **Self-consistency** — five replays of the same kind on the same
  corpus produce per-criterion κ ≥ 0.7. ``_FlakyJudgeKind`` proves the
  self-consistency arm catches non-determinism without a live judge.

**Cassette mode by default.** The judge-loop's ``LLMClient`` is
monkeypatched at :mod:`tolokaforge.core.grading.default_judge_model_provider`
to a :class:`ScriptedLLMClient` seeded from each fixture's
``judge_scripts[<kind_name>]`` cassette. The whole lane runs keyless,
network-free, and under a hard runtime budget (inner-sum < 60 s, full
wall-clock < 90 s).

**Live mode** (``pytest --live-parity``) is a flag-parity contract
only today: passing the flag requires ``OPENAI_API_KEY`` or
``ANTHROPIC_API_KEY``. Cassette-refresh writeback against the real
``litellm`` judge is TODO (#1572); with the flag set the lane skips.

The corpus loader raises loudly (``KeyError``) when a fixture is missing
a cassette for a kind under test — silent skips are the failure mode
this contract refuses.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock

import pytest
import yaml

from tests.utils.scripted_llm_client import ScriptedLLMClient
from tolokaforge.core.grading.judge_kinds import SingleShotRubricJudgeKind
from tolokaforge.core.grading.judge_kinds.parity import (
    ParityCorpusEntry,
    ParityGateThresholds,
    decide_parity_gate,
    measure_cross_kind_agreement,
    measure_self_consistency,
)
from tolokaforge.core.grading.judge_model_provider import JudgeModel, JudgeModelProvider
from tolokaforge.core.grading.judge_result import JudgeResult, JudgeStatus, JudgeUsage
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import ModelConfig
from tolokaforge.runner.models import CriterionResult, Rubric

pytestmark = pytest.mark.canonical


_CORPUS_ROOT = Path(__file__).parent.parent / "data" / "judge_kind_parity_corpus"
_JUDGE_MODEL = ModelConfig(provider="openai", name="gpt-4o-mini", temperature=0.0)
_LIVE_API_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")

# Inner-sum budgets kind work only; outer wall-clock adds ~30 s for
# corpus load and CI jitter.
_INNER_SUM_BUDGET_S = 60.0
_FULL_WALLCLOCK_BUDGET_S = 90.0


# ---------------------------------------------------------------------------
# Corpus loader
# ---------------------------------------------------------------------------


def _normalise_cassette(raw: list[Any]) -> list[Any]:
    """Translate an authored cassette (YAML) into the shape
    :class:`ScriptedLLMClient` consumes: each turn is either a plain
    ``str`` (assistant text) or a list of ``(tool_name, arguments_dict)``
    tuples. YAML naturally produces dict-form tool-call steps
    (``{name: ..., arguments: {...}}``); the client wants tuples, so
    the boundary normalises here.
    """
    normalised: list[Any] = []
    for step in raw:
        if isinstance(step, str):
            normalised.append(step)
            continue
        if isinstance(step, dict) and "text" in step:
            normalised.append(str(step["text"]))
            continue
        if isinstance(step, list):
            calls: list[tuple[str, dict[str, Any]]] = []
            for call in step:
                if not isinstance(call, dict):
                    raise ValueError(f"cassette tool-call step must be a mapping, got {call!r}")
                if "name" not in call or "arguments" not in call:
                    raise ValueError(
                        f"cassette tool-call step must carry name+arguments, got {call!r}"
                    )
                calls.append((str(call["name"]), dict(call["arguments"])))
            normalised.append(calls)
            continue
        raise ValueError(f"unrecognised cassette step shape: {step!r}")
    return normalised


def _load_corpus_entry(path: Path) -> ParityCorpusEntry:
    """Read one ``entry.yaml`` sibling into a :class:`ParityCorpusEntry`.

    Fails loud on a malformed rubric (Pydantic validation) or a missing
    top-level key. Cassette presence for a specific kind is checked at
    dispatch time by :func:`_cassette_for` so a corpus fixture without
    the fixture kind's script still loads (kind-specific scripts, like
    fixture kinds such as ``_FlakyJudgeKind``, are authored inline in
    the test file; corpus YAML ships ``single_shot_rubric`` only).
    """
    data = yaml.safe_load(path.read_text())
    raw_scripts = data.get("judge_scripts", {}) or {}
    normalised_scripts = {
        kind_name: _normalise_cassette(list(script)) for kind_name, script in raw_scripts.items()
    }
    return ParityCorpusEntry(
        entry_id=str(data["entry_id"]),
        rubric=Rubric.model_validate(data["rubric"]),
        agent_system_prompt=str(data["agent_system_prompt"]),
        transcript=list(data["transcript"]),
        state_diff=data.get("state_diff"),
        disable_knowledge_search=bool(data.get("disable_knowledge_search", False)),
        custom_system_prompt=data.get("custom_system_prompt"),
        include_agent_system_prompt=bool(data.get("include_agent_system_prompt", True)),
        judge_scripts=normalised_scripts,
    )


def _load_corpus() -> list[ParityCorpusEntry]:
    """Enumerate every ``*.yaml`` under the corpus root, sorted."""
    yaml_paths = sorted(_CORPUS_ROOT.glob("*/*.yaml"))
    return [_load_corpus_entry(p) for p in yaml_paths]


def _cassette_for(entry: ParityCorpusEntry, kind_name: str) -> list[Any]:
    """Return ``entry.judge_scripts[kind_name]`` or raise a loud
    :class:`KeyError` naming the entry and the missing kind.

    Silent skips (returning ``[]`` on missing scripts) are the failure
    mode the parity contract refuses — a kind under test whose cassette
    is missing must fail lane collection, not silently pair a zero-turn
    judge against a real one."""
    if kind_name not in entry.judge_scripts:
        raise KeyError(
            f"parity corpus entry {entry.entry_id!r} carries no cassette for "
            f"kind {kind_name!r}; add a judge_scripts[{kind_name!r}] block to "
            f"tests/data/judge_kind_parity_corpus/**/{entry.entry_id}.yaml "
            "before naming the kind in the parametrise list."
        )
    return list(entry.judge_scripts[kind_name])


# ---------------------------------------------------------------------------
# Scripted providers and fixture kinds
# ---------------------------------------------------------------------------


class _ScriptedJudgeModelProvider:
    """Test :class:`JudgeModelProvider` — returns a preloaded scripted
    client as the :class:`JudgeModel`. Bypasses the shipped ``litellm``
    transport so the canonical lane drives the judge loop
    deterministically."""

    def __init__(self, client: ScriptedLLMClient) -> None:
        self._client = client

    def build(self, model_config: ModelConfig) -> JudgeModel:  # noqa: ARG002
        return self._client


def _cassette_provider_factory(
    corpus: list[ParityCorpusEntry], kind_name: str
) -> Callable[[int], JudgeModelProvider]:
    """Return a provider_factory that lands a fresh
    :class:`ScriptedLLMClient` for each corpus entry on every replay.

    The harness calls the returned factory ONCE per replay and reuses the
    provider for every entry in that replay; the factory delegates to a
    provider whose :meth:`build` pops the next scripted client off a
    per-replay pool. Each pool is seeded from the fixed
    ``entry.judge_scripts[kind_name]`` cassette, so replay N's client
    stream is a byte-for-byte clone of replay 0's.
    """

    def _factory(_replay_index: int) -> JudgeModelProvider:
        clients = [ScriptedLLMClient(_cassette_for(entry, kind_name)) for entry in corpus]
        remaining = list(clients)

        class _PoolProvider:
            def build(self, model_config: ModelConfig) -> JudgeModel:  # noqa: ARG002
                return remaining.pop(0)

        return _PoolProvider()

    return _factory


def _cassette_provider(corpus: list[ParityCorpusEntry], kind_name: str) -> JudgeModelProvider:
    """Single-replay provider for cross-kind dispatch — pops one scripted
    client per entry in corpus order."""
    return _cassette_provider_factory(corpus, kind_name)(0)


class _FlakyJudgeKind:
    """Fixture kind that deterministically alternates verdicts by replay
    parity. Not registered in the ``tolokaforge.judge_kinds`` entry-point
    group — instantiated directly in-test only.

    Constructor takes ``replay_index``; :meth:`evaluate` returns a
    :class:`JudgeResult` whose per-criterion verdicts are ``met=True`` on
    even replay indices and ``met=False`` on odd ones. Across five
    replays paired against replay 0 (MET), replays 1 and 3 fully
    disagree (κ = -1) and replays 2 and 4 fully agree (κ = 1) — pooled,
    the per-criterion κ collapses to well below 0.7, tripping the
    self-consistency block band.
    """

    NAME: ClassVar[str] = "flaky_fixture"

    def __init__(self, replay_index: int) -> None:
        self._replay_index = replay_index

    def evaluate(self, **kwargs: Any) -> JudgeResult:
        rubric: Rubric = kwargs["rubric"]
        met = self._replay_index % 2 == 0
        return JudgeResult(
            status=JudgeStatus.COMPLETED,
            usage=JudgeUsage(),
            reasons="flaky fixture — verdict depends on replay index",
            score=1.0 if met else 0.0,
            criterion_results=tuple(
                CriterionResult(
                    id=c.id,
                    met=met,
                    score=1.0 if met else 0.0,
                    justification=f"replay={self._replay_index}",
                )
                for c in rubric.criteria
            ),
        )


def _flaky_provider_factory(_replay_index: int) -> JudgeModelProvider:
    """Placeholder provider for :class:`_FlakyJudgeKind` — the fixture
    kind never dispatches through the provider (it hand-builds its
    :class:`JudgeResult`), so a :class:`MagicMock` stands in without
    ever being called."""
    return MagicMock(spec=JudgeModelProvider)


# ---------------------------------------------------------------------------
# Runtime-budget helpers
# ---------------------------------------------------------------------------


class _InnerBudget:
    """Accumulator for the inner-sum wall-clock (kind work only)."""

    def __init__(self) -> None:
        self.total = 0.0

    def measure(self, label: str, fn):  # type: ignore[no-untyped-def]
        del label
        start = time.perf_counter()
        result = fn()
        self.total += time.perf_counter() - start
        return result


# ---------------------------------------------------------------------------
# Corpus-level sanity — the loader is the load-bearing surface for every
# other test in the lane, so its guardrails run first.
# ---------------------------------------------------------------------------


def test_corpus_has_twenty_entries() -> None:
    """Twenty fixtures, every one parses cleanly into
    :class:`ParityCorpusEntry`, every one ships a
    ``judge_scripts.single_shot_rubric`` cassette. A malformed rubric
    fails Pydantic validation right here — never at replay time."""
    corpus = _load_corpus()
    assert len(corpus) == 20, f"corpus size drift: {len(corpus)} entries"
    for entry in corpus:
        assert entry.rubric.criteria, f"{entry.entry_id}: empty rubric"
        missing_cassette_msg = f"{entry.entry_id}: missing single_shot_rubric cassette"
        assert "single_shot_rubric" in entry.judge_scripts, missing_cassette_msg


def test_missing_cassette_raises_with_entry_and_kind_name() -> None:
    """A fixture that never authored a cassette for a kind under test →
    :func:`_cassette_for` raises with both the entry_id and the kind
    name, so lane collection fails loud instead of pairing a zero-turn
    judge against a real one."""
    corpus = _load_corpus()
    with pytest.raises(KeyError) as excinfo:
        _cassette_for(corpus[0], "no_such_kind")
    message = str(excinfo.value)
    assert corpus[0].entry_id in message
    assert "no_such_kind" in message


# ---------------------------------------------------------------------------
# Cassette-mode gate behaviour — four measurements: cross-kind ships,
# cross-kind blocks, self-consistency ships, self-consistency blocks.
# ---------------------------------------------------------------------------


def _thresholds() -> ParityGateThresholds:
    return ParityGateThresholds()


def _single_shot_kind() -> SingleShotRubricJudgeKind:
    return SingleShotRubricJudgeKind()


def test_single_shot_self_parity_ships() -> None:
    """Five deterministic replays of ``single_shot_rubric`` on the corpus
    produce per-criterion κ = 1.0 (identical labels across replays with
    the pooled corpus giving both True and False per criterion), so the
    self-consistency arm ships. Asserts an empty
    ``blocking_criteria`` — a positive lock on the shipped kind."""
    corpus = _load_corpus()
    report = measure_self_consistency(
        kind_factory=lambda _i: _single_shot_kind(),
        corpus=corpus,
        replays=5,
        judge_model_config=_JUDGE_MODEL,
        provider_factory=_cassette_provider_factory(corpus, "single_shot_rubric"),
    )
    decision = decide_parity_gate(report, thresholds=_thresholds(), measurement="self_consistency")
    assert decision.shippable is True, f"self-parity blocked on {decision.blocking_criteria!r}"
    assert decision.blocking_criteria == ()
    assert decision.warning_criteria == ()


def test_flaky_kind_fails_self_consistency() -> None:
    """:class:`_FlakyJudgeKind` returns opposite verdicts on odd vs even
    replay indices; five replays paired against replay 0 pool into
    per-criterion κ well below 0.7, so the self-consistency gate
    blocks. Every per-criterion verdict lands as ``"block"`` —
    proving the gate catches non-determinism.
    """
    corpus = _load_corpus()
    report = measure_self_consistency(
        kind_factory=lambda i: _FlakyJudgeKind(replay_index=i),
        corpus=corpus,
        replays=5,
        judge_model_config=_JUDGE_MODEL,
        provider_factory=_flaky_provider_factory,
    )
    decision = decide_parity_gate(report, thresholds=_thresholds(), measurement="self_consistency")
    assert decision.shippable is False
    assert decision.blocking_criteria, "flaky kind must land at least one block"
    for verdict in decision.per_criterion:
        wrong_status_msg = (
            f"criterion {verdict.criterion_id!r}: expected block, got {verdict.status}"
        )
        assert verdict.status == "block", wrong_status_msg
        assert verdict.kappa is not None
        assert f"{verdict.kappa:.3f}" in verdict.reason


def test_cross_kind_identity_ships() -> None:
    """``single_shot_rubric`` vs ``single_shot_rubric`` on the same
    cassettes produces per-criterion κ = 1.0 everywhere. Byte-parity is
    the κ=1.0 special case."""
    corpus = _load_corpus()
    report = measure_cross_kind_agreement(
        reference_kind=_single_shot_kind(),
        candidate_kind=_single_shot_kind(),
        corpus=corpus,
        judge_model_config=_JUDGE_MODEL,
        reference_provider=_cassette_provider(corpus, "single_shot_rubric"),
        candidate_provider=_cassette_provider(corpus, "single_shot_rubric"),
    )
    decision = decide_parity_gate(report, thresholds=_thresholds(), measurement="cross_kind")
    assert decision.shippable is True
    assert decision.blocking_criteria == ()


def test_cross_kind_flaky_vs_single_shot_blocks() -> None:
    """Reference ``single_shot_rubric`` cassettes carry an alternating
    True/False pattern across fixtures; the flaky candidate returns MET
    everywhere (replay_index=0). Half the observations agree, half
    disagree → κ tracks the flip rate below 0.6 → gate blocks."""
    corpus = _load_corpus()
    report = measure_cross_kind_agreement(
        reference_kind=_single_shot_kind(),
        candidate_kind=_FlakyJudgeKind(replay_index=0),
        corpus=corpus,
        judge_model_config=_JUDGE_MODEL,
        reference_provider=_cassette_provider(corpus, "single_shot_rubric"),
        candidate_provider=MagicMock(spec=JudgeModelProvider),
    )
    decision = decide_parity_gate(report, thresholds=_thresholds(), measurement="cross_kind")
    assert decision.shippable is False
    assert decision.blocking_criteria, "flaky-vs-single_shot must land at least one block"


def test_report_reports_per_criterion_not_aggregate() -> None:
    """Cross-kind identity → :class:`ParityGateDecision` carries a
    per-criterion verdict for every criterion id in the pool, and
    ``shippable`` is the ``all(status in {pass, warn})`` roll-up rather
    than a single-number aggregate κ. Locks per-criterion output (not
    aggregate) against a silent flip to aggregate scoring."""
    corpus = _load_corpus()
    report = measure_cross_kind_agreement(
        reference_kind=_single_shot_kind(),
        candidate_kind=_single_shot_kind(),
        corpus=corpus,
        judge_model_config=_JUDGE_MODEL,
        reference_provider=_cassette_provider(corpus, "single_shot_rubric"),
        candidate_provider=_cassette_provider(corpus, "single_shot_rubric"),
    )
    decision = decide_parity_gate(report, thresholds=_thresholds(), measurement="cross_kind")

    pool_ids = {c.id for entry in corpus for c in entry.rubric.criteria}
    verdict_ids = {v.criterion_id for v in decision.per_criterion}
    assert verdict_ids == pool_ids, (
        f"per-criterion coverage drift: missing {pool_ids - verdict_ids!r}, "
        f"extra {verdict_ids - pool_ids!r}"
    )
    expected = all(v.status in {"pass", "warn"} for v in decision.per_criterion)
    assert decision.shippable is expected


def test_cassette_lane_runtime_budget(request: pytest.FixtureRequest) -> None:
    """Runs the four cassette measurements above end-to-end and asserts
    two thresholds:

    - **Inner sum** < 60 s across the four ``measure_*`` calls (kind
      work only, excludes corpus load).
    - **Full wall-clock** < 90 s including corpus load + YAML parse.

    Both assertions are skipped under ``--live-parity`` because live
    dispatch has no bounded latency."""
    if request.config.getoption("--live-parity"):
        pytest.skip("runtime budgets are cassette-mode only")

    wall_start = time.perf_counter()
    corpus = _load_corpus()
    budget = _InnerBudget()

    budget.measure(
        "single_shot_self",
        lambda: measure_self_consistency(
            kind_factory=lambda _i: _single_shot_kind(),
            corpus=corpus,
            replays=5,
            judge_model_config=_JUDGE_MODEL,
            provider_factory=_cassette_provider_factory(corpus, "single_shot_rubric"),
        ),
    )
    budget.measure(
        "flaky_self",
        lambda: measure_self_consistency(
            kind_factory=lambda i: _FlakyJudgeKind(replay_index=i),
            corpus=corpus,
            replays=5,
            judge_model_config=_JUDGE_MODEL,
            provider_factory=_flaky_provider_factory,
        ),
    )
    budget.measure(
        "cross_kind_identity",
        lambda: measure_cross_kind_agreement(
            reference_kind=_single_shot_kind(),
            candidate_kind=_single_shot_kind(),
            corpus=corpus,
            judge_model_config=_JUDGE_MODEL,
            reference_provider=_cassette_provider(corpus, "single_shot_rubric"),
            candidate_provider=_cassette_provider(corpus, "single_shot_rubric"),
        ),
    )
    budget.measure(
        "cross_kind_flaky",
        lambda: measure_cross_kind_agreement(
            reference_kind=_single_shot_kind(),
            candidate_kind=_FlakyJudgeKind(replay_index=0),
            corpus=corpus,
            judge_model_config=_JUDGE_MODEL,
            reference_provider=_cassette_provider(corpus, "single_shot_rubric"),
            candidate_provider=MagicMock(spec=JudgeModelProvider),
        ),
    )

    wall_elapsed = time.perf_counter() - wall_start
    inner_budget_s = _INNER_SUM_BUDGET_S
    inner_msg = f"cassette-mode inner-sum {budget.total:.2f}s exceeds {inner_budget_s:.0f}s budget"
    assert budget.total < _INNER_SUM_BUDGET_S, inner_msg
    wall_msg = (
        f"cassette-mode wall-clock {wall_elapsed:.2f}s exceeds "
        f"{_FULL_WALLCLOCK_BUDGET_S:.0f}s budget"
    )
    assert wall_elapsed < _FULL_WALLCLOCK_BUDGET_S, wall_msg


# ---------------------------------------------------------------------------
# Live mode — opt-in via --live-parity, gated behind an API key.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_live_mode_flag_skips_when_no_api_key(request: pytest.FixtureRequest) -> None:
    """``--live-parity`` is a flag-parity contract today: passed →
    live-mode opt-in, missing ``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY``
    skips. Cassette-refresh writeback is TODO (#1572). Marked
    ``integration`` so the canonical lane stays keyless."""
    if not request.config.getoption("--live-parity"):
        pytest.skip("live-parity mode disabled — pass --live-parity to opt in")
    if not any(os.environ.get(k) for k in _LIVE_API_KEYS):
        pytest.skip(f"live-parity requires one of {_LIVE_API_KEYS!r} in the process env")
    # Cassette-refresh writeback not wired yet (TODO #1572).
    pytest.skip(
        "live-parity cassette-refresh writeback not wired (TODO #1572); "
        "flag is parsed and honoured."
    )


# ---------------------------------------------------------------------------
# Logger used across tests (keeps the harness's default from firing).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _logger() -> StructuredLogger:
    return StructuredLogger(name="test-judge-kind-parity")
