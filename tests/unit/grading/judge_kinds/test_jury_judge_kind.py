"""Unit tests for :class:`JuryRubricJudgeKind`.

Exercises the panel-dispatch loop (each member a DIFFERENT model, not a
repeated sample), the credential preflight, the merge contract, the
``kind_config`` schema, the aggregator selection (`median` / `majority`
end to end), and the fail-loud contract (any member ERRORED or missing a
criterion verdict yields a whole-trial ERRORED result naming the member's
index AND provider/name; construction-field divergence across members
raises).

Every case drives a scripted :class:`JudgeModelProvider` that records the
:class:`ModelConfig` it received per ``build`` call and pops one fresh
:class:`ScriptedLLMClient` — so the N panel members are deterministic and
each sees its own script. The default wrapped kind (``single_shot_rubric``)
builds exactly one client per ``evaluate`` call, so N members consume
exactly N clients.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.utils.scripted_llm_client import ScriptedLLMClient
from tolokaforge.core.grading.judge_kinds import DEFAULT_PANEL, JuryRubricJudgeKind
from tolokaforge.core.grading.judge_kinds._shared import member_failure_reason
from tolokaforge.core.grading.judge_kinds.jury import _merge_member_results
from tolokaforge.core.grading.judge_result import JudgeResult, JudgeStatus, JudgeUsage
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import ModelConfig
from tolokaforge.core.plugin_registry import UnknownImplementationError
from tolokaforge.runner.models import Criterion, CriterionResult, Rubric
from tolokaforge.secrets import SecretManager, init_default_from
from tolokaforge.secrets import manager as manager_module

pytestmark = pytest.mark.unit


_JUDGE_MODEL = ModelConfig(provider="openai", name="gpt-4o-mini", temperature=0.0)


@pytest.fixture(autouse=True)
def _reset_secret_singleton():
    """Each test gets a fresh SecretManager singleton — no leakage between cases."""
    original = manager_module._default_manager
    manager_module._default_manager = None
    yield
    manager_module._default_manager = original


def _seed_secrets(**secrets: str) -> None:
    init_default_from(SecretManager.from_dict(secrets))


class _RecordingProvider:
    """Scripted :class:`JudgeModelProvider` that pops one client per ``build``
    and records every :class:`ModelConfig` it was asked to build."""

    def __init__(self, clients: list[ScriptedLLMClient]) -> None:
        self._clients = list(clients)
        self.built_configs: list[ModelConfig] = []

    def build(self, model_config: ModelConfig):
        self.built_configs.append(model_config)
        if not self._clients:
            raise AssertionError(
                "provider ran out of scripted clients; a panel member called build "
                "more times than the test expected."
            )
        return self._clients.pop(0)


def _binary_rubric(n: int, *, prefix: str = "c") -> Rubric:
    """``n`` non-required binary criteria."""
    return Rubric(
        criteria=[
            Criterion(id=f"{prefix}{i}", description=f"criterion {i}", kind="binary", weight=1.0)
            for i in range(n)
        ]
    )


def _submit_call(criteria: list[Criterion], verdicts: dict[str, bool] | None = None) -> list:
    """One ``submit_report`` tool-call turn covering every ``criteria`` id with ``verdicts``."""
    v = verdicts or {}
    args: dict[str, Any] = {"reasons": "overall summary"}
    for c in criteria:
        met = v.get(c.id, True)
        args[c.id] = met
        args[f"{c.id}_justification"] = f"because {c.id}\nVERDICT: {'MET' if met else 'NOT MET'}"
    return [("submit_report", args)]


def _clients(scripts: list[list]) -> list[ScriptedLLMClient]:
    return [ScriptedLLMClient(script) for script in scripts]


def _evaluate(
    rubric: Rubric,
    *,
    provider: _RecordingProvider,
    kind_config: dict[str, Any] | None,
) -> JudgeResult:
    """Drive :meth:`JuryRubricJudgeKind.evaluate` with a minimal input surface."""
    kind = JuryRubricJudgeKind()
    return kind.evaluate(
        rubric=rubric,
        agent_system_prompt="you are an agent",
        transcript=[{"role": "user", "content": "hi"}],
        db_reader=None,
        kb_search=None,
        workspace_dir=None,
        extra_read_tools=[],
        state_diff=None,
        judge_model_config=_JUDGE_MODEL,
        judge_model_provider=provider,
        disable_knowledge_search=False,
        custom_system_prompt=None,
        include_agent_system_prompt=True,
        kind_config=kind_config,
        logger=StructuredLogger(name="test-jury"),
    )


def test_default_panel_dispatches_three_distinct_models() -> None:
    """Default panel, all clean scripts → merged result, usage.calls == 3, and
    every dispatched ModelConfig differs in provider/name matching the panel."""
    _seed_secrets(OPENROUTER_API_KEY="sk-test")
    rubric = _binary_rubric(2)
    scripts = [[_submit_call(rubric.criteria)] for _ in range(len(DEFAULT_PANEL))]
    provider = _RecordingProvider(_clients(scripts))

    result = _evaluate(rubric, provider=provider, kind_config=None)

    assert result.status is JudgeStatus.COMPLETED
    assert result.usage.calls == len(DEFAULT_PANEL)
    built_pairs = [(c.provider, c.name) for c in provider.built_configs]
    expected_pairs = [(m["provider"], m["name"]) for m in DEFAULT_PANEL]
    assert built_pairs == expected_pairs
    assert len(set(built_pairs)) == len(DEFAULT_PANEL)
    assert all(cr.met and cr.score == pytest.approx(1.0) for cr in result.criterion_results)


def test_merges_composition_and_gate_fields() -> None:
    """Locks the cross-member merge: reasons joined, failed_required_ids re-derived,
    transcript concatenated, construction-flag fields from member 0, and the
    per-criterion justification carries the panel-labelled aggregator audit trail."""
    _seed_secrets(OPENROUTER_API_KEY="sk-test")
    rubric = Rubric(
        criteria=[
            Criterion(id="a0", description="a0", kind="binary", weight=1.0),
            Criterion(id="a1", description="a1", kind="binary", weight=1.0, required=True),
        ]
    )
    scripts = [[_submit_call(rubric.criteria, verdicts={"a1": False})] for _ in range(3)]
    provider = _RecordingProvider(_clients(scripts))

    result = _evaluate(rubric, provider=provider, kind_config=None)

    assert result.status is JudgeStatus.COMPLETED
    assert result.reasons.count("overall summary") == 3
    assert "\n\n" in result.reasons
    assert result.failed_required_ids == ("a1",)
    assert result.gate_failed is True
    assert result.chunk_boundaries == ()
    justification = result.criterion_results[0].justification
    assert "aggregator=geometric_median" in justification
    assert "N=3" in justification
    assert f"[panel 0: {DEFAULT_PANEL[0]['provider']}/{DEFAULT_PANEL[0]['name']}]" in justification
    assert f"[panel 2: {DEFAULT_PANEL[2]['provider']}/{DEFAULT_PANEL[2]['name']}]" in justification


@pytest.mark.parametrize(
    ("kind_config", "expected_error_fragment"),
    [
        (None, None),
        ({"unknown_key": "x"}, "unknown_key"),
        ({"panel": "not-a-list"}, "must be a list or tuple"),
        (
            {"panel": [{"provider": "openrouter", "name": "m"}]},
            "K<2 makes voting undefined",
        ),
        (
            {"panel": [{"name": "m0"}, {"provider": "openrouter", "name": "m1"}]},
            "must have a non-empty str 'provider'",
        ),
        (
            {"panel": [{"provider": "openrouter"}, {"provider": "openrouter", "name": "m1"}]},
            "must have a non-empty str 'name'",
        ),
        (
            {
                "panel": [
                    {"provider": "openrouter", "name": "m0", "unknown": 1},
                    {"provider": "openrouter", "name": "m1"},
                ]
            },
            "unknown key",
        ),
        (
            {
                "panel": [
                    {"provider": "openrouter", "name": "m0", "temperature": "hot"},
                    {"provider": "openrouter", "name": "m1"},
                ]
            },
            "temperature must be an int or float",
        ),
        ({"aggregator": "mean"}, "unknown aggregator"),
        (
            {
                "panel": [
                    {"provider": "openrouter", "name": "m0"},
                    {"provider": "openrouter", "name": "m1"},
                    {"provider": "openrouter", "name": "m2"},
                    {"provider": "openrouter", "name": "m3"},
                ],
                "aggregator": "majority",
            },
            "odd n_samples",
        ),
        ({"wrapped_kind": "does_not_exist"}, "does_not_exist"),
    ],
)
def test_kind_config_schema(
    kind_config: dict[str, Any] | None,
    expected_error_fragment: str | None,
) -> None:
    """``kind_config`` is validated at ``evaluate`` entry before any judge call."""
    _seed_secrets(OPENROUTER_API_KEY="sk-test")
    rubric = _binary_rubric(2)
    if expected_error_fragment is None:
        panel = kind_config.get("panel", DEFAULT_PANEL) if kind_config else DEFAULT_PANEL
        provider = _RecordingProvider(_clients([[_submit_call(rubric.criteria)] for _ in panel]))
        result = _evaluate(rubric, provider=provider, kind_config=kind_config)
        assert result.status is JudgeStatus.COMPLETED
        assert result.usage.calls == len(panel)
        return

    provider = _RecordingProvider([])
    if kind_config == {"wrapped_kind": "does_not_exist"}:
        with pytest.raises(UnknownImplementationError):
            _evaluate(rubric, provider=provider, kind_config=kind_config)
    else:
        with pytest.raises(ValueError, match=expected_error_fragment):
            _evaluate(rubric, provider=provider, kind_config=kind_config)
    assert provider.built_configs == []


def test_majority_rejects_non_binary_rubric() -> None:
    _seed_secrets(OPENROUTER_API_KEY="sk-test")
    rubric = Rubric(
        criteria=[
            Criterion(id="a0", description="a0", kind="binary", weight=1.0),
            Criterion(id="clarity", description="clarity", kind="graded", weight=1.0),
        ]
    )
    provider = _RecordingProvider([])
    panel = [
        {"provider": "openrouter", "name": "m0"},
        {"provider": "openrouter", "name": "m1"},
        {"provider": "openrouter", "name": "m2"},
    ]
    with pytest.raises(ValueError, match="all-binary rubric"):
        _evaluate(rubric, provider=provider, kind_config={"panel": panel, "aggregator": "majority"})
    assert provider.built_configs == []


def test_credential_preflight_fails_loud_naming_missing_provider_and_candidates() -> None:
    """No OpenRouter credential seeded → ValueError naming openrouter and both
    candidate env-var names, BEFORE any panel member's client is built."""
    _seed_secrets()  # empty — nothing seeded
    rubric = _binary_rubric(2)
    provider = _RecordingProvider([])

    with pytest.raises(ValueError, match="openrouter") as excinfo:
        _evaluate(rubric, provider=provider, kind_config=None)

    message = str(excinfo.value)
    assert "OPENROUTER_API_KEYS" in message
    assert "OPENROUTER_API_KEY" in message
    assert provider.built_configs == []


