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
network-free, and under a hard runtime budget.

**Live mode** (``pytest --live-parity``) drives every corpus entry and
every kind under test against a real judge model, records each turn
via :class:`~tests.utils.recording_llm_client.RecordingLLMClient`, and
rewrites the fixture's ``judge_scripts`` cassette in place, preserving
every other key. Requires ``OPENAI_API_KEY`` or ``ANTHROPIC_API_KEY``;
see ``test_live_mode_writeback`` (``@pytest.mark.integration``, never
runs in CI — see that test's docstring). The writeback mechanics
themselves are locked keyless by two ``unit``-tier tests:
:mod:`tests.utils.test_recording_llm_client` (script-capture fidelity)
and ``test_writeback_rewrites_cassette_preserving_other_keys`` below
(YAML rewrite mechanics against a scripted, not live, recorder).

The corpus loader raises loudly (``KeyError``) when a fixture is missing
a cassette for a kind under test — silent skips are the failure mode
this contract refuses.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock

import pytest
import yaml

from tests.utils.recording_llm_client import RecordingLLMClient
from tests.utils.scripted_llm_client import ScriptedLLMClient
from tolokaforge.core.grading.judge_kinds import (
    AutoAnchoredRubricJudgeKind,
    AutoRubricJudgeKind,
    JudgeKind,
    JudgeTrialOptions,
    MultiTurnRubricJudgeKind,
    SingleShotRubricJudgeKind,
    VotedRubricJudgeKind,
)
from tolokaforge.core.grading.judge_kinds.auto_anchored import clear_anchor_cache
from tolokaforge.core.grading.judge_kinds.parity import (
    ParityCorpusEntry,
    ParityGateThresholds,
    _evaluate_kwargs,
    decide_parity_gate,
    measure_cross_kind_agreement,
    measure_self_consistency,
)
from tolokaforge.core.grading.judge_kinds.voted import DEFAULT_N_SAMPLES
from tolokaforge.core.grading.judge_model_provider import JudgeModel, JudgeModelProvider
from tolokaforge.core.grading.judge_result import JudgeResult, JudgeStatus, JudgeUsage
from tolokaforge.core.grading.kb_search import DEFAULT_JUDGE_SNIPPET_CHARS
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
        judge_scripts=normalised_scripts,
        options=JudgeTrialOptions(
            disable_knowledge_search=bool(data.get("disable_knowledge_search", False)),
            custom_system_prompt=data.get("custom_system_prompt"),
            include_agent_system_prompt=bool(data.get("include_agent_system_prompt", True)),
            judge_snippet_chars=data.get("judge_snippet_chars", DEFAULT_JUDGE_SNIPPET_CHARS),
        ),
    )


def _load_corpus_paths() -> list[Path]:
    """Enumerate every corpus fixture path, sorted — the writeback path
    needs the originating file, not just the parsed entry."""
    return sorted(_CORPUS_ROOT.glob("*/*.yaml"))


def _load_corpus() -> list[ParityCorpusEntry]:
    """Enumerate every ``*.yaml`` under the corpus root, sorted."""
    return [_load_corpus_entry(p) for p in _load_corpus_paths()]


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


def _serialise_cassette_step(step: Any) -> Any:
    """Inverse of :func:`_normalise_cassette`'s per-step transform:
    :class:`ScriptedLLMClient`'s tuple shape back to the YAML-writable
    dict shape ``judge_scripts`` cassettes are authored in."""
    if isinstance(step, str):
        return step
    return [{"name": name, "arguments": dict(arguments)} for name, arguments in step]


