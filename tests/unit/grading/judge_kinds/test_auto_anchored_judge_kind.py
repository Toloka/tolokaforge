"""Unit tests for ``AutoAnchoredRubricJudgeKind``.

Locks the M51 Layer 2 contract:

- One warm-up judge call per unique (rubric, judge_model); a second call in
  the same process hits the cache.
- Only ``kind: graded`` criteria with ``expected is None`` get an auto-anchor;
  author-written anchors are passed through unchanged.
- The wrapped kind sees the synthetic rubric (unanchored graded → anchored).
- Warm-up usage folds into the returned ``JudgeResult.usage``; auto-anchors
  appear in ``JudgeResult.reasons`` for audit.
- Unknown ``kind_config`` keys fail loud.
- Warm-up call that returns non-JSON / missing keys / non-string values
  fails loud with a named error.
- Rubric with no unanchored graded criteria skips the warm-up call entirely.
"""

from __future__ import annotations

from typing import Any

import pytest

from tolokaforge.core.grading.judge_kinds.auto_anchored import (
    AutoAnchoredRubricJudgeKind,
    PerRubricAnchorGeneratorError,
    clear_anchor_cache,
)
from tolokaforge.core.grading.judge_result import JudgeResult, JudgeStatus, JudgeUsage
from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.llm.client import Usage as LLMUsage
from tolokaforge.core.models import ModelConfig
from tolokaforge.runner.models import Criterion, Rubric

pytestmark = pytest.mark.unit


# --- Fakes ---------------------------------------------------------------


class _FakeJudgeModel:
    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.call_count = 0

    def generate(
        self,
        *,
        system: str,
        messages: list[Any],
        tools: list[Any],
        tool_choice: str = "auto",
        observation: Any | None = None,
    ) -> GenerationResult:
        self.call_count += 1
        return GenerationResult(
            text=self.response_text,
            tool_calls=[],
            usage=LLMUsage(prompt_tokens=42, completion_tokens=7),
            cost_usd=0.0001,
        )

    def classify_loop_error(self, exc: Exception) -> Any:  # pragma: no cover
        raise NotImplementedError

    def sanitize_tools_for_execution(self, tools: list[Any]) -> dict[str, Any]:  # pragma: no cover
        return {}


class _FakeProvider:
    def __init__(self, response_text: str) -> None:
        self.model = _FakeJudgeModel(response_text)
        self.build_count = 0

    def build(self, model_config: ModelConfig) -> _FakeJudgeModel:
        self.build_count += 1
        return self.model


class _CaptureWrappedKind:
    """Records the rubric it was called with and returns a stub COMPLETED result."""

    NAME = "capture_wrapped_kind"

    def __init__(self) -> None:
        self.rubric_seen: Rubric | None = None
        self.kind_config_seen: Any = "SENTINEL"

    def evaluate(self, **kwargs: Any) -> JudgeResult:
        self.rubric_seen = kwargs["rubric"]
        self.kind_config_seen = kwargs["kind_config"]
        return JudgeResult(
            status=JudgeStatus.COMPLETED,
            usage=JudgeUsage(calls=1, prompt_tokens=100, completion_tokens=20, cost_usd=0.001),
            reasons="wrapped-reasons",
            score=0.75,
            binary_pass=True,
        )


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    clear_anchor_cache()
    yield
    clear_anchor_cache()


@pytest.fixture
def register_capture_kind(monkeypatch):
    """Monkeypatch ``load_judge_kind`` inside auto_anchored so tests can inject a
    capture kind without touching entry-point registration.
    """
    instance = _CaptureWrappedKind()

    class _Cls:
        NAME = _CaptureWrappedKind.NAME

        def evaluate(self, **kwargs: Any) -> JudgeResult:
            return instance.evaluate(**kwargs)

    def _fake_load(name: str) -> type:
        assert (
            name == _CaptureWrappedKind.NAME
        ), f"test expected wrapped_kind={_CaptureWrappedKind.NAME!r}, got {name!r}"
        return _Cls

    # The auto_anchored module imports load_judge_kind lazily inside
    # _dispatch_wrapped(), so we patch the source of truth.
    monkeypatch.setattr("tolokaforge.core.plugin_registry.load_judge_kind", _fake_load)
    yield instance