def test_credential_preflight_passes_when_single_shared_key_present() -> None:
    """Only OPENROUTER_API_KEY present covers all 3 default-panel members
    (they share provider: openrouter)."""
    _seed_secrets(OPENROUTER_API_KEY="sk-test")
    rubric = _binary_rubric(2)
    scripts = [[_submit_call(rubric.criteria)] for _ in range(len(DEFAULT_PANEL))]
    provider = _RecordingProvider(_clients(scripts))

    result = _evaluate(rubric, provider=provider, kind_config=None)

    assert result.status is JudgeStatus.COMPLETED
    assert len(provider.built_configs) == len(DEFAULT_PANEL)


def test_credential_preflight_names_only_the_missing_provider_in_mixed_panel() -> None:
    """Mixed panel (openrouter + direct openai) missing OPENAI_API_KEY but with
    OPENROUTER_API_KEY present → ValueError naming only openai, not openrouter."""
    _seed_secrets(OPENROUTER_API_KEY="sk-test")
    rubric = _binary_rubric(2)
    panel = [
        {"provider": "openrouter", "name": "m0"},
        {"provider": "openai", "name": "m1"},
    ]
    provider = _RecordingProvider([])

    with pytest.raises(ValueError) as excinfo:
        _evaluate(rubric, provider=provider, kind_config={"panel": panel})

    message = str(excinfo.value)
    assert "openai" in message
    assert "openrouter" not in message
    assert provider.built_configs == []