def _write_cassette(
    yaml_path: Path,
    kind_name: str,
    recorded_script: list[Any],
) -> None:
    """Rewrite one fixture's ``judge_scripts[kind_name]`` cassette in
    place, preserving every other top-level key."""
    data = yaml.safe_load(yaml_path.read_text())
    data.setdefault("judge_scripts", {})[kind_name] = [
        _serialise_cassette_step(step) for step in recorded_script
    ]
    yaml_path.write_text(yaml.safe_dump(data, sort_keys=False))


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
    :class:`ScriptedLLMClient` for each :meth:`build` call the kind makes
    on the corpus in every replay.

    The harness calls the returned factory ONCE per replay and reuses the
    provider for every entry in that replay; the factory delegates to a
    provider whose :meth:`build` pops the next scripted client off a
    per-replay pool. The pool is one client per entry (single-client kind
    shape), so replay N's client stream is a byte-for-byte clone of
    replay 0's.
    """

    def _factory(_replay_index: int) -> JudgeModelProvider:
        remaining = [ScriptedLLMClient(_cassette_for(entry, kind_name)) for entry in corpus]

        class _PoolProvider:
            def build(self, model_config: ModelConfig) -> JudgeModel:  # noqa: ARG002
                return remaining.pop(0)

        return _PoolProvider()

    return _factory


def _cassette_provider(corpus: list[ParityCorpusEntry], kind_name: str) -> JudgeModelProvider:
    """Single-replay provider for cross-kind dispatch — pops one scripted
    client per :meth:`build` call in corpus order."""
    return _cassette_provider_factory(corpus, kind_name)(0)


def _wrapped_kind_provider_factory(
    corpus: list[ParityCorpusEntry],
    wrapped_cassette_name: str,
    builds_per_entry: int,
) -> Callable[[int], JudgeModelProvider]:
    """Provider factory for a wrapper kind whose wrapped kind calls
    ``build()`` exactly once per ``evaluate``.

    ``voted_rubric`` calls its wrapped kind K times per corpus entry;
    each wrapped call pops one client. All K samples share the SAME
    cassette (``entry.judge_scripts[wrapped_cassette_name]``), so the K
    per-sample results are byte-identical and cross-kind κ against the
    wrapped kind's own single-shot replay lands at 1.0 — the invariant
    the deleted parity assertions locked.
    """

    def _factory(_replay_index: int) -> JudgeModelProvider:
        remaining = [
            ScriptedLLMClient(_cassette_for(entry, wrapped_cassette_name))
            for entry in corpus
            for _ in range(builds_per_entry)
        ]

        class _PoolProvider:
            def build(self, model_config: ModelConfig) -> JudgeModel:  # noqa: ARG002
                return remaining.pop(0)

        return _PoolProvider()

    return _factory


def _wrapped_kind_provider(
    corpus: list[ParityCorpusEntry],
    wrapped_cassette_name: str,
    builds_per_entry: int,
) -> JudgeModelProvider:
    """Single-replay peer of :func:`_wrapped_kind_provider_factory` for
    cross-kind dispatch."""
    return _wrapped_kind_provider_factory(corpus, wrapped_cassette_name, builds_per_entry)(0)


class _RecordingJudgeModelProvider:
    """:class:`JudgeModelProvider` that wraps a real (live) provider so
    every :meth:`build` call's client is a :class:`RecordingLLMClient`,
    collected in call order."""

    def __init__(self, delegate: JudgeModelProvider) -> None:
        self._delegate = delegate
        self.recorders: list[RecordingLLMClient] = []

    def build(self, model_config: ModelConfig) -> JudgeModel:
        recorder = RecordingLLMClient(self._delegate.build(model_config))
        self.recorders.append(recorder)
        return recorder


def _record_live_script(
    entry: ParityCorpusEntry,
    kind: JudgeKind,
    *,
    judge_model_config: ModelConfig,
    kind_config: Mapping[str, Any] | None,
    logger: StructuredLogger,
) -> list[Any]:
    """Drive ``kind.evaluate()`` against a real (live) judge model for one
    corpus entry, recording the single dispatched client's turns."""
    from tolokaforge.core.plugin_registry import load_judge_model_provider

    provider = _RecordingJudgeModelProvider(load_judge_model_provider("litellm")())
    kind.evaluate(
        **_evaluate_kwargs(
            entry=entry,
            judge_model_config=judge_model_config,
            judge_model_provider=provider,
            db_reader=None,
            kb_search=None,
            workspace_dir=None,
            extra_read_tools=(),
            kind_config=kind_config,
            logger=logger,
        )
    )
    (recorder,) = provider.recorders
    return recorder.recorded_script


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
# Cassette-mode gate behaviour.
# ---------------------------------------------------------------------------