def _rubric_with_two_graded_one_binary() -> Rubric:
    return Rubric(
        criteria=[
            Criterion(id="mentions_id", description="Mentions the id.", kind="binary"),
            Criterion(id="clarity", description="Reads clearly.", kind="graded"),
            Criterion(id="tone", description="Is professional in tone.", kind="graded"),
        ]
    )


def _model_config() -> ModelConfig:
    return ModelConfig(provider="openai", name="gpt-4o-mini", temperature=0.0)


def _evaluate_kwargs(rubric: Rubric, provider: _FakeProvider, kind_config: dict | None = None):
    return {
        "rubric": rubric,
        "agent_system_prompt": "",
        "transcript": [],
        "db_reader": None,
        "kb_search": None,
        "workspace_dir": None,
        "extra_read_tools": [],
        "state_diff": None,
        "judge_model_config": _model_config(),
        "judge_model_provider": provider,
        "disable_knowledge_search": False,
        "custom_system_prompt": None,
        "include_agent_system_prompt": True,
        "kind_config": {"wrapped_kind": _CaptureWrappedKind.NAME, **(kind_config or {})},
        "logger": None,
    }


# --- Tests ---------------------------------------------------------------


def test_name_matches_entry_point() -> None:
    assert AutoAnchoredRubricJudgeKind.NAME == "auto_anchored_rubric"


def test_warmup_call_fires_once_and_anchors_land_in_wrapped_rubric(register_capture_kind):
    capture = register_capture_kind
    provider = _FakeProvider(
        response_text='{"clarity": "A clear reply is one paragraph.", '
        '"tone": "A professional tone is neither casual nor stiff."}'
    )
    rubric = _rubric_with_two_graded_one_binary()

    AutoAnchoredRubricJudgeKind().evaluate(**_evaluate_kwargs(rubric, provider))

    assert provider.model.call_count == 1  # warm-up call fired
    assert capture.rubric_seen is not None
    by_id = {c.id: c for c in capture.rubric_seen.criteria}
    assert by_id["clarity"].expected == "auto-anchor: A clear reply is one paragraph."
    assert by_id["tone"].expected == (
        "auto-anchor: A professional tone is neither casual nor stiff."
    )
    # Author-written anchor (binary) untouched; it had no expected so it stays None.
    assert by_id["mentions_id"].expected is None


def test_second_call_hits_cache(register_capture_kind):
    provider = _FakeProvider(response_text='{"clarity": "one paragraph", "tone": "professional"}')
    rubric = _rubric_with_two_graded_one_binary()

    AutoAnchoredRubricJudgeKind().evaluate(**_evaluate_kwargs(rubric, provider))
    AutoAnchoredRubricJudgeKind().evaluate(**_evaluate_kwargs(rubric, provider))

    assert provider.model.call_count == 1  # still one warm-up


def test_author_written_anchor_is_passed_through_unchanged(register_capture_kind):
    capture = register_capture_kind
    provider = _FakeProvider(response_text='{"clarity": "auto-def"}')
    rubric = Rubric(
        criteria=[
            Criterion(
                id="tone",
                description="tone criterion",
                kind="graded",
                expected="AUTHOR-WRITTEN ANCHOR",
            ),
            Criterion(id="clarity", description="clarity", kind="graded"),
        ]
    )
    AutoAnchoredRubricJudgeKind().evaluate(**_evaluate_kwargs(rubric, provider))
    by_id = {c.id: c for c in capture.rubric_seen.criteria}
    assert by_id["tone"].expected == "AUTHOR-WRITTEN ANCHOR"
    assert by_id["clarity"].expected == "auto-anchor: auto-def"