def test_fail_loud_when_panel_member_never_completes() -> None:
    """Member 1 of 3 never calls submit_report → whole-trial ERRORED naming its
    index AND provider/name; member 2 never dispatched."""
    _seed_secrets(OPENROUTER_API_KEY="sk-test")
    rubric = _binary_rubric(3)
    scripts = [[_submit_call(rubric.criteria)] for _ in range(3)]
    scripts[1] = ["panel member 1 refuses to call submit_report"] * 25
    provider = _RecordingProvider(_clients(scripts[:2]))  # member 2 must never be built

    result = _evaluate(rubric, provider=provider, kind_config=None)

    assert result.status is JudgeStatus.ERRORED
    assert result.score is None
    assert result.binary_pass is None
    assert result.criterion_results == ()
    assert "panel member 1" in result.reasons
    assert DEFAULT_PANEL[1]["provider"] in result.reasons
    assert DEFAULT_PANEL[1]["name"] in result.reasons
    assert len(provider.built_configs) == 2


def test_fail_loud_on_missing_verdict_in_member() -> None:
    """A member COMPLETED but missing one rubric criterion id is a fail-loud shape
    (reuses the Stage-1 shared helper, locking it against jury's call site)."""
    member_result = JudgeResult(
        status=JudgeStatus.COMPLETED,
        usage=JudgeUsage(),
        reasons="ok",
        criterion_results=(
            CriterionResult(id="c0", met=True, score=1.0, justification="j\nVERDICT: MET"),
        ),
    )
    reason = member_failure_reason(member_result, ("c0", "c1"))
    assert reason is not None
    assert "missing verdicts" in reason
    assert "c1" in reason