def _thresholds() -> ParityGateThresholds:
    return ParityGateThresholds()


def _single_shot_kind() -> SingleShotRubricJudgeKind:
    return SingleShotRubricJudgeKind()


@contextmanager
def _cassette_credentials() -> Iterator[None]:
    """Placeholder credential seam preserved for kinds whose preflight
    checks a credential (none today under this pared-down parity lane).
    """
    yield


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


def test_voted_self_parity_ships() -> None:
    """Five deterministic replays of ``voted_rubric`` (K=3 wrapping
    ``single_shot_rubric``) on the corpus produce per-criterion κ = 1.0
    across the whole pool — every K sample within one replay is
    byte-identical (all K clients seeded from the same
    ``judge_scripts.single_shot_rubric`` cassette), so replays produce
    the same aggregated verdicts. Positive lock on the shipped kind."""
    corpus = _load_corpus()
    report = measure_self_consistency(
        kind_factory=lambda _i: VotedRubricJudgeKind(),
        corpus=corpus,
        replays=5,
        judge_model_config=_JUDGE_MODEL,
        provider_factory=_wrapped_kind_provider_factory(
            corpus, "single_shot_rubric", builds_per_entry=DEFAULT_N_SAMPLES
        ),
    )
    decision = decide_parity_gate(report, thresholds=_thresholds(), measurement="self_consistency")
    assert decision.shippable is True, f"self-parity blocked on {decision.blocking_criteria!r}"
    assert decision.blocking_criteria == ()
    assert decision.warning_criteria == ()


def test_cross_kind_voted_vs_single_shot_ships() -> None:
    """``voted_rubric`` (K=3 wrapping ``single_shot_rubric``) vs
    ``single_shot_rubric`` produces per-criterion κ = 1.0 in cassette
    mode — the wrapped kind's K samples all share the reference's
    cassette, so the aggregated verdict matches the reference verdict
    byte-for-byte. This is the invariant the deleted cross-kind
    assertion locked."""
    corpus = _load_corpus()
    report = measure_cross_kind_agreement(
        reference_kind=_single_shot_kind(),
        candidate_kind=VotedRubricJudgeKind(),
        corpus=corpus,
        judge_model_config=_JUDGE_MODEL,
        reference_provider=_cassette_provider(corpus, "single_shot_rubric"),
        candidate_provider=_wrapped_kind_provider(
            corpus, "single_shot_rubric", builds_per_entry=DEFAULT_N_SAMPLES
        ),
    )
    decision = decide_parity_gate(report, thresholds=_thresholds(), measurement="cross_kind")
    assert decision.shippable is True, f"cross-kind blocked on {decision.blocking_criteria!r}"
    assert decision.blocking_criteria == ()