def test_warmup_usage_folded_into_returned_judge_result(register_capture_kind):
    provider = _FakeProvider(response_text='{"clarity": "one paragraph", "tone": "professional"}')
    rubric = _rubric_with_two_graded_one_binary()

    result = AutoAnchoredRubricJudgeKind().evaluate(**_evaluate_kwargs(rubric, provider))

    # Wrapped stub reported calls=1 + 100 prompt + 20 completion + 0.001 usd.
    # Warm-up adds calls=1 + 42 prompt + 7 completion + 0.0001 usd.
    assert result.usage.calls == 2
    assert result.usage.prompt_tokens == 142
    assert result.usage.completion_tokens == 27
    assert result.usage.cost_usd == pytest.approx(0.0011)


def test_anchor_audit_prefixes_reasons(register_capture_kind):
    provider = _FakeProvider(response_text='{"clarity": "one paragraph", "tone": "professional"}')
    rubric = _rubric_with_two_graded_one_binary()

    result = AutoAnchoredRubricJudgeKind().evaluate(**_evaluate_kwargs(rubric, provider))

    assert "auto_anchored_rubric warm-up anchors:" in result.reasons
    assert "clarity: one paragraph" in result.reasons
    assert "tone: professional" in result.reasons
    assert result.reasons.endswith("wrapped-reasons")


def test_rubric_with_no_unanchored_graded_skips_warmup(register_capture_kind):
    provider = _FakeProvider(response_text="{}")  # would fail parse if called
    rubric = Rubric(
        criteria=[
            Criterion(id="mentions_id", description="binary", kind="binary"),
            Criterion(
                id="clarity",
                description="clarity",
                kind="graded",
                expected="author-anchor",
            ),
        ]
    )
    result = AutoAnchoredRubricJudgeKind().evaluate(**_evaluate_kwargs(rubric, provider))
    assert provider.model.call_count == 0  # no warm-up call
    assert result.status == JudgeStatus.COMPLETED


def test_unknown_kind_config_key_fails_loud(register_capture_kind):
    provider = _FakeProvider(response_text="{}")
    rubric = _rubric_with_two_graded_one_binary()

    with pytest.raises(ValueError) as exc_info:
        AutoAnchoredRubricJudgeKind().evaluate(
            **_evaluate_kwargs(rubric, provider, kind_config={"n_samples": 3})
        )
    assert "auto_anchored_rubric" in str(exc_info.value)
    assert "n_samples" in str(exc_info.value)


def test_warmup_non_json_response_fails_loud(register_capture_kind):
    provider = _FakeProvider(response_text="not json at all")
    rubric = _rubric_with_two_graded_one_binary()

    with pytest.raises(PerRubricAnchorGeneratorError) as exc_info:
        AutoAnchoredRubricJudgeKind().evaluate(**_evaluate_kwargs(rubric, provider))
    assert "non-JSON" in str(exc_info.value)


def test_warmup_missing_anchor_key_fails_loud(register_capture_kind):
    provider = _FakeProvider(response_text='{"clarity": "one paragraph"}')  # missing tone
    rubric = _rubric_with_two_graded_one_binary()

    with pytest.raises(PerRubricAnchorGeneratorError) as exc_info:
        AutoAnchoredRubricJudgeKind().evaluate(**_evaluate_kwargs(rubric, provider))
    assert "tone" in str(exc_info.value)


def test_warmup_response_strips_code_fence(register_capture_kind):
    capture = register_capture_kind
    provider = _FakeProvider(
        response_text='```json\n{"clarity": "one paragraph", "tone": "professional"}\n```'
    )
    rubric = _rubric_with_two_graded_one_binary()
    AutoAnchoredRubricJudgeKind().evaluate(**_evaluate_kwargs(rubric, provider))
    by_id = {c.id: c for c in capture.rubric_seen.criteria}
    assert by_id["clarity"].expected == "auto-anchor: one paragraph"