def test_construction_field_mismatch_raises() -> None:
    rubric = _binary_rubric(1, prefix="a")
    good = JudgeResult(
        status=JudgeStatus.COMPLETED,
        usage=JudgeUsage(),
        reasons="ok",
        score=1.0,
        binary_pass=True,
        criterion_results=(
            CriterionResult(id="a0", met=True, score=1.0, justification="j\nVERDICT: MET"),
        ),
        custom_system_prompt=False,
    )
    divergent = JudgeResult(
        status=JudgeStatus.COMPLETED,
        usage=JudgeUsage(),
        reasons="ok",
        score=1.0,
        binary_pass=True,
        criterion_results=(
            CriterionResult(id="a0", met=True, score=1.0, justification="j\nVERDICT: MET"),
        ),
        custom_system_prompt=True,
    )
    with pytest.raises(RuntimeError, match="custom_system_prompt"):
        _merge_member_results(
            rubric=rubric,
            member_results=[good, divergent],
            panel=DEFAULT_PANEL[:2],
            aggregator="median",
        )


def test_median_aggregator_end_to_end() -> None:
    """3 members vote [True, True, False] on one binary criterion → median score 1.0."""
    _seed_secrets(OPENROUTER_API_KEY="sk-test")
    rubric = _binary_rubric(1, prefix="a")
    scripts = [
        [_submit_call(rubric.criteria, verdicts={"a0": True})],
        [_submit_call(rubric.criteria, verdicts={"a0": True})],
        [_submit_call(rubric.criteria, verdicts={"a0": False})],
    ]
    provider = _RecordingProvider(_clients(scripts))

    result = _evaluate(rubric, provider=provider, kind_config={"aggregator": "median"})

    assert result.status is JudgeStatus.COMPLETED
    assert result.criterion_results[0].score == pytest.approx(1.0)
    assert result.criterion_results[0].met is True


def test_majority_aggregator_end_to_end() -> None:
    """3 members vote [True, False, False] on one binary criterion → majority score 0.0."""
    _seed_secrets(OPENROUTER_API_KEY="sk-test")
    rubric = _binary_rubric(1, prefix="a")
    scripts = [
        [_submit_call(rubric.criteria, verdicts={"a0": True})],
        [_submit_call(rubric.criteria, verdicts={"a0": False})],
        [_submit_call(rubric.criteria, verdicts={"a0": False})],
    ]
    provider = _RecordingProvider(_clients(scripts))

    result = _evaluate(rubric, provider=provider, kind_config={"aggregator": "majority"})

    assert result.status is JudgeStatus.COMPLETED
    assert result.criterion_results[0].score == 0.0
    assert result.criterion_results[0].met is False


def test_build_panel_model_config_propagates_validation_error_on_openrouter_mismatch() -> None:
    """A base ``judge_model_config`` carrying an ``openrouter:`` routing block
    combined with a panel member whose provider is NOT openrouter surfaces
    ModelConfig's own ValidationError naming the panel member — not swallowed."""
    from pydantic import ValidationError

    from tolokaforge.core.grading.judge_kinds.jury import _build_panel_model_config
    from tolokaforge.core.models import OpenRouterConfig

    base = ModelConfig(
        provider="openrouter",
        name="anthropic/claude-3-haiku",
        openrouter=OpenRouterConfig(provider_order=["Together"]),
    )
    member = {"provider": "openai", "name": "gpt-4o-mini"}

    with pytest.raises(ValidationError):
        _build_panel_model_config(base, member)