def _build_auto_anchored_corpus() -> list[ParityCorpusEntry]:
    """Three inline fixtures for the auto-anchored parity lane.

    Each fixture carries a unique rubric with one unanchored ``graded``
    criterion so ``auto_anchored_rubric`` triggers its warm-up call
    every time (the process-wide anchor cache is keyed on rubric hash;
    distinct rubrics guarantee cache misses per fixture rather than
    hits, so build-call counts stay predictable across fixtures).

    Two scoring cassettes ship per fixture:

    - ``single_shot_rubric`` — includes the ``<id>_interpretation``
      slot the schema requires when a graded criterion is unanchored
      (``expected is None``).
    - ``auto_anchored_wrapped`` — omits the interpretation slot,
      because ``auto_anchored_rubric``'s warm-up fills the
      criterion's ``expected`` field before dispatching the wrapped
      kind, and the wrapped-kind schema then rejects the (now
      unknown) interpretation key.
    """
    from tolokaforge.runner.models import Criterion  # local import — narrow, in-test only

    entries: list[ParityCorpusEntry] = []
    for i in range(3):
        # Shared criterion ids across fixtures so per-criterion κ pools
        # observations (Cohen's κ is undefined for n=1). Descriptions
        # carry the fixture index so each rubric hashes to a distinct
        # key in the process-wide anchor cache, keeping the build-call
        # count predictable per replay.
        criteria = [
            Criterion(
                id="aa_binary",
                description=f"binary criterion (fixture {i})",
                kind="binary",
                weight=1.0,
            ),
            Criterion(
                id="aa_graded",
                description=f"graded criterion (fixture {i})",
                kind="graded",
                weight=1.0,
            ),
        ]
        rubric = Rubric(criteria=criteria)
        # Vary the fixture-level verdict so the pooled per-criterion κ
        # has label variation across the corpus — κ's chance-agreement
        # denominator collapses to zero when every observation carries
        # the same label, and the gate would then read "undefined"
        # rather than "identical".
        binary_met = i % 2 == 0
        graded_score = 0.9 if i % 2 == 0 else 0.2
        entries.append(
            ParityCorpusEntry(
                entry_id=f"auto_anchored_synth_{i:02d}",
                rubric=rubric,
                agent_system_prompt=f"synthetic agent prompt {i}",
                transcript=[{"role": "user", "content": f"synthetic user turn {i}"}],
                state_diff=None,
                judge_scripts={
                    "single_shot_rubric": [
                        _submit_scoring_step(
                            criteria,
                            include_interpretation=True,
                            binary_met=binary_met,
                            graded_score=graded_score,
                        )
                    ],
                    "auto_anchored_wrapped": [
                        _submit_scoring_step(
                            criteria,
                            include_interpretation=False,
                            binary_met=binary_met,
                            graded_score=graded_score,
                        )
                    ],
                },
            )
        )
    return entries


def _submit_scoring_step(
    criteria: list[Any],
    *,
    include_interpretation: bool,
    binary_met: bool = True,
    graded_score: float = 0.9,
) -> list[tuple[str, dict[str, Any]]]:
    """Build a scripted ``submit_report`` step covering every criterion id.

    Binary criteria get ``met=True`` + a ``VERDICT: MET`` justification;
    graded criteria get ``score=0.9`` + a ``SCORE: 0.9`` justification.
    ``include_interpretation`` toggles the per-criterion interpretation
    slot the unanchored-graded schema requires; a caller emitting the
    scoring turn for a wrapped judge whose rubric has been anchored by
    ``auto_anchored_rubric`` must pass ``False``, because the anchored
    schema no longer allows the key."""
    args: dict[str, Any] = {"reasons": "overall summary — synthetic fixture"}
    for c in criteria:
        if c.kind == "graded":
            args[c.id] = graded_score
            args[f"{c.id}_justification"] = f"because {c.id}\nSCORE: {graded_score}"
        else:
            marker = "MET" if binary_met else "NOT MET"
            args[c.id] = binary_met
            args[f"{c.id}_justification"] = f"because {c.id}\nVERDICT: {marker}"
        if include_interpretation and c.kind == "graded" and c.expected is None:
            args[f"{c.id}_interpretation"] = f"interpretation for {c.id}"
    return [("submit_report", args)]


def _auto_anchored_provider_factory(
    corpus: list[ParityCorpusEntry],
) -> Callable[[int], JudgeModelProvider]:
    """Provider factory for ``auto_anchored_rubric`` self-consistency.

    Two clients per entry: a warm-up client whose single turn is a JSON
    anchor map covering every unanchored graded criterion, then a
    wrapped scoring client seeded from the entry's
    ``judge_scripts.single_shot_rubric`` cassette. The anchor cache is
    cleared in the ``_auto_anchored_cache`` fixture on the test and
    :func:`_auto_anchored_kind_factory` clears it again per replay so
    every replay drives both build calls."""

    def _factory(_replay_index: int) -> JudgeModelProvider:
        remaining: list[ScriptedLLMClient] = []
        for entry in corpus:
            unanchored = tuple(
                c.id for c in entry.rubric.criteria if c.kind == "graded" and c.expected is None
            )
            if unanchored:
                anchor_map = {cid: f"one-sentence anchor for {cid}" for cid in unanchored}
                remaining.append(ScriptedLLMClient([json.dumps(anchor_map)]))
            remaining.append(ScriptedLLMClient(_cassette_for(entry, "auto_anchored_wrapped")))

        class _PoolProvider:
            def build(self, model_config: ModelConfig) -> JudgeModel:  # noqa: ARG002
                return remaining.pop(0)

        return _PoolProvider()

    return _factory


def _auto_anchored_kind_factory(_replay_index: int) -> AutoAnchoredRubricJudgeKind:
    """Clear the process-wide anchor cache before each replay so every
    replay drives the warm-up call (otherwise replay 0 populates the
    cache and replays 1..N-1 skip the warm-up, and the build-call pool
    the sibling provider factory constructs runs long)."""
    clear_anchor_cache()
    return AutoAnchoredRubricJudgeKind()


@pytest.fixture
def _auto_anchored_cache() -> Iterator[None]:
    """Test-scope guard around ``auto_anchored_rubric``'s process-wide
    anchor cache — cleared before and after so a test never inherits a
    warm cache or leaks one to its neighbour."""
    clear_anchor_cache()
    try:
        yield
    finally:
        clear_anchor_cache()


def test_auto_anchored_self_parity_ships(_auto_anchored_cache: None) -> None:  # noqa: ARG001
    """Two replays of ``auto_anchored_rubric`` (default wrapping
    ``single_shot_rubric``) on a 3-fixture synthetic corpus produce
    per-criterion κ = 1.0 — the warm-up cassette lands a deterministic
    anchor map and the wrapped scoring cassette is byte-identical
    across replays. Uses a synthetic in-line corpus because adding
    warm-up + wrapped scripts to all 20 canonical fixtures would balloon
    the fix pass (see :mod:`auto_anchored` for the warm-up + wrapped
    dispatch shape this test locks)."""
    corpus = _build_auto_anchored_corpus()
    report = measure_self_consistency(
        kind_factory=_auto_anchored_kind_factory,
        corpus=corpus,
        replays=2,
        judge_model_config=_JUDGE_MODEL,
        provider_factory=_auto_anchored_provider_factory(corpus),
    )
    decision = decide_parity_gate(report, thresholds=_thresholds(), measurement="self_consistency")
    assert decision.shippable is True, f"self-parity blocked on {decision.blocking_criteria!r}"
    assert decision.blocking_criteria == ()
    assert decision.warning_criteria == ()


def test_cross_kind_auto_anchored_vs_single_shot_ships(
    _auto_anchored_cache: None,  # noqa: ARG001
) -> None:
    """``auto_anchored_rubric`` (default wrapping ``single_shot_rubric``)
    vs ``single_shot_rubric`` on the synthetic corpus lands
    per-criterion κ = 1.0. The wrapped scoring client is seeded from
    the same cassette both legs draw from, so the aggregated verdicts
    match byte-for-byte — the auto-anchor step only rewrites the
    prompt fed to the wrapped judge, and the scripted client's output
    is prompt-invariant."""
    corpus = _build_auto_anchored_corpus()
    report = measure_cross_kind_agreement(
        reference_kind=_single_shot_kind(),
        candidate_kind=AutoAnchoredRubricJudgeKind(),
        corpus=corpus,
        judge_model_config=_JUDGE_MODEL,
        reference_provider=_cassette_provider(corpus, "single_shot_rubric"),
        candidate_provider=_auto_anchored_provider_factory(corpus)(0),
    )
    decision = decide_parity_gate(report, thresholds=_thresholds(), measurement="cross_kind")
    assert decision.shippable is True, f"cross-kind blocked on {decision.blocking_criteria!r}"
    assert decision.blocking_criteria == ()


def _multi_turn_provider_factory(
    corpus: list[ParityCorpusEntry],
) -> Callable[[int], JudgeModelProvider]:
    """Provider factory for ``multi_turn_rubric`` self-consistency.

    The stack is ``voted(K=3) → auto_anchored → single_shot``. Per
    fixture per replay, the anchor cache is cleared upstream so the
    first sample runs the warm-up (2 builds: warmup + wrapped scoring)
    and the remaining K-1 samples hit the cache (1 build each: wrapped
    scoring only). Total: 1 warm-up client + K wrapped scoring clients.
    """

    def _factory(_replay_index: int) -> JudgeModelProvider:
        remaining: list[ScriptedLLMClient] = []
        for entry in corpus:
            unanchored = tuple(
                c.id for c in entry.rubric.criteria if c.kind == "graded" and c.expected is None
            )
            if unanchored:
                anchor_map = {cid: f"one-sentence anchor for {cid}" for cid in unanchored}
                remaining.append(ScriptedLLMClient([json.dumps(anchor_map)]))
            for _ in range(DEFAULT_N_SAMPLES):
                remaining.append(ScriptedLLMClient(_cassette_for(entry, "auto_anchored_wrapped")))

        class _PoolProvider:
            def build(self, model_config: ModelConfig) -> JudgeModel:  # noqa: ARG002
                return remaining.pop(0)

        return _PoolProvider()

    return _factory


def _multi_turn_kind_factory(_replay_index: int) -> MultiTurnRubricJudgeKind:
    """Clear the process-wide anchor cache before each replay so both
    replays drive the warm-up call and the sibling provider factory's
    pooled client stream matches the observed build order."""
    clear_anchor_cache()
    return MultiTurnRubricJudgeKind()


def test_multi_turn_self_parity_ships(_auto_anchored_cache: None) -> None:  # noqa: ARG001
    """Two replays of ``multi_turn_rubric`` on the synthetic corpus
    produce per-criterion κ = 1.0 — the warm-up cassette lands a
    deterministic anchor map and all K wrapped scoring cassettes are
    byte-identical across samples and replays, so voted's aggregated
    verdict is identical in every replay. Uses the same synthetic
    inline corpus as ``test_auto_anchored_self_parity_ships`` to keep
    the fix pass narrow (see plan Step 6)."""
    corpus = _build_auto_anchored_corpus()
    report = measure_self_consistency(
        kind_factory=_multi_turn_kind_factory,
        corpus=corpus,
        replays=2,
        judge_model_config=_JUDGE_MODEL,
        provider_factory=_multi_turn_provider_factory(corpus),
    )
    decision = decide_parity_gate(report, thresholds=_thresholds(), measurement="self_consistency")
    assert decision.shippable is True, f"self-parity blocked on {decision.blocking_criteria!r}"
    assert decision.blocking_criteria == ()
    assert decision.warning_criteria == ()


def _build_auto_selector_corpus() -> list[ParityCorpusEntry]:
    """Two-family corpus for ``auto_rubric`` parity: fully-anchored and
    unanchored fixtures interleaved so both selection branches fire and
    the per-criterion label pool stays label-variant."""
    from tolokaforge.runner.models import Criterion  # local — narrow, in-test only

    entries: list[ParityCorpusEntry] = []
    for i in range(3):
        anchored = i % 2 == 0
        binary_met = i % 2 == 0
        graded_score = 0.9 if i % 2 == 0 else 0.2
        graded = Criterion(
            id="aa_graded",
            description=f"graded criterion (fixture {i})",
            kind="graded",
            weight=1.0,
            expected=f"anchor for fixture {i}" if anchored else None,
        )
        criteria = [
            Criterion(
                id="aa_binary",
                description=f"binary criterion (fixture {i})",
                kind="binary",
                weight=1.0,
            ),
            graded,
        ]
        entries.append(
            ParityCorpusEntry(
                entry_id=f"auto_selector_synth_{i:02d}",
                rubric=Rubric(criteria=criteria),
                agent_system_prompt=f"synthetic agent prompt {i}",
                transcript=[{"role": "user", "content": f"synthetic user turn {i}"}],
                state_diff=None,
                judge_scripts={
                    # single_shot's own cassette — used by auto_rubric when the
                    # rubric is fully anchored, and by auto_anchored's wrapped
                    # dispatch (which itself sees no interpretation slot).
                    "single_shot_rubric": [
                        _submit_scoring_step(
                            criteria,
                            include_interpretation=False,
                            binary_met=binary_met,
                            graded_score=graded_score,
                        )
                    ],
                    "auto_anchored_wrapped": [
                        _submit_scoring_step(
                            criteria,
                            include_interpretation=False,
                            binary_met=binary_met,
                            graded_score=graded_score,
                        )
                    ],
                },
            )
        )
    return entries


def _auto_selector_provider_factory(
    corpus: list[ParityCorpusEntry],
) -> Callable[[int], JudgeModelProvider]:
    """Provider factory covering both ``auto_rubric`` dispatch branches.

    For each fixture in order: an anchored fixture routes to
    ``single_shot_rubric`` (1 build per replay); an unanchored fixture
    routes to ``multi_turn_rubric`` (1 warm-up + K wrapped scoring
    builds per replay)."""

    def _factory(_replay_index: int) -> JudgeModelProvider:
        remaining: list[ScriptedLLMClient] = []
        for entry in corpus:
            unanchored = tuple(
                c.id for c in entry.rubric.criteria if c.kind == "graded" and c.expected is None
            )
            if unanchored:
                anchor_map = {cid: f"one-sentence anchor for {cid}" for cid in unanchored}
                remaining.append(ScriptedLLMClient([json.dumps(anchor_map)]))
                for _ in range(DEFAULT_N_SAMPLES):
                    remaining.append(
                        ScriptedLLMClient(_cassette_for(entry, "auto_anchored_wrapped"))
                    )
            else:
                remaining.append(ScriptedLLMClient(_cassette_for(entry, "single_shot_rubric")))

        class _PoolProvider:
            def build(self, model_config: ModelConfig) -> JudgeModel:  # noqa: ARG002
                return remaining.pop(0)

        return _PoolProvider()

    return _factory


def _auto_selector_kind_factory(_replay_index: int) -> AutoRubricJudgeKind:
    """Clear the process-wide anchor cache before each replay so the
    unanchored-rubric branch drives the warm-up call on every replay."""
    clear_anchor_cache()
    return AutoRubricJudgeKind()


def test_auto_self_parity_ships(_auto_anchored_cache: None) -> None:  # noqa: ARG001
    """Two replays of ``auto_rubric`` on a mixed anchored/unanchored
    synthetic corpus produce per-criterion κ = 1.0 across both dispatch
    branches. The selector is deterministic (rubric shape only), so a
    fixture routes to the same wrapped kind on every replay; both wrapped
    kinds are byte-deterministic on their cassettes."""
    corpus = _build_auto_selector_corpus()
    report = measure_self_consistency(
        kind_factory=_auto_selector_kind_factory,
        corpus=corpus,
        replays=2,
        judge_model_config=_JUDGE_MODEL,
        provider_factory=_auto_selector_provider_factory(corpus),
    )
    decision = decide_parity_gate(report, thresholds=_thresholds(), measurement="self_consistency")
    assert decision.shippable is True, f"self-parity blocked on {decision.blocking_criteria!r}"
    assert decision.blocking_criteria == ()
    assert decision.warning_criteria == ()


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

    - **Inner sum** < 60 s across the ``measure_*`` calls (kind work
      only, excludes corpus load).
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
# Writeback mechanics — locked keyless (no live client involved).
# ---------------------------------------------------------------------------


def test_writeback_rewrites_cassette_preserving_other_keys(tmp_path: Path) -> None:
    """:func:`_write_cassette` rewrites only ``judge_scripts[kind_name]``
    and leaves every other top-level key byte-for-byte equal to the
    source fixture — the contract the live ``--live-parity`` writeback
    path depends on to avoid clobbering unrelated cassettes when it
    refreshes one kind."""
    source_path = _load_corpus_paths()[0]
    original = yaml.safe_load(source_path.read_text())
    working_copy = tmp_path / source_path.name
    working_copy.write_text(source_path.read_text())

    script = [
        "plain text verdict",
        [("submit_report", {"criteria": [{"id": "c1", "met": True}]})],
    ]
    recorder = RecordingLLMClient(ScriptedLLMClient(list(script)))
    for _ in script:
        recorder.generate(system="sys", messages=[], tools=[])

    _write_cassette(working_copy, "single_shot_rubric", recorder.recorded_script)

    rewritten = yaml.safe_load(working_copy.read_text())
    assert rewritten["judge_scripts"]["single_shot_rubric"] == [
        "plain text verdict",
        [{"name": "submit_report", "arguments": {"criteria": [{"id": "c1", "met": True}]}}],
    ]
    for key in original:
        if key == "judge_scripts":
            continue
        assert rewritten[key] == original[key], f"unrelated key {key!r} was rewritten"


# ---------------------------------------------------------------------------
# Live mode — opt-in via --live-parity, gated behind an API key.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_live_mode_writeback(request: pytest.FixtureRequest) -> None:
    """Last-mile sanity check, not the sole lock (see the keyless
    ``unit``-tier :mod:`tests.utils.test_recording_llm_client` and
    :func:`test_writeback_rewrites_cassette_preserving_other_keys` above
    for the behaviour this test only re-confirms end-to-end).

    Skips unless both ``--live-parity`` is passed AND one of
    ``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY`` is set — never runs in CI.
    Drives every corpus entry through ``single_shot_rubric`` against a
    real judge model, rewrites each fixture's cassette, then repeats the
    whole pass a second time and asserts the second pass's recorded
    scripts equal the first pass's — the writeback is idempotent under
    repeated live runs."""
    if not request.config.getoption("--live-parity"):
        pytest.skip("live-parity mode disabled — pass --live-parity to opt in")
    if not any(os.environ.get(k) for k in _LIVE_API_KEYS):
        pytest.skip(f"live-parity requires one of {_LIVE_API_KEYS!r} in the process env")

    corpus_paths = _load_corpus_paths()
    kinds: list[tuple[str, JudgeKind, Mapping[str, Any] | None]] = [
        ("single_shot_rubric", _single_shot_kind(), None),
    ]
    logger = StructuredLogger(name="test-judge-kind-parity-live")

    def _run_pass() -> dict[tuple[str, str], list[Any]]:
        recorded: dict[tuple[str, str], list[Any]] = {}
        for path in corpus_paths:
            entry = _load_corpus_entry(path)
            for kind_name, kind, kind_config in kinds:
                script = _record_live_script(
                    entry,
                    kind,
                    judge_model_config=_JUDGE_MODEL,
                    kind_config=kind_config,
                    logger=logger,
                )
                _write_cassette(path, kind_name, script)
                recorded[(entry.entry_id, kind_name)] = script
        return recorded

    first_pass = _run_pass()
    second_pass = _run_pass()
    idempotence_msg = "--live-parity writeback is not idempotent across repeated live runs"
    assert second_pass == first_pass, idempotence_msg


# ---------------------------------------------------------------------------
# Logger used across tests (keeps the harness's default from firing).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _logger() -> StructuredLogger:
    return StructuredLogger(name="test-judge-kind-parity")
